"""Startup-time recovery for turns interrupted by a backend restart."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select

from app.database import async_session
from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE, Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.channel_llm import _call_agent_llm
from app.services.chat_history import (
    is_incomplete_delivery_progress,
    load_recoverable_history_for_turn,
    load_recoverable_messages_for_turn,
    persist_assistant_reply_row,
    persist_tool_call_row,
)
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.llm.tool_output_store import finalize_tool_output
from app.services.redis_lease_lock import RedisLeaseBusyError, RedisLeaseLock
from app.services.turn_runtime import deliver_recovered_reply_to_origin, load_turn_runtime
from app.services.workload_capacity import WorkloadKind, get_workload_capacity

DEFAULT_RECOVERY_MAX_AGE_HOURS = 2.0
STARTUP_RECOVERY_LEASE_RESOURCE = "startup-turn-recovery"


@dataclass
class RecoveryStats:
    scanned: int = 0
    resumed: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _RecoveryOrigin:
    session_found: bool
    source_channel: str | None
    external_conv_id: str | None


def _metadata_execution_agent_id(anchor: ChatMessage) -> uuid.UUID:
    meta = anchor.message_meta if isinstance(anchor.message_meta, dict) else {}
    try:
        return uuid.UUID(str(meta.get("execution_agent_id")))
    except (TypeError, ValueError):
        return anchor.agent_id


async def _validated_execution_agent_id(
    db,
    anchor: ChatMessage,
) -> uuid.UUID | None:
    """Resolve an execution Agent only from a validated Subagent edge."""
    candidate = _metadata_execution_agent_id(anchor)
    if candidate == anchor.agent_id:
        return candidate
    meta = anchor.message_meta if isinstance(anchor.message_meta, dict) else {}
    if meta.get("kind") != "subagent_event":
        logger.warning(f"[turn_recovery] ignored execution_agent_id on non-subagent anchor={anchor.id}")
        return anchor.agent_id

    try:
        parent_id = uuid.UUID(str(anchor.conversation_id))
        child_id = uuid.UUID(str(meta.get("subagent_id")))
    except (TypeError, ValueError):
        return None

    from app.models.subagent_run import SubagentRun

    parent = await db.get(ChatSession, parent_id)
    child = await db.get(ChatSession, child_id)
    run = await db.get(SubagentRun, child_id)
    storage_agent = await db.get(Agent, anchor.agent_id)
    execution_agent = await db.get(Agent, candidate)
    if (
        parent is None
        or child is None
        or run is None
        or storage_agent is None
        or execution_agent is None
        or parent.agent_id != anchor.agent_id
        or run.parent_session_id != parent.id
        or child.source_channel != "subagent"
        or child.agent_id != candidate
        or anchor.sender_agent_id != candidate
        or run.execution_user_id != anchor.user_id
        or storage_agent.tenant_id != execution_agent.tenant_id
        or not (
            parent.agent_id == candidate or (parent.source_channel == "agent" and parent.peer_agent_id == candidate)
        )
    ):
        logger.error(
            f"[turn_recovery] rejected invalid subagent execution edge anchor={anchor.id} candidate={candidate}"
        )
        return None
    return candidate


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
    *,
    turn_anchor_id: uuid.UUID,
) -> list[tuple[ChatMessage, dict, str]]:
    anchor_idx = next(
        (idx for idx, row in enumerate(rows) if row.id == turn_anchor_id),
        None,
    )
    if anchor_idx is None:
        return []

    done_keys: set[str] = set()
    candidates: list[tuple[ChatMessage, dict, str]] = []
    for row in rows[anchor_idx + 1 :]:
        if getattr(row, "role", None) != "tool_call":
            continue
        meta = row.message_meta if isinstance(row.message_meta, dict) else {}
        row_anchor_id = str(meta.get("turn_anchor_id") or "")
        if row_anchor_id and row_anchor_id != str(turn_anchor_id):
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


async def _complete_unfinished_tool_calls(
    db,
    anchor: ChatMessage,
    *,
    ctx_size: int,
    expected_origin: _RecoveryOrigin,
    execution_agent_id: uuid.UUID | None = None,
    release_db_before_execution: bool = False,
) -> int:
    execution_agent_id = execution_agent_id or anchor.agent_id
    rows = await load_recoverable_messages_for_turn(
        db,
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        turn_anchor_id=anchor.id,
        ctx_size=ctx_size,
    )
    unfinished = _unfinished_tool_call_rows(rows, turn_anchor_id=anchor.id)
    if release_db_before_execution:
        # Tool execution can wait on subprocesses and remote services. The
        # rows above are immutable inputs, so the recovery session must not
        # retain a pooled connection while those tools run.
        await db.close()
    completed = 0
    for _row, payload, key in unfinished:
        if not await _recovery_origin_matches(anchor, expected_origin):
            return completed
        name = str(payload.get("name") or payload.get("tool_name") or "")
        if not name:
            continue
        args = payload.get("args")
        if args is None:
            args = payload.get("arguments")
        if args is None:
            args = {}
        # The original turn snapshot is not persisted. Recovery must not load a
        # new list: that could grant an old code block tools enabled only after
        # its turn began. Without the exact snapshot, execute_code_aio runs but
        # receives no toolscall launcher.
        raw_result = await execute_tool(
            name,
            args,
            agent_id=execution_agent_id,
            user_id=anchor.user_id,
            session_id=anchor.conversation_id,
            tool_call_id=key,
            turn_anchor_id=anchor.id,
            on_output=None,
        )
        result_text = str(raw_result)
        llm_view = finalize_tool_output(
            result_text,
            tool_name=name,
            agent_id=execution_agent_id,
            session_id=anchor.conversation_id,
            tool_call_id=key,
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
                turn_anchor_id=anchor.id,
            )
            await done_db.commit()
        completed += 1
    return completed


def _turn_status(row: ChatMessage) -> str:
    meta = row.message_meta if isinstance(row.message_meta, dict) else {}
    return str(meta.get("turn_status") or "")


async def _load_recovery_origin(
    db,
    anchor: ChatMessage,
    *,
    for_update: bool = False,
) -> _RecoveryOrigin | None:
    """Load the cancellation and session-generation boundary for a turn."""
    try:
        session_id = uuid.UUID(str(anchor.conversation_id))
    except (TypeError, ValueError):
        session_id = None
    session = await db.get(ChatSession, session_id, with_for_update=for_update) if session_id else None
    fresh_anchor = await db.get(
        ChatMessage,
        anchor.id,
        with_for_update=for_update,
    )
    if fresh_anchor is None or _turn_status(fresh_anchor) == "cancelled":
        return None
    if session is None:
        return _RecoveryOrigin(False, None, None)
    external_conv_id = session.external_conv_id
    if external_conv_id and "__archived_" in external_conv_id:
        return None
    return _RecoveryOrigin(
        True,
        session.source_channel,
        external_conv_id,
    )


async def _load_fresh_recovery_origin(anchor: ChatMessage) -> _RecoveryOrigin | None:
    async with async_session() as db:
        return await _load_recovery_origin(db, anchor)


async def _recovery_origin_matches(
    anchor: ChatMessage,
    expected: _RecoveryOrigin,
) -> bool:
    return await _load_fresh_recovery_origin(anchor) == expected


async def prepare_recoverable_turn_history(
    db,
    anchor: ChatMessage,
    *,
    execution_agent_id: uuid.UUID,
    ctx_size: int,
) -> list[dict]:
    """Finish an interrupted durable tool tail and rebuild its exact history.

    Subagent workers share the normal startup recovery semantics but retain
    ownership of their own lifecycle transition and parent notification.
    """
    expected_origin = await _load_recovery_origin(db, anchor)
    if expected_origin is None:
        return []
    await _complete_unfinished_tool_calls(
        db,
        anchor,
        ctx_size=ctx_size,
        expected_origin=expected_origin,
        execution_agent_id=execution_agent_id,
    )
    if not await _recovery_origin_matches(anchor, expected_origin):
        return []
    return await load_recoverable_history_for_turn(
        db,
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        turn_anchor_id=anchor.id,
        ctx_size=ctx_size,
    )


async def _deliver_recovered_reply(
    anchor: ChatMessage,
    *,
    expected_origin: _RecoveryOrigin,
    reply: str,
    execution_agent_id: uuid.UUID,
    message_id: uuid.UUID | str | None = None,
) -> bool:
    """Validate the turn generation and deliver while its rows stay locked."""
    async with async_session() as db:
        current_origin = await _load_recovery_origin(
            db,
            anchor,
            for_update=True,
        )
        if current_origin != expected_origin:
            return False

        delivery_kwargs = {
            "agent_id": execution_agent_id,
            "conversation_id": anchor.conversation_id,
            "reply": reply,
            "message_id": message_id,
        }
        if expected_origin.session_found:
            delivery_kwargs.update(
                {
                    "expected_source_channel": expected_origin.source_channel,
                    "expected_external_conv_id": expected_origin.external_conv_id,
                    "validate_external_conv_id": True,
                }
            )
        return await deliver_recovered_reply_to_origin(**delivery_kwargs)


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
        if await _load_recovery_origin(db, anchor) is None:
            continue
        anchors.append(anchor)
        if len(anchors) >= limit:
            break
    return anchors


async def _latest_row_needs_recovery(db, row: ChatMessage) -> bool:
    try:
        session = await db.get(ChatSession, uuid.UUID(str(row.conversation_id)))
    except (TypeError, ValueError):
        session = None
    if session is not None and session.source_channel == "subagent":
        return False
    meta = row.message_meta if isinstance(getattr(row, "message_meta", None), dict) else {}
    if meta.get("consumed_by_onmessage") or meta.get("kind") == "on_message_event":
        # TriggerExecution owns these durable event turns and has its own lease
        # reclaim path.  Startup recovery must not race it and create a second
        # LLM invocation for the same event.
        return False
    if row.role == "assistant":
        if is_incomplete_delivery_progress(row):
            return True
        # Persisted assistant output is the durable completion boundary. Without a
        # separate delivery receipt, startup cannot distinguish "persisted before
        # send" from "already sent"; retrying here duplicates every recent IM reply
        # on each restart. Recover only turns that stopped before assistant output.
        return False
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
        if payload and payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME and payload.get("status") == "pending":
            return True
    return False


async def startup_turn_resume_once(*, limit: int = 50) -> RecoveryStats:
    """Resume startup-recoverable turn anchors once.

    All app instances may pass through this gate. A renewable Redis lease keeps
    one replica responsible for a recovery batch without reserving a PostgreSQL
    connection while model turns execute. Callers may run this synchronously or
    as a detached startup background task.
    """
    stats = RecoveryStats()
    try:
        async with RedisLeaseLock(
            STARTUP_RECOVERY_LEASE_RESOURCE,
            namespace="turn-recovery",
        ):
            # Scanning is a short read transaction. Close it before admission,
            # model execution, tool execution, or inter-turn waits.
            async with async_session() as db:
                anchors = await _load_recoverable_anchors(db, limit=limit)
            stats.scanned = len(anchors)
            for anchor in anchors:
                try:
                    # Each recovered anchor is an independently cancellable
                    # logical turn. Keeping them on the startup scanner task
                    # would make one stop request cancel the entire batch.
                    did_resume = await asyncio.create_task(resume_turn(anchor))
                except asyncio.CancelledError:
                    scanner_task = asyncio.current_task()
                    if scanner_task is not None and scanner_task.cancelling():
                        raise
                    stats.skipped += 1
                    logger.info(
                        "[turn_recovery] recovery cancelled for anchor={}; continuing batch",
                        anchor.id,
                    )
                    continue
                except Exception as exc:
                    stats.failed += 1
                    logger.exception(f"[turn_recovery] failed to resume anchor={anchor.id}: {exc}")
                    continue
                if did_resume:
                    stats.resumed += 1
                else:
                    stats.skipped += 1
    except RedisLeaseBusyError:
        logger.info("[turn_recovery] another replica owns startup recovery")
    return stats


async def resume_turn(anchor: ChatMessage) -> bool:
    """Resume one inferred incomplete user turn via the normal channel LLM path."""

    expected_origin = await _load_fresh_recovery_origin(anchor)
    if expected_origin is None:
        return False

    # Resolve admission identity in a short transaction. Capacity waiting must
    # never reserve a database connection.
    async with async_session() as db:
        execution_agent_id = await _validated_execution_agent_id(db, anchor)
        if execution_agent_id is None:
            return False
        agent = (await db.execute(select(Agent).where(Agent.id == execution_agent_id))).scalar_one_or_none()
        if agent is None or getattr(agent, "agent_type", None) == "openclaw":
            return False
        tenant_id = agent.tenant_id
        ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE

    async with get_workload_capacity().slot(WorkloadKind.BACKGROUND, tenant_id):
        if not await _recovery_origin_matches(anchor, expected_origin):
            return False

        async with async_session() as db:
            if await _tail_has_pending_confirmation(db, anchor, ctx_size=ctx_size):
                return False
            await _complete_unfinished_tool_calls(
                db,
                anchor,
                ctx_size=ctx_size,
                expected_origin=expected_origin,
                execution_agent_id=execution_agent_id,
                release_db_before_execution=True,
            )

        if not await _recovery_origin_matches(anchor, expected_origin):
            return False

        async with async_session() as db:
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
                # Defensive guard for direct callers and races after anchor selection:
                # a persisted assistant means this turn is already complete.
                return False

            reply = await _call_agent_llm(
                db,
                execution_agent_id,
                "",
                session_id=anchor.conversation_id,
                user_id=anchor.user_id,
                history=history,
                recovery_hint=None,
                continue_turn=True,
                recovery_mode=True,
                turn_anchor_id=anchor.id,
                storage_agent_id=anchor.agent_id,
                turn_type="recovery",
                release_db_before_dispatch=True,
            )

        if reply and reply.strip():
            if not await _recovery_origin_matches(anchor, expected_origin):
                return False
            from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

            runtime = await load_turn_runtime(
                agent_id=anchor.agent_id,
                conversation_id=anchor.conversation_id,
            )
            async with async_session() as db:
                assistant_message_id = await persist_assistant_reply_row(
                    db,
                    agent_id=anchor.agent_id,
                    user_id=anchor.user_id,
                    conversation_id=anchor.conversation_id,
                    content=reply,
                    message_meta=attach_delivery_to_meta(
                        {},
                        IMDeliveryResult.pending(runtime.source_channel),
                    ),
                    turn_anchor_id=anchor.id,
                    sender_agent_id=execution_agent_id,
                )
                await db.commit()
            delivered = await _deliver_recovered_reply(
                anchor,
                expected_origin=expected_origin,
                reply=reply,
                execution_agent_id=execution_agent_id,
                message_id=assistant_message_id,
            )
            if not delivered:
                logger.warning(f"[turn_recovery] final reply delivery pending anchor={anchor.id}")
                return False
            return True

        return False
