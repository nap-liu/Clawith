"""Startup-time recovery for turns interrupted by a backend restart."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select, text

from app.database import async_session
from app.models.agent import Agent, DEFAULT_CONTEXT_WINDOW_SIZE
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.channel_llm import _call_agent_llm
from app.services.chat_history import (
    load_recoverable_messages_for_turn,
    load_recoverable_history_for_turn,
    persist_tool_call_row,
    persist_assistant_reply_row,
)
from app.services.turn_runtime import deliver_recovered_reply_to_origin
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.llm.tool_output_store import finalize_tool_output


RECOVERY_ADVISORY_LOCK_KEY = 2026070801
DEFAULT_RECOVERY_MAX_AGE_HOURS = 2.0
DEFAULT_DELIVERY_RETRY_MAX_AGE_MINUTES = 10.0
ASSISTANT_TAIL_REDELIVERY_CHANNELS = frozenset({"dingtalk"})
RECOVERY_AUTO_REEXECUTE_TOOL_NAMES = frozenset({
    "list_files",
    "read_file",
    "search_files",
    "find_files",
    "list_focus_items",
    "list_triggers",
    "web_search",
    "search_contacts",
})


@dataclass
class RecoveryStats:
    scanned: int = 0
    resumed: int = 0
    skipped: int = 0
    failed: int = 0


def _recovery_max_age_hours() -> float:
    raw = os.environ.get("TURN_RECOVERY_MAX_AGE_HOURS")
    if raw is None or raw.strip() == "":
        return DEFAULT_RECOVERY_MAX_AGE_HOURS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"[turn_recovery] invalid TURN_RECOVERY_MAX_AGE_HOURS={raw!r}; using default")
        return DEFAULT_RECOVERY_MAX_AGE_HOURS
    return max(value, 0.0)


def _delivery_retry_max_age_minutes() -> float:
    raw = os.environ.get("TURN_RECOVERY_DELIVERY_MAX_AGE_MINUTES")
    if raw is None or raw.strip() == "":
        return DEFAULT_DELIVERY_RETRY_MAX_AGE_MINUTES
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            f"[turn_recovery] invalid TURN_RECOVERY_DELIVERY_MAX_AGE_MINUTES={raw!r}; using default"
        )
        return DEFAULT_DELIVERY_RETRY_MAX_AGE_MINUTES
    return max(value, 0.0)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def execute_tool(*args, **kwargs):
    from app.services.agent_tools import execute_tool as _execute_tool

    return await _execute_tool(*args, **kwargs)


def _tool_payload(row: ChatMessage) -> dict | None:
    try:
        payload = json.loads(row.content or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _tool_call_key(row: ChatMessage, payload: dict) -> str:
    return str(payload.get("call_id") or payload.get("tool_call_id") or row.id)


def _unfinished_tool_call_rows(
    rows: list[ChatMessage],
) -> list[tuple[ChatMessage, dict, str]]:
    done_keys: set[str] = set()
    candidates: list[tuple[ChatMessage, dict, str]] = []
    for row in rows:
        if getattr(row, "role", None) != "tool_call":
            continue
        payload = _tool_payload(row)
        if not payload:
            continue
        status = payload.get("status")
        key = _tool_call_key(row, payload)
        if status == "done":
            done_keys.add(key)
            continue
        if status not in {"running", "pending"}:
            continue
        if payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME:
            continue
        candidates.append((row, payload, key))
    return [(row, payload, key) for row, payload, key in candidates if key not in done_keys]


async def _complete_unfinished_tool_calls(db, anchor: ChatMessage, *, ctx_size: int) -> int:
    rows = await load_recoverable_messages_for_turn(
        db,
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        turn_anchor_id=anchor.id,
        ctx_size=ctx_size,
    )
    unfinished = _unfinished_tool_call_rows(rows)
    completed = 0
    for _row, payload, key in unfinished:
        name = str(payload.get("name") or payload.get("tool_name") or "")
        if not name:
            continue
        args = payload.get("args")
        if args is None:
            args = payload.get("arguments")
        if args is None:
            args = {}
        if name in RECOVERY_AUTO_REEXECUTE_TOOL_NAMES:
            raw_result = await execute_tool(
                name,
                args,
                agent_id=anchor.agent_id,
                user_id=anchor.user_id,
                session_id=anchor.conversation_id,
                on_output=None,
            )
            result_text = str(raw_result)
            llm_view = finalize_tool_output(
                result_text,
                tool_name=name,
                agent_id=anchor.agent_id,
                session_id=anchor.conversation_id,
                tool_call_id=key,
            )
        else:
            llm_view = (
                "Tool execution was interrupted by a service restart and was not "
                "automatically re-run because it may have external side effects. "
                "Explain this to the user and ask them to retry or confirm the action."
            )
        async with async_session() as done_db:
            await persist_tool_call_row(
                done_db,
                agent_id=anchor.agent_id,
                user_id=anchor.user_id,
                conversation_id=anchor.conversation_id,
                evt={
                    "name": name,
                    "call_id": key,
                    "args": args,
                    "status": "done",
                    "result": llm_view,
                    "reasoning_content": payload.get("reasoning_content"),
                },
            )
            await done_db.commit()
        completed += 1
    return completed


async def _load_recoverable_anchors(db, *, limit: int) -> list[ChatMessage]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_recovery_max_age_hours())
    latest = (
        select(
            ChatMessage.id.label("id"),
            func.row_number()
            .over(
                partition_by=(ChatMessage.agent_id, ChatMessage.conversation_id),
                order_by=(ChatMessage.created_at.desc(), ChatMessage.id.desc()),
            )
            .label("rn"),
        )
        .where(
            ChatMessage.compacted_into.is_(None),
            ChatMessage.created_at >= cutoff,
        )
        .subquery()
    )
    result = await db.execute(
        select(ChatMessage)
        .join(latest, ChatMessage.id == latest.c.id)
        .where(latest.c.rn == 1)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(limit * 4)
    )
    anchors: list[ChatMessage] = []
    for latest_row in result.scalars().all():
        if not await _latest_row_needs_recovery(db, latest_row):
            continue
        anchor = await _find_turn_anchor_for_latest(db, latest_row)
        if anchor is None:
            continue
        anchors.append(anchor)
        if len(anchors) >= limit:
            break
    return anchors


async def _source_channel_for_row(db, row: ChatMessage) -> str:
    try:
        session_id = uuid.UUID(str(row.conversation_id))
    except (TypeError, ValueError):
        return "web"

    session = (
        await db.execute(
            select(ChatSession.source_channel).where(
                ChatSession.id == session_id,
                ChatSession.agent_id == row.agent_id,
            )
        )
    ).scalar_one_or_none()
    return str(session or "web")


async def _latest_row_needs_recovery(db, row: ChatMessage) -> bool:
    if row.role == "assistant":
        source_channel = await _source_channel_for_row(db, row)
        if source_channel not in ASSISTANT_TAIL_REDELIVERY_CHANNELS:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_delivery_retry_max_age_minutes())
        return _as_aware_utc(row.created_at) >= cutoff
    if row.role == "user":
        return True
    if row.role != "tool_call":
        return False
    payload = _tool_payload(row)
    if payload is None:
        return False
    if payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME and payload.get("status") == "pending":
        return False
    return payload.get("status") in {"running", "done", "pending"}


async def _find_turn_anchor_for_latest(db, latest_row: ChatMessage) -> ChatMessage | None:
    if latest_row.role == "user":
        return latest_row
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == latest_row.agent_id,
            ChatMessage.conversation_id == latest_row.conversation_id,
            ChatMessage.role == "user",
            ChatMessage.compacted_into.is_(None),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _tail_has_pending_confirmation(db, anchor: ChatMessage, *, ctx_size: int) -> bool:
    rows = await load_recoverable_messages_for_turn(
        db,
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        turn_anchor_id=anchor.id,
        ctx_size=ctx_size,
    )
    for row in rows:
        if getattr(row, "role", None) != "tool_call":
            continue
        payload = _tool_payload(row)
        if (
            payload
            and payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME
            and payload.get("status") == "pending"
        ):
            return True
    return False


async def startup_turn_resume_once(*, limit: int = 50) -> RecoveryStats:
    """Resume startup-recoverable turn anchors once before background loops start.

    All app instances should pass through this gate. The advisory lock makes
    non-winning instances wait until the winner finishes recovery, preventing
    trigger/connectors from racing with resumed turns.
    """
    stats = RecoveryStats()
    async with async_session() as db:
        await db.execute(text("SELECT pg_advisory_lock(:key)"), {"key": RECOVERY_ADVISORY_LOCK_KEY})
        try:
            anchors = await _load_recoverable_anchors(db, limit=limit)
            stats.scanned = len(anchors)
            for anchor in anchors:
                try:
                    did_resume = await resume_turn(anchor)
                except Exception as exc:
                    stats.failed += 1
                    logger.exception(f"[turn_recovery] failed to resume anchor={anchor.id}: {exc}")
                    continue
                if did_resume:
                    stats.resumed += 1
                else:
                    stats.skipped += 1
        finally:
            await db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RECOVERY_ADVISORY_LOCK_KEY})
    return stats


async def resume_turn(anchor: ChatMessage) -> bool:
    """Resume one inferred incomplete user turn via the normal channel LLM path."""

    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == anchor.agent_id))).scalar_one_or_none()
        if agent is None or getattr(agent, "agent_type", None) == "openclaw":
            return False
        ctx_size = (agent.context_window_size if agent else None) or DEFAULT_CONTEXT_WINDOW_SIZE
        if await _tail_has_pending_confirmation(db, anchor, ctx_size=ctx_size):
            return False
        await _complete_unfinished_tool_calls(db, anchor, ctx_size=ctx_size)
        history = await load_recoverable_history_for_turn(
            db,
            agent_id=anchor.agent_id,
            conversation_id=anchor.conversation_id,
            turn_anchor_id=anchor.id,
            ctx_size=ctx_size,
        )
        if not history:
            return False
        last_role = history[-1].get("role")
        if last_role == "assistant":
            reply = str(history[-1].get("content") or "")
            delivered = await deliver_recovered_reply_to_origin(
                agent_id=anchor.agent_id,
                conversation_id=anchor.conversation_id,
                reply=reply,
            )
            if not delivered:
                logger.warning(f"[turn_recovery] final reply delivery pending anchor={anchor.id}")
                return False
            return True

        reply = await _call_agent_llm(
            db,
            anchor.agent_id,
            "",
            session_id=anchor.conversation_id,
            user_id=anchor.user_id,
            history=history,
            recovery_hint=None,
            continue_turn=True,
            recovery_mode=True,
            turn_anchor_id=anchor.id,
        )

    if reply and reply.strip():
        async with async_session() as db:
            await persist_assistant_reply_row(
                db,
                agent_id=anchor.agent_id,
                user_id=anchor.user_id,
                conversation_id=anchor.conversation_id,
                content=reply,
            )
            await db.commit()
        delivered = await deliver_recovered_reply_to_origin(
            agent_id=anchor.agent_id,
            conversation_id=anchor.conversation_id,
            reply=reply,
        )
        if not delivered:
            logger.warning(f"[turn_recovery] final reply delivery pending anchor={anchor.id}")
            return False
        return True

    return False
