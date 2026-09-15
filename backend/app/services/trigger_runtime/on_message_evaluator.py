"""Deterministic on-message matching and recovery evaluation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import and_, func, or_, select

from app.database import async_session
from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.cron_schedule import (
    compute_next_trigger_cron_occurrence,
)

MIN_POLL_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class OnMessageMatchResult:
    matched: bool = False
    consumed: bool = False
    execution_ids: tuple[uuid.UUID, ...] = ()
    trigger_ids: tuple[uuid.UUID, ...] = ()


def _canonical_trigger_actor(config: dict) -> tuple[str, uuid.UUID] | None:
    """Return the exactly-one canonical actor frozen in an on_message trigger."""
    raw_user_id = str(config.get("from_user_id") or "").strip()
    raw_agent_id = str(config.get("from_agent_id") or "").strip()
    if bool(raw_user_id) == bool(raw_agent_id):
        return None
    try:
        if raw_user_id:
            return "user", uuid.UUID(raw_user_id)
        return "agent", uuid.UUID(raw_agent_id)
    except ValueError:
        return None


def _canonical_message_actor(message) -> tuple[str, uuid.UUID] | None:
    if getattr(message, "sender_user_id", None):
        return "user", message.sender_user_id
    if getattr(message, "sender_agent_id", None):
        return "agent", message.sender_agent_id
    return None


async def match_incoming_chat_message(db, message, session) -> OnMessageMatchResult:
    """Atomically enqueue the exact on_message binding for one durable row.

    Channel/Web entry points call this after flushing the inbound ChatMessage and
    before running the remote session's LLM turn. Human messages may enqueue an
    observer execution but always continue through the ordinary conversation.
    The daemon poll remains a recovery fallback for the send→set_trigger gap.
    """
    from app.models.audit import ChatMessage
    from app.models.trigger import AgentTrigger
    from app.models.trigger_execution import TriggerExecution
    from app.services.trigger_runtime.dispatch import runtime_execution_payload
    from app.services.trigger_runtime.keys import build_scheduled_execution_key
    from app.services.trigger_runtime.queue import enqueue_trigger_execution

    if not isinstance(message, ChatMessage) or message.role not in {"user", "assistant"}:
        return OnMessageMatchResult()

    result = await db.execute(
        select(AgentTrigger)
        .where(
            AgentTrigger.type == "on_message",
            AgentTrigger.is_enabled.is_(True),
            AgentTrigger.config["_watch_session_id"].as_string() == str(session.id),
        )
        .order_by(AgentTrigger.created_at.asc())
        .with_for_update()
    )
    candidates = result.scalars().all()
    queued_by_trigger: dict[uuid.UUID, int] = {}
    if candidates:
        queued_result = await db.execute(
            select(TriggerExecution.trigger_id, func.count(TriggerExecution.id))
            .where(
                TriggerExecution.trigger_id.in_([trigger.id for trigger in candidates]),
                # Processing executions have already incremented fire_count at
                # first claim. Only pending rows consume an additional slot.
                TriggerExecution.status == "pending",
            )
            .group_by(TriggerExecution.trigger_id)
        )
        queued_by_trigger = {
            trigger_id: int(count or 0) for trigger_id, count in queued_result.all()
        }
    matches: list[AgentTrigger] = []
    meta = message.message_meta if isinstance(message.message_meta, dict) else {}
    actual_actor_pair = _canonical_message_actor(message)
    actual_actor = str(actual_actor_pair[1]) if actual_actor_pair else ""

    for trigger in candidates:
        cfg = trigger.config or {}
        if trigger.expires_at and datetime.now(timezone.utc) >= trigger.expires_at:
            continue
        if (
            trigger.max_fires is not None
            and (trigger.fire_count or 0) + queued_by_trigger.get(trigger.id, 0)
            >= trigger.max_fires
        ):
            continue
        expected_channel = str(cfg.get("_watch_source_channel") or "").strip()
        if expected_channel and expected_channel != session.source_channel:
            continue
        expected_actor_pair = _canonical_trigger_actor(cfg)
        if expected_actor_pair is None or actual_actor_pair != expected_actor_pair:
            continue
        if session.source_channel != "agent" and message.role != "user":
            continue
        anchor_id = cfg.get("_outbound_message_id")
        if anchor_id:
            try:
                anchor = await db.get(ChatMessage, uuid.UUID(str(anchor_id)))
            except (TypeError, ValueError):
                anchor = None
            if anchor is None or anchor.conversation_id != message.conversation_id:
                continue
            if message.id == anchor.id:
                continue
            if anchor.created_at and message.created_at and message.created_at < anchor.created_at:
                continue
        if str(cfg.get("_correlation_mode") or "session_event") == "reply_to":
            expected_reply = str(cfg.get("_outbound_external_message_id") or "").strip()
            actual_reply = str(meta.get("reply_to_external_message_id") or "").strip()
            if not expected_reply or actual_reply != expected_reply:
                continue
        matches.append(trigger)

    if not matches:
        return OnMessageMatchResult()
    execution_ids: list[uuid.UUID] = []
    matched_trigger_ids: list[uuid.UUID] = []
    consume_requested = False
    for trigger in matches:
        cfg = trigger.config or {}
        runtime_cfg = {
            **cfg,
            "_matched_message": (message.content or ""),
            "_matched_from": actual_actor or "message",
            "_matched_message_id": str(message.id),
            "_matched_session_id": str(session.id),
            "_trigger_context": cfg.get("_set_trigger_context")
            or {
                "name": trigger.name,
                "type": trigger.type,
                "reason": trigger.reason,
                "focus_ref": trigger.focus_ref or "",
                "config": {
                    key: value for key, value in cfg.items() if not str(key).startswith("_")
                },
            },
        }
        if message.external_event_key:
            runtime_cfg["_matched_external_event_key"] = message.external_event_key
        runtime_trigger = AgentTrigger(
            id=trigger.id,
            agent_id=trigger.agent_id,
            name=trigger.name,
            type=trigger.type,
            config=runtime_cfg,
            reason=trigger.reason,
            focus_ref=trigger.focus_ref,
            is_enabled=trigger.is_enabled,
            last_fired_at=trigger.last_fired_at,
            fire_count=trigger.fire_count,
            max_fires=trigger.max_fires,
            cooldown_seconds=trigger.cooldown_seconds,
            is_system=trigger.is_system,
            created_at=trigger.created_at,
            expires_at=trigger.expires_at,
        )
        execution, created = await enqueue_trigger_execution(
            db,
            trigger=trigger,
            source="on_message",
            idempotency_key=build_scheduled_execution_key(runtime_trigger, datetime.now(timezone.utc)),
            payload_obj=runtime_execution_payload(runtime_trigger),
            commit=False,
        )
        consume_requested = consume_requested or bool(
            cfg.get("_consume_remote")
            and actual_actor_pair
            and actual_actor_pair[0] == "agent"
        )
        if created and execution is not None:
            execution_ids.append(execution.id)
            matched_trigger_ids.append(trigger.id)

    if consume_requested:
        existing_trigger_ids = {
            str(value) for value in (meta.get("onmessage_trigger_ids") or [])
        }
        existing_execution_ids = {
            str(value) for value in (meta.get("onmessage_execution_ids") or [])
        }
        message.message_meta = {
            **meta,
            "consumed_by_onmessage": True,
            "onmessage_trigger_ids": sorted(
                existing_trigger_ids | {str(trigger.id) for trigger in matches}
            ),
            "onmessage_execution_ids": sorted(
                existing_execution_ids | {str(execution_id) for execution_id in execution_ids}
            ),
        }
    return OnMessageMatchResult(
        matched=bool(execution_ids),
        consumed=consume_requested,
        execution_ids=tuple(execution_ids),
        trigger_ids=tuple(matched_trigger_ids),
    )


async def recover_exact_on_message_events(
    trigger: AgentTrigger,
    *,
    page_size: int = 200,
) -> int:
    """Backfill every durable event missed around send→set_trigger arming.

    Live ingress is authoritative for new events.  This scanner closes only the
    race where one or more replies committed before the trigger row became
    visible.  It deliberately scans from the arm-time/outbound cursor on every
    pass and relies on the DB ``(trigger, message.id)`` execution key for
    idempotency; ``last_fired_at`` is a processing timestamp, never an event
    cursor.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    cfg = trigger.config if isinstance(trigger.config, dict) else {}
    watch_session_id = str(cfg.get("_watch_session_id") or "").strip()
    if not watch_session_id:
        return 0
    try:
        watch_uuid = uuid.UUID(watch_session_id)
    except (TypeError, ValueError):
        return 0

    created_count = 0
    async with async_session() as db:
        watch_session = await db.get(ChatSession, watch_uuid)
        owns_watch = watch_session is not None and (
            watch_session.agent_id == trigger.agent_id
            or (
                watch_session.source_channel == "agent"
                and trigger.agent_id in {watch_session.agent_id, watch_session.peer_agent_id}
            )
        )
        if not owns_watch:
            return 0
        expected_channel = str(cfg.get("_watch_source_channel") or "").strip()
        if expected_channel and expected_channel != watch_session.source_channel:
            return 0

        since = trigger.created_at or datetime.now(timezone.utc)
        since_raw = cfg.get("_since_ts")
        if since_raw:
            try:
                parsed_since = datetime.fromisoformat(str(since_raw))
                if parsed_since.tzinfo is None:
                    parsed_since = parsed_since.replace(tzinfo=timezone.utc)
                since = parsed_since
            except (TypeError, ValueError):
                pass
        anchor_id = cfg.get("_outbound_message_id")
        if anchor_id:
            try:
                anchor = await db.get(ChatMessage, uuid.UUID(str(anchor_id)))
            except (TypeError, ValueError):
                anchor = None
            if anchor is not None and anchor.conversation_id == watch_session_id and anchor.created_at:
                since = max(since, anchor.created_at)

        offset = 0
        while True:
            rows = list(
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == watch_session_id,
                            ChatMessage.role.in_(
                                ["user", "assistant"]
                                if watch_session.source_channel == "agent"
                                else ["user"]
                            ),
                            ChatMessage.created_at >= since,
                        )
                        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                        .offset(offset)
                        .limit(page_size)
                    )
                ).scalars()
            )
            if not rows:
                break
            for row in rows:
                matched = await match_incoming_chat_message(db, row, watch_session)
                if trigger.id in matched.trigger_ids:
                    created_count += 1
            offset += len(rows)
            if len(rows) < page_size:
                break
        await db.commit()
    return created_count


