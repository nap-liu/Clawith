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
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
from loguru import logger
from sqlalchemy import select, text, update

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.services import session_query
from app.services.user_output import sanitize_user_visible_text

DELIVERY_META_VERSION = 1
DELIVERY_LEASE = timedelta(minutes=2)
RECALL_LEASE = timedelta(minutes=2)

DeliveryPartObserver = Callable[["IMDeliveryPart"], Awaitable[None]]
DeliveryCallback = Callable[
    [str, DeliveryPartObserver], Awaitable["IMDeliveryResult"]
]
DeliveryClaimObserver = Callable[[uuid.UUID], Awaitable[None]]


class DeliveryReceiptPersistenceError(RuntimeError):
    """A provider side effect succeeded but its durable receipt did not persist."""

# A test-only observation seam for proving the database claim is serialized.
# Production callers leave it unset, so the hot path only pays one ContextVar read.
delivery_claim_observer: ContextVar[DeliveryClaimObserver | None] = ContextVar(
    "delivery_claim_observer",
    default=None,
)


@dataclass(frozen=True)
class MentionIntent:
    """One native mention request prepared for the selected IM adapter.

    ``target_ids`` are short-lived opaque provider identifiers. Canonical
    platform user IDs belong in durable message metadata, never in this
    delivery-only envelope.
    """

    scope: Literal["users", "all"]
    target_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scope not in {"users", "all"}:
            raise ValueError("invalid mention scope")
        if self.scope == "users" and not self.target_ids:
            raise ValueError("user mention requires target_ids")
        if self.scope == "all" and self.target_ids:
            raise ValueError("all mention cannot contain target_ids")


