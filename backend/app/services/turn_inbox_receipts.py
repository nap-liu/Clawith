"""Durable receipt anchor lifecycle for turn-inbox sessions."""

from __future__ import annotations

import asyncio
import json
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.turn_inbox_shared import (
    CHANNEL_RECEIPT_ANCHOR_KEY,
    CHANNEL_RECEIPT_CLEANUP_BATCH_SIZE,
    CHANNEL_RECEIPT_CLEANUP_DIAGNOSTIC_RING_SIZE,
    CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY,
    CHANNEL_RECEIPT_CLEANUP_MAX_ATTEMPTS,
    CHANNEL_RECEIPT_PROVIDER_META_KEY,
    CHANNEL_RECEIPT_STARTUP_MAX_BATCHES,
    _marker_cleanup_message_ids,
)


def durable_channel_receipt_anchor_id(session: ChatSession) -> uuid.UUID | None:
    """Return the last consumed IM message for the session's current generation.

    Reaction callback objects are process-local and intentionally cannot be
    reconstructed after restart. This marker is the durable proof of which
    inbound message was consumed last; generation/root validation prevents a
    prior turn's receipt anchor leaking into a newer lifecycle.
    """

    raw = dict(session.im_config or {}).get(CHANNEL_RECEIPT_ANCHOR_KEY)
    if not isinstance(raw, dict):
        return None
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    snapshot = conversation_turn_snapshot_for_session(session)
    if snapshot.anchor_id is None:
        return None
    try:
        message_id = uuid.UUID(str(raw.get("message_id") or ""))
        marker_root_id = uuid.UUID(str(raw.get("turn_anchor_id") or ""))
        marker_generation = int(raw.get("generation") or 0)
    except (TypeError, ValueError):
        return None
    if marker_root_id != snapshot.anchor_id or marker_generation != snapshot.generation:
        return None
    return message_id


def bind_durable_channel_receipt_anchor(
    session: ChatSession,
    message_id: uuid.UUID,
) -> bool:
    """Bind the initial consumed message to the active durable turn generation."""

    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    snapshot = conversation_turn_snapshot_for_session(session)
    if snapshot.anchor_id != message_id or snapshot.status != "running":
        return False
    prior_marker = dict(session.im_config or {}).get(CHANNEL_RECEIPT_ANCHOR_KEY)
    cleanup_message_ids = (
        _marker_cleanup_message_ids(prior_marker)
        if isinstance(prior_marker, dict)
        else []
    )
    if message_id not in cleanup_message_ids:
        cleanup_message_ids.append(message_id)
    session.im_config = {
        **dict(session.im_config or {}),
        CHANNEL_RECEIPT_ANCHOR_KEY: {
            "message_id": str(message_id),
            "turn_anchor_id": str(snapshot.anchor_id),
            "generation": snapshot.generation,
            "cleanup_message_ids": [
                str(cleanup_message_id)
                for cleanup_message_id in cleanup_message_ids
            ],
            "cleanup_attempts": (
                dict(prior_marker.get("cleanup_attempts") or {})
                if isinstance(prior_marker, dict)
                and isinstance(prior_marker.get("cleanup_attempts"), dict)
                else {}
            ),
        },
    }
    return True


async def load_durable_channel_receipt_anchor(
    *,
    session_id: str,
    agent_id: uuid.UUID,
) -> uuid.UUID | None:
    """Reload the canonical consumed receipt anchor without process memory."""

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None
    async with async_session() as db:
        session = await db.get(ChatSession, durable_session_id)
        if session is None or session.agent_id != agent_id:
            return None
        return durable_channel_receipt_anchor_id(session)


