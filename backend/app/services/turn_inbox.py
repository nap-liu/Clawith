"""Durable follow-up inbox shared by every external IM transport."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

TURN_INBOX_MAX_MESSAGES = 20
TURN_INBOX_MAX_BYTES = 24 * 1024
CHANNEL_RECEIPT_ANCHOR_KEY = "channel_receipt_anchor"
CHANNEL_RECEIPT_PROVIDER_META_KEY = "channel_receipt"
CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY = "channel_receipt_cleanup_diagnostics"
CHANNEL_RECEIPT_CLEANUP_BATCH_SIZE = 8
CHANNEL_RECEIPT_CLEANUP_MAX_ATTEMPTS = 3
CHANNEL_RECEIPT_CLEANUP_DIAGNOSTIC_RING_SIZE = 8
CHANNEL_RECEIPT_STARTUP_MAX_BATCHES = 512
TURN_INBOX_CHANNELS = frozenset(
    {
        "agent",
        "dingtalk",
        "feishu",
        "wecom",
        "wechat",
        "slack",
        "discord",
        "teams",
        "microsoft_teams",
        "whatsapp",
    }
)


def is_turn_inbox_channel(source_channel: str | None) -> bool:
    return str(source_channel or "").lower() in TURN_INBOX_CHANNELS


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


def _marker_cleanup_message_ids(marker: dict) -> list[uuid.UUID]:
    raw_cleanup_ids = marker.get("cleanup_message_ids")
    raw_ids = (
        list(raw_cleanup_ids)
        if isinstance(raw_cleanup_ids, list)
        else [marker.get("message_id")]
    )
    parsed: list[uuid.UUID] = []
    for raw_id in raw_ids:
        try:
            message_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
        if message_id not in parsed:
            parsed.append(message_id)
    return parsed


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
        # A partial final batch means the global cap is exhausted. Otherwise
        # every selected session advances once per round, preventing starvation.
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


async def _resume_promoted_turn(anchor: ChatMessage) -> None:
    """Own one promoted anchor with bounded event-local retry, never polling."""

    from app.services.redis_lease_lock import RedisLeaseBusyError, RedisLeaseLock
    from app.services.turn_recovery import resume_turn

    last_error: Exception | None = None
    for delay in (0, 0.25, 1, 4, 10):
        if delay:
            await asyncio.sleep(delay)
        resume_owner = False
        try:
            async with RedisLeaseLock(
                str(anchor.id),
                namespace="turn-inbox-resume",
            ):
                resume_owner = True
                if await resume_turn(anchor):
                    return
                raise RuntimeError("promoted turn was not accepted by recovery")
        except RedisLeaseBusyError as exc:
            if not resume_owner:
                # Another replica owns the exact recovery anchor.
                return
            # This owner reached resume_turn but the shared conversation lease
            # is still draining. Retry locally instead of waiting for restart.
            last_error = exc
            logger.warning(
                "[turn_inbox] conversation lease busy during resume "
                "anchor={} delay={}",
                anchor.id,
                delay,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[turn_inbox] promoted turn retry anchor={} delay={}: {}",
                anchor.id,
                delay,
                exc,
            )
    await _fail_exhausted_promoted_turn(anchor, last_error)


def schedule_durable_turn_resume(anchor: ChatMessage) -> None:
    """Resume any admitted durable turn after its foreground owner exits."""

    asyncio.create_task(
        _resume_promoted_turn(anchor),
        name=f"durable-turn-resume:{anchor.id}",
    )


async def _fail_exhausted_promoted_turn(
    anchor: ChatMessage,
    error: Exception | None,
) -> None:
    """Release an exhausted promoted owner instead of leaving it running."""

    async with async_session() as db:
        session = await db.get(
            ChatSession,
            uuid.UUID(str(anchor.conversation_id)),
            with_for_update=True,
        )
        if session is None or session.agent_id != anchor.agent_id:
            return
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
            transition_conversation_turn,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.status != "running" or snapshot.anchor_id != anchor.id:
            return
        failed = await transition_conversation_turn(
            db,
            agent_id=anchor.agent_id,
            conversation_id=anchor.conversation_id,
            turn_anchor_id=anchor.id,
            status="failed",
        )
        next_anchor_id = await promote_next_turn_inbox(
            db,
            session=session,
        )
        await db.commit()

    from app.services.conversation_turn_lifecycle import (
        publish_conversation_turn_event,
    )

    await publish_conversation_turn_event(
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        payload={
            "type": "done",
            "role": "assistant",
            "content": "",
            "error": str(error or "promoted turn recovery exhausted")[:200],
        },
        snapshot=failed,
        event_kind="turn_terminal",
    )
    if next_anchor_id is not None:
        await kick_promoted_turn_inbox(
            agent_id=anchor.agent_id,
            session_id=anchor.conversation_id,
        )


async def promote_next_turn_inbox(
    db,
    *,
    session: ChatSession,
) -> uuid.UUID | None:
    """Atomically admit the oldest deferred input after one turn terminates."""

    root = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.message_meta["turn_inbox_state"].as_string()
                == "pending",
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if root is None:
        return None
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    promoted = await transition_conversation_turn(
        db,
        agent_id=session.agent_id,
        conversation_id=str(session.id),
        turn_anchor_id=root.id,
        status="running",
    )
    root.message_meta = {
        **dict(root.message_meta or {}),
        "turn_inbox_state": "promoted",
        "turn_inbox_anchor_id": str(root.id),
        "turn_inbox_generation": promoted.generation,
        "turn_inbox_mode": "current_turn",
    }
    await db.flush()
    return root.id


async def kick_promoted_turn_inbox(
    *,
    agent_id: uuid.UUID,
    session_id: str,
) -> bool:
    """Start a committed promoted input; startup recovery is the durable fallback."""

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        session = await db.get(ChatSession, durable_session_id)
        if session is None or session.agent_id != agent_id:
            return False
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.status != "running" or snapshot.anchor_id is None:
            return False
        anchor = await db.get(ChatMessage, snapshot.anchor_id)
        if anchor is None or dict(anchor.message_meta or {}).get("turn_inbox_state") != "promoted":
            return False

    schedule_durable_turn_resume(anchor)
    return True


async def drain_turn_inbox(
    *,
    session_id: str,
    active_turn_anchor_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    before_injection=None,
) -> list[dict]:
    """Claim a bounded FIFO batch at one ordinary LLM round boundary."""

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return []

    async with async_session() as db:
        session = await db.get(ChatSession, durable_session_id, with_for_update=True)
        if session is None or session.agent_id != execution_agent_id:
            return []
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if (
            snapshot.status != "running"
            or snapshot.anchor_id != active_turn_anchor_id
        ):
            raise asyncio.CancelledError

        candidates = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(session.id),
                        ChatMessage.message_meta["turn_inbox_state"].as_string()
                        == "pending",
                        ChatMessage.message_meta["turn_inbox_mode"].as_string()
                        == "current_turn",
                        ChatMessage.user_id == execution_user_id,
                        ChatMessage.message_meta["turn_inbox_anchor_id"].as_string()
                        == str(active_turn_anchor_id),
                        ChatMessage.message_meta["turn_inbox_generation"].as_integer()
                        == snapshot.generation,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .limit(TURN_INBOX_MAX_MESSAGES * 4)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        selected: list[ChatMessage] = []
        total_bytes = 0
        for row in candidates:
            meta = dict(row.message_meta or {})
            content = str(row.content or "")
            if session.is_group:
                from app.services.sender_attribution import wrap_with_sender

                content = wrap_with_sender(
                    content,
                    row.user_id,
                    meta.get("sender_display_name") or meta.get("sender_nickname"),
                )
            size = len(content.encode("utf-8")) + 256
            if selected and (
                len(selected) >= TURN_INBOX_MAX_MESSAGES
                or total_bytes + size > TURN_INBOX_MAX_BYTES
            ):
                break
            selected.append(row)
            total_bytes += min(size, TURN_INBOX_MAX_BYTES)

        if selected and before_injection is not None:
            # The provider may already have emitted a visible assistant segment.
            # Commit it before these later inputs transition to delivered so a
            # restart always observes the same causal order.
            earliest_injection = min(row.created_at for row in selected)
            await before_injection(
                created_at=earliest_injection - timedelta(microseconds=1)
            )

        injected: list[dict] = []
        for row in selected:
            meta = dict(row.message_meta or {})
            content = str(row.content or "")
            if session.is_group:
                from app.services.sender_attribution import wrap_with_sender

                content = wrap_with_sender(
                    content,
                    row.user_id,
                    meta.get("sender_display_name") or meta.get("sender_nickname"),
                )
            message = {"role": "user", "content": content}
            attachments = list(meta.get("attachments") or [])
            if attachments:
                message["attachments"] = attachments
            injected.append(message)
            row.message_meta = {**meta, "turn_inbox_state": "delivered"}
        if selected:
            if session.source_channel == "dingtalk":
                prior_marker = dict(session.im_config or {}).get(
                    CHANNEL_RECEIPT_ANCHOR_KEY
                )
                cleanup_message_ids = (
                    _marker_cleanup_message_ids(prior_marker)
                    if isinstance(prior_marker, dict)
                    else []
                )
                for row in selected:
                    if row.id not in cleanup_message_ids:
                        cleanup_message_ids.append(row.id)
                session.im_config = {
                    **dict(session.im_config or {}),
                    CHANNEL_RECEIPT_ANCHOR_KEY: {
                        "message_id": str(selected[-1].id),
                        "turn_anchor_id": str(active_turn_anchor_id),
                        "generation": snapshot.generation,
                        "cleanup_message_ids": [
                            str(message_id) for message_id in cleanup_message_ids
                        ],
                        "cleanup_attempts": (
                            dict(prior_marker.get("cleanup_attempts") or {})
                            if isinstance(prior_marker, dict)
                            and isinstance(
                                prior_marker.get("cleanup_attempts"), dict
                            )
                            else {}
                        ),
                    },
                }
            await db.commit()
            from app.services.channel_dispatch import advance_channel_receipt_anchor

            previous_cleanup_completed = await advance_channel_receipt_anchor(
                [row.id for row in selected]
            )
            if previous_cleanup_completed and session.source_channel == "dingtalk":
                await _acknowledge_live_receipt_handoff(
                    session_id=durable_session_id,
                    agent_id=execution_agent_id,
                    turn_anchor_id=active_turn_anchor_id,
                    generation=snapshot.generation,
                    message_id=selected[-1].id,
                )
        return injected