@dataclass(frozen=True)
class IMDeliveryPart:
    """One provider-visible message artifact."""

    transport: str
    provider_message_id: str | None = None
    conversation_ref: str | None = None
    artifact_role: str = "final"
    recallable: bool = True
    send_status: str = "sent"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_meta(self, index: int) -> dict[str, Any]:
        recall_status = (
            "available"
            if self.send_status == "sent" and self.recallable and self.provider_message_id
            else "unsupported"
        )
        return {
            "part_id": str(index),
            "transport": self.transport,
            "artifact_role": self.artifact_role,
            "provider_message_id": str(self.provider_message_id or ""),
            "conversation_ref": str(self.conversation_ref or ""),
            "send_status": self.send_status,
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
                DeliveryReceiptPersistenceError,
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
    indexes: dict[tuple[str, str, str, str], int] = {}
    for part in [*(current.get("parts") or []), *(incoming.get("parts") or [])]:
        if not isinstance(part, dict):
            continue
        key = _delivery_part_key(part)
        if key in indexes:
            index = indexes[key]
            combined[index] = {**combined[index], **part, "part_id": str(index)}
            continue
        indexes[key] = len(combined)
        combined.append({**part, "part_id": str(len(combined))})
    incoming["parts"] = combined
    if result.status == "pending":
        incoming["status"] = str(current.get("status") or "pending")
        # Appending a provider part is an incremental update, not completion of
        # the outbound attempt.  Preserve the lease until finalization so no
        # concurrent worker can claim the remaining parts.
        for key in ("attempt_id", "started_at"):
            if current.get(key):
                incoming[key] = current[key]
    sent_parts = [part for part in combined if part.get("send_status") == "sent"]
    if sent_parts and incoming.get("status") in {"failed", "unknown"}:
        incoming["status"] = "partial"
        incoming["uncertain"] = result.status == "unknown"
    recallable = any(part.get("recall_status") == "available" for part in sent_parts)
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
    try:
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
    except DeliveryReceiptPersistenceError:
        raise
    except Exception as exc:
        raise DeliveryReceiptPersistenceError(
            "provider delivery receipt persistence failed"
        ) from exc


async def append_delivery_part(
    message_id: uuid.UUID | str,
    part: IMDeliveryPart,
) -> bool:
    """Durably append one provider-visible part immediately after it is sent."""
    try:
        persisted = await register_delivery(
            message_id,
            IMDeliveryResult(ok=True, channel="", parts=(part,), status="pending"),
        )
    except DeliveryReceiptPersistenceError:
        # The provider ID is already known.  Make one bounded best-effort retry
        # that preserves it as uncertain, then propagate so callers never send
        # a fallback duplicate for a receipt failure.
        try:
            await register_delivery(
                message_id,
                IMDeliveryResult(
                    ok=False,
                    channel="",
                    parts=(part,),
                    status="unknown",
                    error="delivery_part_persistence_failed",
                ),
            )
        except DeliveryReceiptPersistenceError:
            pass
        raise
    if not persisted:
        raise DeliveryReceiptPersistenceError(
            "provider artifact receipt persistence failed"
        )
    return True


@dataclass(frozen=True)
class PersistedDeliveryRecorder:
    """Persist provider parts as they arrive, then atomically aggregate status."""

    message_id: uuid.UUID | str
    channel: str

    async def append(self, part: IMDeliveryPart) -> None:
        await append_delivery_part(self.message_id, part)

    async def finalize(self, result: IMDeliveryResult) -> IMDeliveryResult:
        if not await register_delivery(self.message_id, result):
            raise DeliveryReceiptPersistenceError(
                "provider delivery finalization failed"
            )
        return result

    async def sent(self) -> IMDeliveryResult:
        return await self.finalize(IMDeliveryResult.sent(self.channel))

    async def failed(self, exc: BaseException) -> IMDeliveryResult:
        return await self.finalize(IMDeliveryResult.from_exception(self.channel, exc))


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

    message = sanitize_user_visible_text(message or "")
    if not message.strip():
        raise ValueError("user-visible message must be non-empty after sanitization")

    try:
        local_id = uuid.UUID(str(message_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid persisted message id") from exc
    async with async_session() as db:
        row = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == local_id,
                    ChatMessage.agent_id == agent_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("persisted_message_not_found")
        if not getattr(runtime, "session_found", False):
            raise RuntimeError("persisted_message_runtime_session_required")
        if str(row.conversation_id or "") != str(
            getattr(runtime, "conversation_id", "") or ""
        ):
            raise RuntimeError("persisted_message_conversation_mismatch")
        if row.role not in {"assistant", "tool_call"}:
            raise RuntimeError("persisted_message_role_not_deliverable")
        meta = dict(row.message_meta) if isinstance(row.message_meta, dict) else {}
        delivery = (
            dict(meta.get("delivery"))
            if isinstance(meta.get("delivery"), dict)
            else {}
        )
        if str(delivery.get("status") or meta.get("delivery_status") or "") != "pending":
            raise RuntimeError("persisted_message_not_pending")
        now = datetime.now(UTC)
        active_attempt = str(delivery.get("attempt_id") or "")
        if active_attempt:
            try:
                started_at = datetime.fromisoformat(
                    str(delivery.get("started_at") or delivery.get("updated_at") or "")
                )
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                started_at = now
            if now - started_at < DELIVERY_LEASE:
                raise RuntimeError("persisted_message_delivery_in_progress")
            row.message_meta = _merge_delivery_into_meta(
                meta,
                IMDeliveryResult.unknown(
                    str(delivery.get("channel") or "im"),
                    "delivery_lease_expired",
                ),
            )
            await db.commit()
            raise RuntimeError("persisted_message_stale_delivery_unknown")
        observer = delivery_claim_observer.get()
        if observer is not None:
            await observer(row.id)
        delivery["attempt_id"] = str(uuid.uuid4())
        delivery["started_at"] = now.isoformat()
        delivery["updated_at"] = now.isoformat()
        meta["delivery"] = delivery
        row.message_meta = meta
        if row.role == "assistant" and row.content != message:
            row.content = message
        await db.commit()

    async def _record_part(part: IMDeliveryPart) -> None:
        await append_delivery_part(message_id, part)

    try:
        result = await deliver_message_with_receipt(
            agent_id=agent_id,
            runtime=runtime,
            message=message,
            on_part=_record_part,
            **delivery_kwargs,
        )
    except asyncio.CancelledError:
        await register_delivery(
            message_id,
            IMDeliveryResult.unknown(
                str(getattr(runtime, "source_channel", "") or "im"),
                "delivery_cancelled",
            ),
        )
        raise
    except Exception as exc:  # noqa: BLE001 - provider adapters vary
        result = IMDeliveryResult.from_exception(
            str(getattr(runtime, "source_channel", "") or "im"),
            exc,
        )
    if not await register_delivery(message_id, result):
        raise DeliveryReceiptPersistenceError(
            "provider delivery finalization failed"
        )
    return result


async def persist_delivery_anchor(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    channel: str,
    message: str,
    turn_anchor_id: uuid.UUID | None = None,
    artifact_role: str = "control",
    message_meta: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Commit one pending outbound message before any provider side effect."""
    message = sanitize_user_visible_text(message or "")
    if not message.strip():
        raise ValueError("user-visible message must be non-empty after sanitization")

    from app.services.chat_history import persist_assistant_reply

    meta = attach_delivery_to_meta(
        {
            **dict(message_meta or {}),
            "artifact_role": artifact_role,
            **(
                {"turn_anchor_id": str(turn_anchor_id)}
                if turn_anchor_id is not None
                else {}
            ),
        },
        IMDeliveryResult.pending(channel),
    )
    message_id = await persist_assistant_reply(
        async_session,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        content=message,
        message_meta=meta,
        # A progress/control artifact must not mark the model turn completed.
        # The final assistant reply owns that state transition.
        turn_anchor_id=None,
        required=True,
    )
    assert message_id is not None
    return message_id


async def update_delivery_message_content(
    message_id: uuid.UUID | str,
    *,
    agent_id: uuid.UUID,
    content: str,
    thinking: str | None = None,
    complete_turn: bool = False,
) -> bool:
    """Update an already-pending anchor without creating a second outbox row."""
    try:
        local_id = uuid.UUID(str(message_id))
    except (TypeError, ValueError):
        return False
    content = sanitize_user_visible_text(content or "")
    if not content.strip():
        raise ValueError("user-visible message must be non-empty after sanitization")
    sanitized_thinking = (
        sanitize_user_visible_text(thinking) if thinking and thinking.strip() else None
    )
    async with async_session() as db:
        row = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.id == local_id,
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.role == "assistant",
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.content = content
        row.thinking = sanitized_thinking
        if complete_turn:
            meta = dict(row.message_meta or {})
            if meta.get("turn_anchor_id"):
                meta["turn_status"] = "completed"
            row.message_meta = meta
        await db.commit()
    return True


async def deliver_persisted_with_callback(
    *,
    message_id: uuid.UUID | str,
    channel: str,
    message: str,
    deliver: DeliveryCallback,
) -> IMDeliveryResult:
    """Run an exact transport adapter and durably merge every observed part."""

    async def _record_part(part: IMDeliveryPart) -> None:
        await append_delivery_part(message_id, part)

    try:
        result = await deliver(sanitize_user_visible_text(message or ""), _record_part)
    except asyncio.CancelledError:
        await register_delivery(
            message_id,
            IMDeliveryResult.unknown(channel, "delivery_cancelled"),
        )
        raise
    except Exception as exc:  # noqa: BLE001 - provider adapters vary
        result = IMDeliveryResult.from_exception(channel, exc)
    if not await register_delivery(message_id, result):
        raise DeliveryReceiptPersistenceError(
            "provider delivery finalization failed"
        )
    return result


async def persist_and_deliver_message(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    channel: str,
    message: str,
    deliver: DeliveryCallback,
    turn_anchor_id: uuid.UUID | None = None,
    artifact_role: str = "control",
    message_meta: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, IMDeliveryResult]:
    """Provider-neutral outbox for transports that need an exact callback."""
    message = sanitize_user_visible_text(message or "")
    message_id = await persist_delivery_anchor(
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        channel=channel,
        message=message,
        turn_anchor_id=turn_anchor_id,
        artifact_role=artifact_role,
        message_meta=message_meta,
    )
    result = await deliver_persisted_with_callback(
        message_id=message_id,
        channel=channel,
        message=message,
        deliver=deliver,
    )
    return message_id, result


async def persist_and_deliver_runtime_message(
    *,
    agent_id: uuid.UUID,
    runtime,
    message: str,
    user_id: uuid.UUID | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    artifact_role: str = "control",
    message_meta: dict[str, Any] | None = None,
    **delivery_kwargs,
) -> tuple[uuid.UUID, IMDeliveryResult]:
    """Create the lifecycle anchor, then deliver one visible runtime message."""
    if not runtime.session_found:
        raise RuntimeError("runtime_session_required")
    message = sanitize_user_visible_text(message or "")
    if not message.strip():
        raise ValueError("user-visible message must be non-empty after sanitization")
    resolved_user_id = user_id
    if resolved_user_id is None:
        try:
            session_uuid = uuid.UUID(str(runtime.conversation_id))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("normalized_runtime_session_required") from exc
        async with async_session() as db:
            session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_uuid,
                        ChatSession.agent_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            if session is not None:
                resolved_user_id = session.user_id
            if resolved_user_id is None:
                agent = await db.get(Agent, agent_id)
                resolved_user_id = agent.creator_id if agent is not None else None
    if resolved_user_id is None:
        raise RuntimeError("runtime_message_user_unavailable")

    channel = str(runtime.source_channel or "web")
    message_id = await persist_delivery_anchor(
        agent_id=agent_id,
        user_id=resolved_user_id,
        conversation_id=str(runtime.conversation_id),
        channel=channel,
        message=message,
        artifact_role=artifact_role,
        message_meta=message_meta,
        turn_anchor_id=turn_anchor_id,
    )

    async def _deliver(
        delivery_message: str,
        on_part: DeliveryPartObserver,
    ) -> IMDeliveryResult:
        from app.services.turn_runtime import deliver_message_with_receipt

        return await deliver_message_with_receipt(
            agent_id=agent_id,
            runtime=runtime,
            message=delivery_message,
            on_part=on_part,
            **delivery_kwargs,
        )

    result = await deliver_persisted_with_callback(
        message_id=message_id,
        channel=channel,
        message=message,
        deliver=_deliver,
    )
    return message_id, result


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
    "slack_file": _unsupported_recall,
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
                    ChatMessage.role.in_(("assistant", "tool_call")),
                )
            )
        ).scalar_one_or_none()
        if agent is None or candidate is None:
            return {"status": "not_found", "message_id": str(local_id)}
        candidate_meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
        if candidate.role == "tool_call" and not isinstance(candidate_meta.get("delivery"), dict):
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
                    ChatMessage.role.in_(("assistant", "tool_call")),
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
                delivery["status"] = "partial" if parts else "unknown"
                delivery["uncertain"] = True
                delivery["updated_at"] = now.isoformat()
                meta["delivery"] = delivery
                row.message_meta = meta
                if not parts:
                    await db.commit()
                    return {"status": "unknown", "message_id": str(local_id)}
                delivery_status = "partial"
            else:
                return {"status": "pending", "message_id": str(local_id)}
        if delivery_status == "unknown":
            if not parts:
                return {"status": "unknown", "message_id": str(local_id)}
            delivery["uncertain"] = True
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
        conversation_id = str(row.conversation_id)
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
        # Progressive summaries may carry this message through later epochs.
        # Take the same session lock as the compactor, then unwind the entire
        # summarized context atomically when recall fully succeeds.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:session_id, 0))"),
            {"session_id": conversation_id},
        )
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
        if aggregate == "recalled" and row.compacted_into is not None:
            await db.execute(
                update(ChatCompaction)
                .where(
                    ChatCompaction.agent_id == agent_id,
                    ChatCompaction.session_id == conversation_id,
                    ChatCompaction.summary_validation_passed.is_(True),
                )
                .values(summary_validation_passed=False)
            )
            await db.execute(
                update(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.compacted_into.is_not(None),
                )
                .values(compacted_into=None)
            )
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