async def recover_legacy_on_message_events(
    trigger: AgentTrigger,
    *,
    page_size: int = 200,
) -> int:
    """Recover exact canonical-ID on_message events outside a watched session."""
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.trigger_execution import TriggerExecution
    from app.services.trigger_runtime.dispatch import runtime_execution_payload
    from app.services.trigger_runtime.keys import build_scheduled_execution_key
    from app.services.trigger_runtime.queue import enqueue_trigger_execution

    created_count = 0
    async with async_session() as db:
        locked_trigger = (
            await db.execute(
                select(AgentTrigger)
                .where(AgentTrigger.id == trigger.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            locked_trigger is None
            or not locked_trigger.is_enabled
            or locked_trigger.type != "on_message"
        ):
            return 0
        trigger = locked_trigger
        cfg = trigger.config if isinstance(trigger.config, dict) else {}
        if cfg.get("_watch_session_id"):
            return 0
        actor = _canonical_trigger_actor(cfg)
        if actor is None:
            return 0
        actor_type, actor_id = actor

        # Event time and processing time are independent. A message can commit
        # after a scan but before an earlier execution is claimed; using
        # last_fired_at would then jump past that message forever. Always replay
        # from the stable subscription cursor and let the (trigger, message) key
        # remove already-enqueued rows.
        since = trigger.created_at or datetime.now(timezone.utc)
        if cfg.get("_since_ts"):
            try:
                since = datetime.fromisoformat(str(cfg["_since_ts"]))
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        legacy_floor_raw = cfg.get("_legacy_scan_floor")
        if legacy_floor_raw:
            try:
                legacy_floor = datetime.fromisoformat(str(legacy_floor_raw))
                if legacy_floor.tzinfo is None:
                    legacy_floor = legacy_floor.replace(tzinfo=timezone.utc)
                since = max(since, legacy_floor)
            except (TypeError, ValueError):
                pass

        queued_count = (
            await db.execute(
                select(func.count(TriggerExecution.id)).where(
                    TriggerExecution.trigger_id == trigger.id,
                    # Processing rows are already reflected in trigger.fire_count.
                    TriggerExecution.status == "pending",
                )
            )
        ).scalar_one()
        remaining = None
        if trigger.max_fires is not None:
            remaining = max(
                0,
                trigger.max_fires - (trigger.fire_count or 0) - int(queued_count or 0),
            )
            if remaining == 0:
                return 0

        from sqlalchemy import String as SaString, cast as sa_cast

        base_query = None
        matched_from = str(actor_id)
        if actor_type == "agent":
            base_query = (
                select(ChatMessage)
                .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                .where(
                    or_(
                        ChatSession.agent_id == trigger.agent_id,
                        and_(
                            ChatSession.source_channel == "agent",
                            ChatSession.peer_agent_id == trigger.agent_id,
                        ),
                    ),
                    ChatMessage.sender_agent_id == actor_id,
                    ChatMessage.created_at >= since,
                    ChatMessage.role.in_(["assistant", "user"]),
                    ChatSession.source_channel != "trigger",
                )
            )
        else:
            user_filters = [
                ChatSession.agent_id == trigger.agent_id,
                ChatSession.source_channel != "trigger",
                ChatMessage.role == "user",
                ChatMessage.created_at >= since,
                ChatMessage.sender_user_id == actor_id,
            ]
            base_query = (
                select(ChatMessage)
                .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                .where(*user_filters)
            )

        last_created_at = None
        last_id = None
        while remaining is None or remaining > 0:
            query = base_query
            if last_created_at is not None and last_id is not None:
                query = query.where(
                    or_(
                        ChatMessage.created_at > last_created_at,
                        and_(
                            ChatMessage.created_at == last_created_at,
                            ChatMessage.id > last_id,
                        ),
                    )
                )
            limit = page_size if remaining is None else min(page_size, remaining)
            rows = list(
                (
                    await db.execute(
                        query.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc()).limit(limit)
                    )
                ).scalars()
            )
            if not rows:
                break
            for message in rows:
                runtime_cfg = {
                    **cfg,
                    "_matched_message": (message.content or ""),
                    "_matched_from": matched_from,
                    "_matched_message_id": str(message.id),
                    "_matched_session_id": str(message.conversation_id),
                    "_trigger_context": cfg.get("_set_trigger_context")
                    or {
                        "name": trigger.name,
                        "type": trigger.type,
                        "reason": trigger.reason,
                        "focus_ref": trigger.focus_ref or "",
                        "config": {
                            key: value
                            for key, value in cfg.items()
                            if not str(key).startswith("_")
                        },
                    },
                }
                if message.external_event_key:
                    runtime_cfg["_matched_external_event_key"] = message.external_event_key
                runtime_trigger = AgentTrigger(
                    id=trigger.id,
                    agent_id=trigger.agent_id,
                    name=trigger.name,
                    type=trigger.type,
                    config=runtime_cfg,
                    reason=trigger.reason,
                    focus_ref=trigger.focus_ref,
                    is_enabled=trigger.is_enabled,
                    last_fired_at=trigger.last_fired_at,
                    fire_count=trigger.fire_count,
                    max_fires=trigger.max_fires,
                    cooldown_seconds=trigger.cooldown_seconds,
                    is_system=trigger.is_system,
                    created_at=trigger.created_at,
                    expires_at=trigger.expires_at,
                )
                _execution, created = await enqueue_trigger_execution(
                    db,
                    trigger=trigger,
                    source="on_message",
                    idempotency_key=build_scheduled_execution_key(
                        runtime_trigger,
                        datetime.now(timezone.utc),
                    ),
                    payload_obj=runtime_execution_payload(runtime_trigger),
                    commit=False,
                )
                if created:
                    created_count += 1
                    if remaining is not None:
                        remaining -= 1
            last_created_at = rows[-1].created_at
            last_id = rows[-1].id
            if len(rows) < limit:
                break
        await db.commit()
    return created_count