async def cleanup_durable_channel_receipt_anchor(
    *,
    session_id: str,
    agent_id: uuid.UUID,
    require_terminal: bool = False,
) -> bool:
    """Consume one durable marker through the owning transport adapter.

    Process-local callback objects are not reconstructed.  For DingTalk the
    persisted provider coordinates are enough to perform a bounded recall of
    the finite progress-reaction vocabulary. Failed references and per-message
    attempt state remain durable until their bounded retry budget is exhausted.
    Exhausted work is removed while a small count/recent-id diagnostic stays.
    """

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return False

    marker: dict | None = None
    provider_refs: dict[uuid.UUID, tuple[str, str]] = {}
    app_key = ""
    app_secret = ""
    async with async_session() as db:
        session = await db.get(ChatSession, durable_session_id)
        if (
            session is None
            or session.agent_id != agent_id
            or session.source_channel != "dingtalk"
            or durable_channel_receipt_anchor_id(session) is None
        ):
            return False
        if require_terminal:
            from app.services.conversation_turn_lifecycle import (
                TERMINAL_TURN_STATUSES,
                conversation_turn_snapshot_for_session,
            )

            if (
                conversation_turn_snapshot_for_session(session).status
                not in TERMINAL_TURN_STATUSES
            ):
                return False
        candidate = dict(session.im_config or {}).get(CHANNEL_RECEIPT_ANCHOR_KEY)
        if not isinstance(candidate, dict):
            return False
        marker = dict(candidate)
        message_ids = _marker_cleanup_message_ids(marker)
        cleanup_batch = message_ids[:CHANNEL_RECEIPT_CLEANUP_BATCH_SIZE]
        if cleanup_batch:
            rows = list(
                (
                    await db.execute(
                        select(ChatMessage).where(
                            ChatMessage.id.in_(cleanup_batch),
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == str(durable_session_id),
                        )
                    )
                ).scalars()
            )
            by_id = {row.id: row for row in rows}
            for message_id in cleanup_batch:
                row = by_id.get(message_id)
                meta = (
                    row.message_meta
                    if row is not None and isinstance(row.message_meta, dict)
                    else {}
                )
                receipt = meta.get(CHANNEL_RECEIPT_PROVIDER_META_KEY)
                if not isinstance(receipt, dict):
                    continue
                provider_message_id = str(receipt.get("provider_message_id") or "")
                provider_conversation_id = str(
                    receipt.get("provider_conversation_id") or ""
                )
                if provider_message_id and provider_conversation_id:
                    provider_refs[message_id] = (
                        provider_message_id,
                        provider_conversation_id,
                    )
        from app.models.channel_config import ChannelConfig

        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "dingtalk",
                )
            )
        ).scalar_one_or_none()
        if config is not None:
            app_key = str(config.app_id or "")
            app_secret = str(config.app_secret or "")

    accepted_ids: set[uuid.UUID] = set()
    if provider_refs and app_key and app_secret:
        from app.services.dingtalk_reaction import (
            cleanup_durable_progress_reactions,
        )

        provider_items = list(provider_refs.items())
        results = await asyncio.gather(
            *(
                cleanup_durable_progress_reactions(
                    app_key,
                    app_secret,
                    provider_message_id,
                    provider_conversation_id,
                )
                for _message_id, (
                    provider_message_id,
                    provider_conversation_id,
                ) in provider_items
            ),
            return_exceptions=True,
        )
        accepted_ids = {
            message_id
            for (message_id, _provider_ref), result in zip(
                provider_items,
                results,
                strict=True,
            )
            if result is True
        }

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.id == durable_session_id,
                    ChatSession.agent_id == agent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return False
        config = dict(session.im_config or {})
        current = config.get(CHANNEL_RECEIPT_ANCHOR_KEY)
        if current != marker:
            return False
        cleanup_ids = _marker_cleanup_message_ids(marker)
        raw_attempts = marker.get("cleanup_attempts")
        attempts = dict(raw_attempts) if isinstance(raw_attempts, dict) else {}
        diagnostics = config.get(CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY)
        diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
        exhausted_count = int(diagnostics.get("exhausted_count") or 0)
        recent_exhausted_ids = list(diagnostics.get("recent_message_ids") or [])
        remaining_ids: list[uuid.UUID] = []
        processed_ids = set(cleanup_ids[:CHANNEL_RECEIPT_CLEANUP_BATCH_SIZE])
        for message_id in cleanup_ids:
            key = str(message_id)
            if message_id in accepted_ids:
                attempts.pop(key, None)
                continue
            if message_id in processed_ids:
                count = int(attempts.get(key) or 0) + 1
                if count >= CHANNEL_RECEIPT_CLEANUP_MAX_ATTEMPTS:
                    attempts.pop(key, None)
                    exhausted_count += 1
                    recent_exhausted_ids = [
                        *[item for item in recent_exhausted_ids if item != key],
                        key,
                    ][-CHANNEL_RECEIPT_CLEANUP_DIAGNOSTIC_RING_SIZE:]
                    logger.warning(
                        "[turn_inbox] durable reaction cleanup exhausted after "
                        "bounded attempts session={} message={}",
                        durable_session_id,
                        message_id,
                    )
                    continue
                else:
                    attempts[key] = count
            remaining_ids.append(message_id)
        if exhausted_count:
            config[CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY] = {
                "exhausted_count": exhausted_count,
                "recent_message_ids": recent_exhausted_ids,
            }
        if not remaining_ids:
            config.pop(CHANNEL_RECEIPT_ANCHOR_KEY, None)
        else:
            config[CHANNEL_RECEIPT_ANCHOR_KEY] = {
                **marker,
                "cleanup_message_ids": [
                    str(message_id) for message_id in remaining_ids
                ],
                "cleanup_attempts": attempts,
            }
        session.im_config = config
        await db.commit()
    return bool(accepted_ids)


