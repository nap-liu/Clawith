"""Durable reply preparation for external-channel control commands."""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession


async def _lock_command_event(db: AsyncSession, event_key: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(
        hashlib.blake2b(event_key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )


async def _load_command_replay(
    db: AsyncSession,
    event_key: str,
) -> dict | None:
    existing = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.external_event_key == event_key)
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
    return {
        "action": str(meta.get("command_action") or "replayed"),
        "message": existing.content,
        "message_id": str(existing.id),
        "external_event_key": event_key,
        "conversation_id": str(existing.conversation_id),
        "user_id": str(existing.user_id) if existing.user_id else None,
        "should_deliver": False,
        "replayed": True,
    }


async def prepare_channel_command_reply(
    db: AsyncSession,
    *,
    command: str,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    external_user_id: str | None,
    external_conv_id: str,
    source_channel: str,
    provider_event_id: str | None = None,
    is_group: bool = False,
    group_name: str | None = None,
    external_user_info: dict | None = None,
) -> dict:
    """Execute a command and durably prepare its normalized reply anchor."""
    from app.services.channel_commands import (
        _load_agent,
        _load_channel_session,
        handle_channel_command,
    )

    if not provider_event_id:
        raise ValueError("provider_event_id is required for channel commands")
    event_digest = hashlib.sha256(str(provider_event_id).encode("utf-8")).hexdigest()
    event_key = f"channel-command:{source_channel}:{agent_id}:{event_digest}"
    await _lock_command_event(db, event_key)
    replay = await _load_command_replay(db, event_key)
    if replay is not None:
        return replay

    agent = await _load_agent(db, agent_id=agent_id)
    resolved_user_id = user_id
    if resolved_user_id is None and agent is not None and external_user_id:
        from app.services.channel_user_service import channel_user_service

        platform_user = await channel_user_service.resolve_channel_user(
            db=db,
            agent=agent,
            channel_type=source_channel,
            external_user_id=external_user_id,
            extra_info=dict(external_user_info or {}),
        )
        resolved_user_id = platform_user.id

    # Release identity locks before session locks, then reacquire the event lock
    # and replay check so concurrent replicas retain exactly-once behavior.
    if db.in_transaction():
        await db.commit()
    await _lock_command_event(db, event_key)
    replay = await _load_command_replay(db, event_key)
    if replay is not None:
        return replay

    result = await handle_channel_command(
        db=db,
        command=command,
        agent_id=agent_id,
        user_id=resolved_user_id,
        external_conv_id=external_conv_id,
        source_channel=source_channel,
        is_group=is_group,
        group_name=group_name,
    )
    from app.services.user_output import sanitize_user_visible_text

    result["message"] = sanitize_user_visible_text(result["message"]).strip()
    if not result["message"]:
        result["message"] = "命令已处理。"

    session = None
    delivery_session_id = result.pop("_delivery_session_id", None)
    if delivery_session_id:
        session = await db.get(ChatSession, uuid.UUID(delivery_session_id))
    if session is None:
        session = await _load_channel_session(
            db,
            agent_id=agent_id,
            external_conv_id=external_conv_id,
            source_channel=source_channel,
            for_update=True,
        )
    if session is None:
        from app.services.channel_session import find_or_create_channel_session

        event_fragment = hashlib.sha256(
            str(provider_event_id).encode("utf-8")
        ).hexdigest()[:20]
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=resolved_user_id,
            external_conv_id=f"{external_conv_id}__control_{event_fragment}",
            source_channel=source_channel,
            first_message_title=result["message"][:40],
            is_group=is_group,
            group_name=group_name,
            allow_unresolved_user=True,
        )

    from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

    reply = ChatMessage(
        agent_id=agent_id,
        user_id=resolved_user_id,
        role="assistant",
        conversation_id=str(session.id),
        content=result["message"],
        external_event_key=event_key,
        message_meta=attach_delivery_to_meta(
            {
                "artifact_role": "command_reply",
                "source_channel": source_channel,
                "command_action": str(result.get("action") or "unknown"),
                **({"turn_control_only": True} if result.get("action", "").startswith("continue_") else {}),
                "provider_event_id_hash": event_digest,
            },
            IMDeliveryResult.pending(source_channel),
        ),
    )
    db.add(reply)
    await db.flush()
    session.last_message_at = datetime.now(UTC)
    continue_anchor_id = result.pop("_continue_anchor_id", None)
    if continue_anchor_id:
        from app.services.turn_continue import dispatch_continue

        await db.commit()
        await dispatch_continue(continue_anchor_id)
    return {
        **result,
        "message_id": str(reply.id),
        "external_event_key": event_key,
        "conversation_id": str(session.id),
        "user_id": str(resolved_user_id) if resolved_user_id else None,
        "should_deliver": True,
        "replayed": False,
    }
