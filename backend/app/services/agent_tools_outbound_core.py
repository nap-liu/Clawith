"""Durable outbound-message operation and receipt primitives."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.im_delivery import (
    IMDeliveryPart,
    IMDeliveryResult,
    attach_delivery_to_meta,
)
from app.services.user_output import sanitize_user_visible_text

MEDIA_DELIVERY_MAX_IN_FLIGHT = 4

_outbound_media_slots = asyncio.Semaphore(MEDIA_DELIVERY_MAX_IN_FLIGHT)
_outbound_media_connection: ContextVar = ContextVar(
    "outbound_media_connection", default=None
)


def _build_outbound_operation_key(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    tool_call_id: str | None,
    origin_turn_anchor_id: uuid.UUID | str | None = None,
) -> str | None:
    """Return the durable replay key for one messaging tool invocation."""
    if not tool_call_id:
        return None
    session_scope = str(origin_session_id or "no-session")
    turn_scope = str(origin_turn_anchor_id or "unanchored")
    return f"outbound:{agent_id}:{session_scope}:{turn_scope}:{tool_call_id}"[:500]


async def _lock_outbound_operation(db, operation_key: str | None) -> None:
    """Serialize one internal send operation even before its receipt exists."""
    if not operation_key or db.get_bind().dialect.name != "postgresql":
        return
    from sqlalchemy import text as sa_text

    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _outbound_operation_lock_id(operation_key)},
    )


def _outbound_operation_lock_id(operation_key: str) -> int:
    import hashlib

    return int.from_bytes(
        hashlib.blake2b(operation_key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


@asynccontextmanager
async def _outbound_operation_lifecycle_lock(operation_key: str | None):
    """Bound media concurrency before reserving one main-pool connection."""
    if not operation_key or engine.dialect.name != "postgresql":
        yield
        return
    async with _outbound_media_slots:
        async with _locked_outbound_media_connection(operation_key):
            yield


@asynccontextmanager
async def _locked_outbound_media_connection(operation_key: str):
    """Hold one crash-safe advisory lock across an outbound provider call."""
    from sqlalchemy import text as sa_text

    async with engine.connect() as connection:
        lock_id = _outbound_operation_lock_id(operation_key)
        lock_acquired = False
        token = None
        try:
            try:
                await connection.execute(
                    sa_text("SELECT pg_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
            except BaseException:
                # The server may have acquired a session lock before the
                # client observed cancellation/failure. Close the physical
                # connection so no uncertain lock can return to the pool.
                await connection.invalidate()
                raise
            lock_acquired = True
            await connection.commit()
            token = _outbound_media_connection.set(connection)
            yield
        finally:
            if token is not None:
                _outbound_media_connection.reset(token)
            if lock_acquired:
                try:
                    if connection.in_transaction():
                        await connection.rollback()
                    await connection.execute(
                        sa_text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": lock_id},
                    )
                    await connection.commit()
                except BaseException:
                    # A physical disconnect also releases PostgreSQL session locks;
                    # never return a possibly locked connection to the pool.
                    await connection.invalidate()
                    raise


@asynccontextmanager
async def _outbound_media_db_session():
    """Use the lifecycle-lock connection without holding a long DB transaction."""
    connection = _outbound_media_connection.get()
    if connection is None:
        async with async_session() as db:
            yield db
        return
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(bind=connection, expire_on_commit=False) as db:
        yield db


async def _find_outbound_tool_receipt(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    tool_call_id: str | None,
    origin_turn_anchor_id: uuid.UUID | str | None = None,
) -> ChatMessage | None:
    """Look up a previously persisted send result before replaying a provider call."""
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return None
    async with async_session() as db:
        return (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == operation_key))
        ).scalar_one_or_none()


async def _persist_outbound_channel_message(
    db,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session: ChatSession,
    content: str,
    source_channel: str,
    actor_ref: str,
    target_name: str,
    origin_session_id: str | None,
    origin_source_channel: str | None,
    tool_call_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    external_message_id: str | None = None,
    delivery_status: str = "sent",
    delivery_result: IMDeliveryResult | None = None,
) -> tuple[ChatMessage, bool]:
    """Atomically claim one outbound operation and return its send ownership."""
    content = sanitize_user_visible_text(content or "").strip()
    if not content:
        raise ValueError("user-visible message must be non-empty after sanitization")
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if operation_key:
        await _lock_outbound_operation(db, operation_key)
        existing = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == operation_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    if origin_session_id and not origin_source_channel:
        try:
            origin = await db.get(ChatSession, uuid.UUID(str(origin_session_id)))
        except (TypeError, ValueError):
            origin = None
        if origin is not None:
            origin_source_channel = origin.source_channel

    message_meta = {
        "direction": "outbound",
        "source_channel": source_channel,
        "actor_ref": str(actor_ref),
        "target_user_id": str(user_id or ""),
        "target_session_id": str(session.id),
        "target_name": str(target_name),
        "origin_session_id": str(origin_session_id or ""),
        "origin_source_channel": str(origin_source_channel or ""),
        "tool_call_id": str(tool_call_id or ""),
        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
        "external_message_id": str(external_message_id or ""),
        "delivery_status": delivery_status,
    }
    if delivery_result is None:
        transport_by_channel = {
            "dingtalk": "dingtalk_openapi_oto",
            "feishu": "feishu_message",
            "wecom": "wecom_app",
            "slack": "slack",
            "discord": "discord_gateway",
            "teams": "microsoft_teams",
            "microsoft_teams": "microsoft_teams",
            "whatsapp": "whatsapp_cloud",
            "wechat": "wechat_ilink",
            "web": "websocket",
            "miniprogram": "websocket",
            "wechat_miniprogram": "websocket",
        }
        transport = transport_by_channel.get(source_channel, "websocket")
        recallable_transports = {
            "dingtalk_openapi_oto",
            "feishu_message",
            "wecom_app",
            "slack",
            "discord_gateway",
            "microsoft_teams",
        }
        delivery_result = IMDeliveryResult.sent(
            source_channel,
            IMDeliveryPart(
                transport=transport,
                provider_message_id=external_message_id,
                conversation_ref=str(actor_ref),
                recallable=bool(external_message_id) and transport in recallable_transports,
            ),
        )
    message_meta = attach_delivery_to_meta(message_meta, delivery_result)

    row = ChatMessage(
        agent_id=agent_id,
        user_id=user_id,
        role="assistant",
        content=content,
        conversation_id=str(session.id),
        external_event_key=operation_key,
        message_meta=message_meta,
    )
    db.add(row)
    session.last_message_at = datetime.now(timezone.utc)
    await db.flush()
    return row, True


def _duplicate_outbound_claim_result(receipt: ChatMessage) -> str:
    return (
        "⏳ This delivery is already claimed and will not be sent twice.\n"
        f"message_id: {receipt.id}"
    )


_SESSION_MESSAGE_CAPABILITIES = {
    "web": frozenset({"person"}),
    "miniprogram": frozenset({"person"}),
    "wechat_miniprogram": frozenset({"person"}),
    "dingtalk": frozenset({"person", "group"}),
    "feishu": frozenset({"person", "group"}),
    "wecom": frozenset({"person", "group"}),
    "slack": frozenset({"person", "group"}),
    "teams": frozenset({"person", "group"}),
    "microsoft_teams": frozenset({"person", "group"}),
    "discord": frozenset({"person", "group"}),
    "whatsapp": frozenset({"person"}),
    "wechat": frozenset({"person"}),
}
_LEGACY_GROUP_SESSION_CHANNELS = frozenset({"dingtalk", "feishu", "wecom", "slack", "teams", "microsoft_teams"})
_PLATFORM_SESSION_CHANNELS = frozenset({"web", "miniprogram", "wechat_miniprogram"})
_SESSION_MESSAGE_DENIAL = "❌ 无法投递：该会话不存在，或不属于当前数字员工。"
_GROUP_SESSION_DENIAL = "❌ 无法投递：该群会话不存在，或不属于当前数字员工。"


def _session_message_result(
    *,
    status: str,
    session_id: str,
    channel: str,
    target_name: str,
    is_group: bool,
    legacy_group_contract: bool,
    mentioned_users: list[str] | None = None,
    mentions: dict | None = None,
    message_id: str | None = None,
) -> str:
    if legacy_group_contract:
        payload = {
            "status": status,
            "session_id": session_id,
            "channel": channel,
            "group_name": target_name,
        }
    else:
        payload = {
            "status": status,
            "session_id": session_id,
            "channel": channel,
            "conversation_type": "group" if is_group else "person",
            "conversation_name": target_name,
        }
    if mentioned_users:
        payload["mentioned_users"] = mentioned_users
    if mentions:
        payload["mentions"] = mentions
    if message_id:
        payload["message_id"] = message_id
    return json.dumps(payload, ensure_ascii=False)


def _session_receipt_replay_status(receipt: ChatMessage) -> str:
    meta = receipt.message_meta if isinstance(receipt.message_meta, dict) else {}
    delivery = meta.get("delivery") if isinstance(meta.get("delivery"), dict) else {}
    status = str(delivery.get("status") or meta.get("delivery_status") or "unknown")
    if status == "sent":
        return "already_sent"
    if status in {"pending", "failed", "unknown", "partial"}:
        return status
    return "unknown"

__all__ = [name for name in globals() if not name.startswith("__")]
