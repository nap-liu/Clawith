"""Provider-neutral lifecycle receipts for visible outbound IM messages.

One durable ``ChatMessage`` may create multiple provider-visible artifacts
(chunks, fallbacks, cards).  This module keeps that one-to-many mapping in the
existing ``message_meta`` JSON and exposes one recall operation keyed only by
the local ``ChatMessage.id``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.services import session_query

DELIVERY_META_VERSION = 1
DELIVERY_LEASE = timedelta(minutes=2)
RECALL_LEASE = timedelta(minutes=2)


@dataclass(frozen=True)
class IMDeliveryPart:
    """One provider-visible message artifact."""

    transport: str
    provider_message_id: str | None = None
    conversation_ref: str | None = None
    artifact_role: str = "final"
    recallable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_meta(self, index: int) -> dict[str, Any]:
        recall_status = "available" if self.recallable and self.provider_message_id else "unsupported"
        return {
            "part_id": str(index),
            "transport": self.transport,
            "artifact_role": self.artifact_role,
            "provider_message_id": str(self.provider_message_id or ""),
            "conversation_ref": str(self.conversation_ref or ""),
            "send_status": "sent",
            "recall_status": recall_status,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class IMDeliveryResult:
    """Normalized result returned by every outbound IM transport."""

    ok: bool
    channel: str
    parts: tuple[IMDeliveryPart, ...] = ()
    status: str = "sent"
    error: str | None = None

    @classmethod
    def sent(cls, channel: str, *parts: IMDeliveryPart) -> IMDeliveryResult:
        return cls(ok=True, channel=channel, parts=tuple(parts), status="sent")

    @classmethod
    def pending(cls, channel: str) -> IMDeliveryResult:
        return cls(ok=True, channel=channel, status="pending")

    @classmethod
    def unsupported_delivery(
        cls,
        channel: str,
        transport: str,
        *,
        conversation_ref: str | None = None,
    ) -> IMDeliveryResult:
        return cls.sent(
            channel,
            IMDeliveryPart(
                transport=transport,
                conversation_ref=conversation_ref,
                recallable=False,
            ),
        )

    @classmethod
    def failed(cls, channel: str, error: str) -> IMDeliveryResult:
        return cls(ok=False, channel=channel, status="failed", error=error)

    @classmethod
    def unknown(cls, channel: str, error: str) -> IMDeliveryResult:
        return cls(ok=False, channel=channel, status="unknown", error=error)

    @classmethod
    def from_exception(cls, channel: str, exc: BaseException) -> IMDeliveryResult:
        if isinstance(
            exc,
            (
                asyncio.TimeoutError,
                ConnectionError,
                httpx.TimeoutException,
                httpx.TransportError,
            ),
        ):
            return cls.unknown(channel, type(exc).__name__)
        return cls.failed(channel, type(exc).__name__)

    def to_meta(self) -> dict[str, Any]:
        parts = [part.to_meta(index) for index, part in enumerate(self.parts)]
        recallable = any(part.get("recall_status") == "available" for part in parts)
        return {
            "version": DELIVERY_META_VERSION,
            "channel": self.channel,
            "status": self.status,
            "updated_at": datetime.now(UTC).isoformat(),
            "parts": parts,
            "recall": {
                "status": "available" if recallable else "unsupported",
                "attempt_id": None,
                "started_at": None,
                "completed_at": None,
            },
            **({"error": self.error} if self.error else {}),
        }


def attach_delivery_to_meta(message_meta: dict | None, result: IMDeliveryResult) -> dict:
    next_meta = dict(message_meta or {})
    next_meta["delivery"] = result.to_meta()
    # Keep the legacy fields readable while new callers migrate to delivery.parts.
    next_meta["delivery_status"] = result.status
    if len(result.parts) == 1 and result.parts[0].provider_message_id:
        next_meta["external_message_id"] = str(result.parts[0].provider_message_id)
    return next_meta


def _delivery_part_key(part: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(part.get("transport") or ""),
        str(part.get("provider_message_id") or ""),
        str(part.get("conversation_ref") or ""),
        str(part.get("artifact_role") or ""),
    )


def _merge_delivery_into_meta(message_meta: dict | None, result: IMDeliveryResult) -> dict:
    current_meta = dict(message_meta or {})
    current = current_meta.get("delivery")
    if not isinstance(current, dict):
        return attach_delivery_to_meta(current_meta, result)

    incoming = result.to_meta()
    if not incoming.get("channel"):
        incoming["channel"] = str(current.get("channel") or "")
    combined: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for part in [*(current.get("parts") or []), *(incoming.get("parts") or [])]:
        if not isinstance(part, dict):
            continue
        key = _delivery_part_key(part)
        if key in seen:
            continue
        seen.add(key)
        combined.append({**part, "part_id": str(len(combined))})
    incoming["parts"] = combined
    if result.status == "pending":
        incoming["status"] = str(current.get("status") or "pending")
    if combined and incoming.get("status") in {"failed", "unknown"}:
        incoming["status"] = "partial"
        incoming["uncertain"] = result.status == "unknown"
    recallable = any(part.get("recall_status") == "available" for part in combined)
    incoming["recall"] = {
        "status": "available" if recallable else "unsupported",
        "attempt_id": None,
        "started_at": None,
        "completed_at": None,
    }
    current_meta["delivery"] = incoming
    current_meta["delivery_status"] = incoming["status"]
    if len(combined) == 1 and combined[0].get("provider_message_id"):
        current_meta["external_message_id"] = str(combined[0]["provider_message_id"])
    return current_meta


async def register_delivery(message_id: uuid.UUID | str, result: IMDeliveryResult) -> bool:
    """Attach a normalized receipt to an existing ChatMessage."""
    try:
        local_id = uuid.UUID(str(message_id))
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        row = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.id == local_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.message_meta = _merge_delivery_into_meta(row.message_meta, result)
        await db.commit()
        return True


async def append_delivery_part(
    message_id: uuid.UUID | str,
    part: IMDeliveryPart,
) -> bool:
    """Durably append one provider-visible part immediately after it is sent."""
    return await register_delivery(
        message_id,
        IMDeliveryResult(ok=True, channel="", parts=(part,), status="pending"),
    )


async def deliver_persisted_message(
    *,
    message_id: uuid.UUID | str,
    agent_id: uuid.UUID,
    runtime,
    message: str,
    **delivery_kwargs,
) -> IMDeliveryResult:
    """Deliver one already-pending ChatMessage and durably finalize its receipt."""
    from app.services.turn_runtime import deliver_message_with_receipt

    async def _record_part(part: IMDeliveryPart) -> None:
        if not await append_delivery_part(message_id, part):
            raise RuntimeError("delivery_part_persistence_failed")

    result = await deliver_message_with_receipt(
        agent_id=agent_id,
        runtime=runtime,
        message=message,
        on_part=_record_part,
        **delivery_kwargs,
    )
    if not await register_delivery(message_id, result):
        raise RuntimeError("delivery_finalization_failed")
    return result


@dataclass(frozen=True)
class PartRecallResult:
    part_id: str
    status: str
    error: str | None = None


RecallAdapter = Callable[[ChannelConfig, list[dict[str, Any]]], Awaitable[list[PartRecallResult]]]


def _part_result(part: dict, status: str, error: str | None = None) -> PartRecallResult:
    return PartRecallResult(str(part.get("part_id") or ""), status, error)


def _dingtalk_failed_results(value: Any) -> dict[str, str]:
    """Normalize both documented and observed DingTalk failure shapes."""
    if isinstance(value, dict):
        return {str(key): str(error) for key, error in value.items()}
    if not isinstance(value, list):
        return {}
    failures: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        key = str(item.get("processQueryKey") or item.get("key") or "")
        if key:
            failures[key] = str(
                item.get("code")
                or item.get("message")
                or item.get("reason")
                or "provider_rejected"
            )
    return failures


async def _unsupported_recall(
    _config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    return [_part_result(part, "unsupported") for part in parts]


async def _run_recall_adapter(
    adapter: RecallAdapter,
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    """Contain provider/SDK failures so a claimed attempt can always finalize."""
    try:
        return await adapter(config, parts)
    except Exception as exc:  # noqa: BLE001 - provider SDKs do not share an exception base
        logger.opt(exception=True).warning(
            "[im_delivery] recall adapter={} failed",
            getattr(adapter, "__name__", type(adapter).__name__),
        )
        return [_part_result(part, "failed", type(exc).__name__) for part in parts]


async def _dingtalk_token(config: ChannelConfig) -> str:
    from app.services.dingtalk_service import get_dingtalk_access_token

    result = await get_dingtalk_access_token(config.app_id or "", config.app_secret or "")
    return str(result.get("access_token") or "")


async def _recall_dingtalk_oto(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    token = await _dingtalk_token(config)
    if not token:
        return [_part_result(part, "failed", "access_token_unavailable") for part in parts]

    by_key = {
        str(part.get("provider_message_id") or ""): part
        for part in parts
        if part.get("provider_message_id")
    }
    pending = set(by_key)
    results: dict[str, PartRecallResult] = {}
    headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}

    # DingTalk can briefly return a per-key failure immediately after send even
    # though the receipt is valid. Retry only failed keys with a short bounded backoff.
    for delay in (0, 1, 2):
        if not pending:
            break
        if delay:
            await asyncio.sleep(delay)
        keys = list(pending)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/otoMessages/batchRecall",
                    headers=headers,
                    json={"robotCode": config.app_id, "processQueryKeys": keys},
                )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if delay == 2:
                for key in pending:
                    results[key] = _part_result(by_key[key], "failed", type(exc).__name__)
            continue
        if response.status_code >= 400:
            if delay == 2:
                error = str(data.get("code") or data.get("message") or response.status_code)
                for key in pending:
                    results[key] = _part_result(by_key[key], "failed", error)
            continue
        succeeded = {str(value) for value in (data.get("successResult") or [])}
        failed = _dingtalk_failed_results(data.get("failedResult"))
        for key in succeeded & pending:
            results[key] = _part_result(by_key[key], "recalled")
            pending.discard(key)
        if delay == 2:
            for key in pending:
                results[key] = _part_result(by_key[key], "failed", failed.get(key) or "provider_rejected")

    return [results.get(key, _part_result(part, "failed", "missing_provider_result")) for key, part in by_key.items()]


async def _recall_dingtalk_group(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    token = await _dingtalk_token(config)
    if not token:
        return [_part_result(part, "failed", "access_token_unavailable") for part in parts]

    results: list[PartRecallResult] = []
    headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
    groups: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        groups.setdefault(str(part.get("conversation_ref") or ""), []).append(part)
    async with httpx.AsyncClient(timeout=30) as client:
        for conversation_ref, grouped in groups.items():
            by_key = {
                str(part.get("provider_message_id") or ""): part
                for part in grouped
                if part.get("provider_message_id")
            }
            pending = set(by_key)
            group_results: dict[str, PartRecallResult] = {}
            last_failed: dict[str, str] = {}
            for delay in (0, 1, 2):
                if not pending:
                    break
                if delay:
                    await asyncio.sleep(delay)
                keys = list(pending)
                try:
                    response = await client.post(
                        "https://api.dingtalk.com/v1.0/robot/groupMessages/recall",
                        headers=headers,
                        json={
                            "robotCode": config.app_id,
                            "openConversationId": conversation_ref,
                            "processQueryKeys": keys,
                        },
                    )
                    data = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    if delay == 2:
                        for key in pending:
                            group_results[key] = _part_result(
                                by_key[key], "failed", type(exc).__name__
                            )
                    continue
                if response.status_code >= 400:
                    if delay == 2:
                        error = str(
                            data.get("code") or data.get("message") or response.status_code
                        )
                        for key in pending:
                            group_results[key] = _part_result(by_key[key], "failed", error)
                    continue
                succeeded = {str(value) for value in (data.get("successResult") or [])}
                last_failed = _dingtalk_failed_results(data.get("failedResult"))
                for key in succeeded & pending:
                    group_results[key] = _part_result(by_key[key], "recalled")
                    pending.discard(key)
                if delay == 2:
                    for key in pending:
                        group_results[key] = _part_result(
                            by_key[key],
                            "failed",
                            last_failed.get(key) or "provider_rejected",
                        )
            results.extend(
                group_results.get(key, _part_result(part, "failed", "missing_provider_result"))
                for key, part in by_key.items()
            )
    return results


async def _recall_feishu(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(config.app_id or "", config.app_secret or "")
    if not token:
        return [_part_result(part, "failed", "access_token_unavailable") for part in parts]

    async def recall_one(part: dict[str, Any]) -> PartRecallResult:
        message_id = quote(str(part.get("provider_message_id") or ""), safe="")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.delete(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return _part_result(part, "failed", type(exc).__name__)
        if response.status_code < 400 and data.get("code", 0) == 0:
            return _part_result(part, "recalled")
        return _part_result(part, "failed", str(data.get("code") or data.get("msg") or response.status_code))

    return list(await asyncio.gather(*(recall_one(part) for part in parts)))


async def _recall_wecom_app(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    from app.services.wecom_service import get_wecom_access_token

    token = (await get_wecom_access_token(config.app_id or "", config.app_secret or "")).get("access_token")
    if not token:
        return [_part_result(part, "failed", "access_token_unavailable") for part in parts]

    async def recall_one(part: dict[str, Any]) -> PartRecallResult:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/message/recall",
                    params={"access_token": token},
                    json={"msgid": str(part.get("provider_message_id") or "")},
                )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return _part_result(part, "failed", type(exc).__name__)
        if response.status_code < 400 and data.get("errcode") == 0:
            return _part_result(part, "recalled")
        return _part_result(part, "failed", str(data.get("errcode") or data.get("errmsg") or response.status_code))

    return list(await asyncio.gather(*(recall_one(part) for part in parts)))


async def _recall_slack(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    token = str(config.app_secret or "")

    async def recall_one(part: dict[str, Any]) -> PartRecallResult:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    "https://slack.com/api/chat.delete",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "channel": str(part.get("conversation_ref") or ""),
                        "ts": str(part.get("provider_message_id") or ""),
                    },
                )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return _part_result(part, "failed", type(exc).__name__)
        if response.status_code < 400 and data.get("ok"):
            return _part_result(part, "recalled")
        if data.get("error") == "message_not_found":
            return _part_result(part, "recalled")
        return _part_result(part, "failed", str(data.get("error") or response.status_code))

    return list(await asyncio.gather(*(recall_one(part) for part in parts)))


async def _recall_discord_gateway(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    token = str(config.app_secret or "")

    async def recall_one(part: dict[str, Any]) -> PartRecallResult:
        channel_id = quote(str(part.get("conversation_ref") or ""), safe="")
        message_id = quote(str(part.get("provider_message_id") or ""), safe="")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.delete(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
                    headers={"Authorization": f"Bot {token}"},
                )
        except httpx.HTTPError as exc:
            return _part_result(part, "failed", type(exc).__name__)
        if response.status_code in {204, 404}:
            return _part_result(part, "recalled")
        return _part_result(part, "failed", str(response.status_code))

    return list(await asyncio.gather(*(recall_one(part) for part in parts)))


async def _recall_teams(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    from app.api.teams import _get_teams_access_token

    token = await _get_teams_access_token(config)
    if not token:
        return [_part_result(part, "failed", "access_token_unavailable") for part in parts]

    async def recall_one(part: dict[str, Any]) -> PartRecallResult:
        metadata = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
        service_url = str(metadata.get("service_url") or (config.extra_config or {}).get("service_url") or "").rstrip("/")
        conversation_id = quote(str(part.get("conversation_ref") or ""), safe="")
        activity_id = quote(str(part.get("provider_message_id") or ""), safe="")
        if not service_url:
            return _part_result(part, "failed", "service_url_unavailable")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.delete(
                    f"{service_url}/v3/conversations/{conversation_id}/activities/{activity_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            return _part_result(part, "failed", type(exc).__name__)
        if response.status_code in {200, 202, 204, 404}:
            return _part_result(part, "recalled")
        return _part_result(part, "failed", str(response.status_code))

    return list(await asyncio.gather(*(recall_one(part) for part in parts)))


IM_RECALL_ADAPTERS: dict[str, RecallAdapter] = {
    "dingtalk_openapi_oto": _recall_dingtalk_oto,
    "dingtalk_openapi_group": _recall_dingtalk_group,
    "dingtalk_session_webhook": _unsupported_recall,
    "feishu_message": _recall_feishu,
    "wecom_app": _recall_wecom_app,
    "wecom_appchat": _unsupported_recall,
    "wecom_aibot_stream": _unsupported_recall,
    "wecom_kf": _unsupported_recall,
    "slack": _recall_slack,
    "discord_gateway": _recall_discord_gateway,
    "discord_interaction": _unsupported_recall,
    "microsoft_teams": _recall_teams,
    "whatsapp_cloud": _unsupported_recall,
    "wechat_ilink": _unsupported_recall,
    "websocket": _unsupported_recall,
}


def _channel_config_type(channel: str) -> str:
    return "microsoft_teams" if channel in {"teams", "microsoft_teams"} else channel


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


async def recall_message(
    *,
    agent_id: uuid.UUID,
    message_id: uuid.UUID | str,
    user_id: uuid.UUID | None,
    current_session_id: uuid.UUID | str | None,
) -> dict[str, Any]:
    """Recall one local outbound message through its recorded transports."""
    try:
        local_id = uuid.UUID(str(message_id))
    except (TypeError, ValueError):
        return {"status": "not_found", "message_id": str(message_id)}

    attempt_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        candidate = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == local_id,
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        if agent is None or candidate is None:
            return {"status": "not_found", "message_id": str(local_id)}
        _scope, session_where = await session_query.resolve_scope(
            db,
            agent,
            str(current_session_id or ""),
            user_id,
        )
        target_session_id = session_query._as_uuid(candidate.conversation_id)
        if session_where is None or target_session_id is None:
            return {"status": "not_found", "message_id": str(local_id)}
        in_scope = (
            await db.execute(
                select(ChatSession.id).where(
                    ChatSession.id == target_session_id,
                    session_where,
                )
            )
        ).scalar_one_or_none()
        if in_scope is None:
            return {"status": "not_found", "message_id": str(local_id)}

        row = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.id == local_id,
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.role == "assistant",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return {"status": "not_found", "message_id": str(local_id)}
        meta = dict(row.message_meta or {})
        delivery = dict(meta.get("delivery") or {})
        parts = [dict(part) for part in (delivery.get("parts") or []) if isinstance(part, dict)]
        delivery_status = str(delivery.get("status") or "")
        if delivery_status == "pending":
            updated_at = _parse_iso(delivery.get("updated_at"))
            if updated_at is not None and now - updated_at >= DELIVERY_LEASE:
                delivery["status"] = "unknown"
                delivery["updated_at"] = now.isoformat()
                meta["delivery"] = delivery
                row.message_meta = meta
                await db.commit()
                return {"status": "unknown", "message_id": str(local_id)}
            return {"status": "pending", "message_id": str(local_id)}
        if delivery_status == "unknown":
            return {"status": "unknown", "message_id": str(local_id)}
        if delivery_status == "failed" and not parts:
            return {
                "status": "failed",
                "reason": "message_not_delivered",
                "message_id": str(local_id),
            }
        if not parts:
            return {"status": "unsupported", "reason": "missing_receipt", "message_id": str(local_id)}
        recall = dict(delivery.get("recall") or {})
        if recall.get("status") == "recalled":
            return {"status": "already_recalled", "message_id": str(local_id)}
        started_at = _parse_iso(recall.get("started_at"))
        if recall.get("status") == "recalling" and started_at and now - started_at < RECALL_LEASE:
            return {"status": "recalling", "message_id": str(local_id)}
        recall.update({"status": "recalling", "attempt_id": attempt_id, "started_at": now.isoformat()})
        delivery["recall"] = recall
        meta["delivery"] = delivery
        row.message_meta = meta
        channel = str(delivery.get("channel") or meta.get("source_channel") or "")
        await db.commit()

    groups: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        if part.get("recall_status") == "recalled":
            continue
        if part.get("recall_status") == "unsupported":
            groups.setdefault("", []).append(part)
            continue
        groups.setdefault(str(part.get("transport") or ""), []).append(part)

    unsupported_groups = {
        transport: transport_parts
        for transport, transport_parts in groups.items()
        if not transport
        or IM_RECALL_ADAPTERS.get(transport, _unsupported_recall) is _unsupported_recall
    }
    provider_groups = {
        transport: transport_parts
        for transport, transport_parts in groups.items()
        if transport not in unsupported_groups
    }
    adapter_results = [
        _part_result(part, "unsupported")
        for transport_parts in unsupported_groups.values()
        for part in transport_parts
    ]
    if provider_groups:
        config_type = _channel_config_type(channel)
        async with async_session() as db:
            config = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == config_type,
                        ChannelConfig.is_configured.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if config is None:
            adapter_results.extend(
                _part_result(part, "failed", "channel_config_unavailable")
                for transport_parts in provider_groups.values()
                for part in transport_parts
            )
        else:
            tasks = [
                _run_recall_adapter(
                    IM_RECALL_ADAPTERS[transport],
                    config,
                    transport_parts,
                )
                for transport, transport_parts in provider_groups.items()
            ]
            adapter_results.extend(
                item
                for group in await asyncio.gather(*tasks)
                for item in group
            )

    result_by_id = {result.part_id: result for result in adapter_results}
    completed_at = datetime.now(UTC).isoformat()
    async with async_session() as db:
        row = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.id == local_id, ChatMessage.agent_id == agent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return {"status": "failed", "reason": "message_disappeared", "message_id": str(local_id)}
        meta = dict(row.message_meta or {})
        delivery = dict(meta.get("delivery") or {})
        recall = dict(delivery.get("recall") or {})
        if recall.get("attempt_id") != attempt_id:
            return {"status": str(recall.get("status") or "recalling"), "message_id": str(local_id)}
        stored_parts = [dict(part) for part in (delivery.get("parts") or []) if isinstance(part, dict)]
        for part in stored_parts:
            result = result_by_id.get(str(part.get("part_id") or ""))
            if result is None:
                if part.get("recall_status") != "recalled":
                    part["recall_status"] = "failed"
                    part["recall_error"] = "missing_adapter_result"
                continue
            part["recall_status"] = result.status
            if result.error:
                part["recall_error"] = result.error
            else:
                part.pop("recall_error", None)
        statuses = [str(part.get("recall_status") or "unsupported") for part in stored_parts]
        recalled_count = sum(status == "recalled" for status in statuses)
        uncertain_delivery = bool(delivery.get("uncertain"))
        if statuses and recalled_count == len(statuses) and not uncertain_delivery:
            aggregate = "recalled"
        elif recalled_count:
            aggregate = "partial"
        elif statuses and all(status == "unsupported" for status in statuses):
            aggregate = "unsupported"
        elif statuses and all(status == "expired" for status in statuses):
            aggregate = "expired"
        else:
            aggregate = "failed"
        recall.update({"status": aggregate, "completed_at": completed_at})
        delivery["parts"] = stored_parts
        delivery["recall"] = recall
        meta["delivery"] = delivery
        row.message_meta = meta
        await db.commit()

    logger.info("[im_delivery] recall message={} status={}", local_id, aggregate)
    return {
        "status": aggregate,
        "message_id": str(local_id),
        "parts": [
            {
                "part_id": str(part.get("part_id") or ""),
                "transport": str(part.get("transport") or ""),
                "status": str(part.get("recall_status") or ""),
                **({"error": str(part.get("recall_error"))} if part.get("recall_error") else {}),
            }
            for part in stored_parts
        ],
    }
