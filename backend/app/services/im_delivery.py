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
from dataclasses import dataclass, field, replace
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


class ProviderResponseUncertainError(RuntimeError):
    """The provider may have accepted a send, but its response was unreadable."""

# A test-only observation seam for proving the database claim is serialized.
# Production callers leave it unset, so the hot path only pays one ContextVar read.
delivery_claim_observer: ContextVar[DeliveryClaimObserver | None] = ContextVar(
    "delivery_claim_observer",
    default=None,
)


@dataclass(frozen=True)
class MentionIntent:
    """One native mention request prepared for the selected IM adapter.

    ``target_ids`` are short-lived opaque provider identifiers and
    ``target_names`` are their display labels for transports whose mention
    payload requires an ID-to-name mapping. Canonical platform user IDs belong
    in durable message metadata, never in this delivery-only envelope.
    """

    scope: Literal["users", "all"]
    target_ids: tuple[str, ...] = ()
    target_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scope not in {"users", "all"}:
            raise ValueError("invalid mention scope")
        if self.scope == "users":
            if not self.target_ids:
                raise ValueError("user mention requires target_ids")
            if len(self.target_names) != len(self.target_ids):
                raise ValueError("user mention requires one target name per target id")
        if self.scope == "all" and (self.target_ids or self.target_names):
            raise ValueError("all mention cannot contain individual targets")


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
                ProviderResponseUncertainError,
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
    receipt_recallable: bool | None = None,
    **delivery_kwargs,
) -> IMDeliveryResult:
    """Deliver one already-pending ChatMessage and durably finalize its receipt."""
    from app.services.turn_runtime import (
        HISTORY_ONLY_CHANNELS,
        deliver_message_with_receipt,
    )

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
        lifecycle_owns_live_terminal = bool(
            meta.get("turn_terminal_published_by_lifecycle")
            and str(getattr(runtime, "source_channel", "") or "")
            in {"web", "miniprogram", "wechat_miniprogram", "mcp", *HISTORY_ONLY_CHANNELS}
        )
        await db.commit()

    async def _record_part(part: IMDeliveryPart) -> None:
        if receipt_recallable is not None:
            part = replace(part, recallable=receipt_recallable)
        await append_delivery_part(message_id, part)

    try:
        if lifecycle_owns_live_terminal:
            result = IMDeliveryResult.unsupported_delivery(
                str(getattr(runtime, "source_channel", "") or "web"),
                "websocket",
                conversation_ref=str(getattr(runtime, "conversation_id", "") or ""),
            )
        else:
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
    if receipt_recallable is not None:
        result = replace(
            result,
            parts=tuple(replace(part, recallable=receipt_recallable) for part in result.parts),
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
    turn_terminal_status: str = "completed",
    error_code: str | None = None,
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
    turn_snapshot = None
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
            if error_code:
                meta["error_code"] = error_code
            if meta.get("turn_anchor_id"):
                meta["turn_status"] = turn_terminal_status
                try:
                    turn_anchor_id = uuid.UUID(str(meta["turn_anchor_id"]))
                except (TypeError, ValueError):
                    turn_anchor_id = None
                if turn_anchor_id is not None:
                    from app.services.conversation_turn_lifecycle import transition_conversation_turn

                    turn_snapshot = await transition_conversation_turn(
                        db,
                        agent_id=agent_id,
                        conversation_id=row.conversation_id,
                        turn_anchor_id=turn_anchor_id,
                        status=turn_terminal_status,
                    )
            row.message_meta = meta
        await db.commit()
        conversation_id = row.conversation_id
    if turn_snapshot is not None:
        from app.services.conversation_turn_lifecycle import publish_conversation_turn_event

        await publish_conversation_turn_event(
            agent_id=agent_id,
            conversation_id=conversation_id,
            payload={
                "type": "done",
                "role": "assistant",
                "content": content,
                "message_id": str(local_id),
            },
            snapshot=turn_snapshot,
            event_kind="turn_terminal",
        )
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


async def _dingtalk_token(config: ChannelConfig) -> str:
    from app.services.dingtalk_service import get_dingtalk_access_token

    result = await get_dingtalk_access_token(config.app_id or "", config.app_secret or "")
    return str(result.get("access_token") or "")


from app.services import im_recall as _im_recall

PartRecallResult = _im_recall.PartRecallResult
RecallAdapter = _im_recall.RecallAdapter
_part_result = _im_recall._part_result
_dingtalk_failed_results = _im_recall._dingtalk_failed_results
_unsupported_recall = _im_recall._unsupported_recall
_run_recall_adapter = _im_recall._run_recall_adapter
_recall_feishu = _im_recall._recall_feishu
_recall_wecom_app = _im_recall._recall_wecom_app
_recall_slack = _im_recall._recall_slack
_recall_discord_gateway = _im_recall._recall_discord_gateway
_recall_teams = _im_recall._recall_teams
IM_RECALL_ADAPTERS = _im_recall.IM_RECALL_ADAPTERS
_channel_config_type = _im_recall._channel_config_type
_parse_iso = _im_recall._parse_iso

PartRecallResult.__module__ = __name__


async def _recall_dingtalk_oto(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    _im_recall._dingtalk_token = _dingtalk_token
    return await _im_recall._recall_dingtalk_oto(config, parts)


async def _recall_dingtalk_group(
    config: ChannelConfig,
    parts: list[dict[str, Any]],
) -> list[PartRecallResult]:
    _im_recall._dingtalk_token = _dingtalk_token
    return await _im_recall._recall_dingtalk_group(config, parts)


async def recall_message(
    *,
    agent_id: uuid.UUID,
    message_id: uuid.UUID | str,
    user_id: uuid.UUID | None,
    current_session_id: uuid.UUID | str | None,
) -> dict[str, Any]:
    _im_recall._dingtalk_token = _dingtalk_token
    return await _im_recall.recall_message(
        agent_id=agent_id,
        message_id=message_id,
        user_id=user_id,
        current_session_id=current_session_id,
    )