async def cleanup_stale_channel_receipt_anchors(*, limit: int = 50) -> int:
    """Round-robin terminal cleanup until clear, stalled, or globally capped."""

    from app.services.conversation_turn_lifecycle import (
        TERMINAL_TURN_STATUSES,
        TURN_SESSION_KEY,
    )

    async with async_session() as db:
        sessions = list(
            (
                await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.source_channel == "dingtalk",
                        ChatSession.im_config[CHANNEL_RECEIPT_ANCHOR_KEY].is_not(None),
                        ChatSession.im_config[TURN_SESSION_KEY]["status"]
                        .as_string()
                        .in_(TERMINAL_TURN_STATUSES),
                    )
                    .order_by(ChatSession.last_message_at.desc().nullslast())
                    .limit(limit)
                )
            ).scalars()
        )
        targets = [(session.id, session.agent_id) for session in sessions]
        states = {
            (session.id, session.agent_id): json.dumps(
                dict(session.im_config or {}).get(CHANNEL_RECEIPT_ANCHOR_KEY),
                sort_keys=True,
                separators=(",", ":"),
            )
            for session in sessions
        }

    active = list(targets)
    successful_targets: set[tuple[uuid.UUID, uuid.UUID]] = set()
    work_count = 0
    while active and work_count < CHANNEL_RECEIPT_STARTUP_MAX_BATCHES:
        batch = active[
            : CHANNEL_RECEIPT_STARTUP_MAX_BATCHES - work_count
        ]
        results = await asyncio.gather(
            *(
                cleanup_durable_channel_receipt_anchor(
                    session_id=str(session_id),
                    agent_id=agent_id,
                    require_terminal=True,
                )
                for session_id, agent_id in batch
            ),
            return_exceptions=True,
        )
        work_count += len(batch)
        for target, result in zip(batch, results, strict=True):
            if result is True:
                successful_targets.add(target)

        async with async_session() as db:
            refreshed = list(
                (
                    await db.execute(
                        select(ChatSession).where(
                            ChatSession.id.in_([target[0] for target in batch])
                        )
                    )
                ).scalars()
            )
        refreshed_by_target = {
            (session.id, session.agent_id): session for session in refreshed
        }
        next_active: list[tuple[uuid.UUID, uuid.UUID]] = []
        for target in batch:
            session = refreshed_by_target.get(target)
            marker = (
                dict(session.im_config or {}).get(CHANNEL_RECEIPT_ANCHOR_KEY)
                if session is not None
                else None
            )
            if not isinstance(marker, dict):
                continue
            next_state = json.dumps(
                marker,
                sort_keys=True,
                separators=(",", ":"),
            )
            if next_state == states[target]:
                continue
            states[target] = next_state
            next_active.append(target)
        active = next_active if len(batch) == len(active) else []
    return len(successful_targets)


async def _acknowledge_live_receipt_handoff(
    *,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    turn_anchor_id: uuid.UUID,
    generation: int,
    message_id: uuid.UUID,
) -> None:
    """Drop prior anchors only after their live cleanup hook completed."""

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == agent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return
        config = dict(session.im_config or {})
        marker = config.get(CHANNEL_RECEIPT_ANCHOR_KEY)
        if not isinstance(marker, dict) or (
            str(marker.get("message_id") or "") != str(message_id)
            or str(marker.get("turn_anchor_id") or "") != str(turn_anchor_id)
            or int(marker.get("generation") or 0) != generation
        ):
            return
        config[CHANNEL_RECEIPT_ANCHOR_KEY] = {
            **marker,
            "cleanup_message_ids": [str(message_id)],
            "cleanup_attempts": {},
        }
        session.im_config = config
        await db.commit()


async def acknowledge_durable_channel_receipt_cleanup(
    *,
    session_id: str,
    agent_id: uuid.UUID,
    message_id: uuid.UUID,
) -> bool:
    """Remove exactly one provider anchor after its live controller recalled it."""

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.id == durable_session_id,
                    ChatSession.agent_id == agent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return False
        config = dict(session.im_config or {})
        marker = config.get(CHANNEL_RECEIPT_ANCHOR_KEY)
        if not isinstance(marker, dict):
            return False
        cleanup_ids = [
            candidate
            for candidate in _marker_cleanup_message_ids(marker)
            if candidate != message_id
        ]
        attempts = (
            dict(marker.get("cleanup_attempts") or {})
            if isinstance(marker.get("cleanup_attempts"), dict)
            else {}
        )
        attempts.pop(str(message_id), None)
        if cleanup_ids:
            config[CHANNEL_RECEIPT_ANCHOR_KEY] = {
                **marker,
                "cleanup_message_ids": [str(candidate) for candidate in cleanup_ids],
                "cleanup_attempts": attempts,
            }
        else:
            config.pop(CHANNEL_RECEIPT_ANCHOR_KEY, None)
        session.im_config = config
        await db.commit()
        return bool(cleanup_ids)
