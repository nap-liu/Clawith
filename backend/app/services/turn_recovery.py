"""Startup-time recovery for turns interrupted by a backend restart."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.database import async_session
from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE, Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.channel_dispatch import ChannelReactions, run_channel_reaction_hook
from app.services.channel_llm import _call_agent_llm
from app.services.channel_reaction_recovery import (
    load_recovered_channel_reactions,
)
from app.services.chat_history import (
    is_incomplete_delivery_progress,
    load_recoverable_history_for_turn,
    load_recoverable_messages_for_turn,
    persist_assistant_reply_row,
    persist_tool_call_row,
    rewrite_tool_call_done_results,
)
from app.services.conversation_turn_lifecycle import (
    ACTIVE_TURN_STATUS,
    SUSPENDED_TURN_STATUS,
    TERMINAL_TURN_STATUSES,
    ConversationTurnConflict,
    ConversationTurnSnapshot,
    conversation_turn_snapshot_for_session,
    transition_conversation_turn,
)
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.llm.tool_output_store import finalize_tool_output
from app.services.redis_lease_lock import RedisLeaseBusyError, RedisLeaseLock
from app.services.turn_runtime import deliver_recovered_reply_to_origin, load_turn_runtime
from app.services.workload_capacity import WorkloadKind, get_workload_capacity

DEFAULT_RECOVERY_MAX_AGE_HOURS = 2.0
STARTUP_RECOVERY_LEASE_RESOURCE = "startup-turn-recovery"
RECOVERY_TOOL_MATERIALIZE_TIMEOUT_SECONDS = 60.0


@dataclass
class RecoveryStats:
    scanned: int = 0
    resumed: int = 0
    skipped: int = 0
    failed: int = 0


class _RecoveryFenceLost(RuntimeError):
    """The durable owner or route changed while recovery was running."""


@dataclass(frozen=True)
class _RecoveryOrigin:
    session_found: bool
    source_channel: str | None
    external_conv_id: str | None
    turn_anchor_id: uuid.UUID | None = None
    turn_generation: int = 0


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
    if meta.get("source_channel") == "agent":
        try:
            session_id = uuid.UUID(str(anchor.conversation_id))
        except (TypeError, ValueError):
            return None
        session = await db.get(ChatSession, session_id)
        storage_agent = await db.get(Agent, anchor.agent_id)
        execution_agent = await db.get(Agent, candidate)
        execution_user = await db.get(User, anchor.user_id)
        participants = (
            {
                participant_id
                for participant_id in (session.agent_id, session.peer_agent_id)
                if participant_id is not None
            }
            if session is not None
            else set()
        )
        if (
            session is None
            or session.source_channel != "agent"
            or storage_agent is None
            or execution_agent is None
            or execution_user is None
            or session.agent_id != anchor.agent_id
            or candidate not in participants
            or anchor.sender_agent_id not in participants
            or anchor.sender_agent_id == candidate
            or storage_agent.tenant_id != execution_agent.tenant_id
            or execution_user.tenant_id != execution_agent.tenant_id
        ):
            logger.error(
                "[turn_recovery] rejected invalid A2A execution edge "
                f"anchor={anchor.id} candidate={candidate}"
            )
            return None
        return candidate
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


def _tool_payload(row: ChatMessage) -> dict | None:
    try:
        payload = json.loads(row.content or "{}")
    except (json.JSONDecodeError, TypeError):
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
    unfinished = [(row, payload, key) for row, payload, key in candidates if key not in done_keys]
    # Transaction-level ``now()`` gives all rows prewritten for one planned
    # round the same timestamp. The provider-issued index is authoritative
    # within that round; UUID ordering is random.
    grouped: dict[str, list[tuple[ChatMessage, dict, str]]] = {}
    group_order: list[str] = []
    for item in unfinished:
        round_id = str(item[1].get("round_id") or f"legacy:{item[0].id}")
        if round_id not in grouped:
            grouped[round_id] = []
            group_order.append(round_id)
        grouped[round_id].append(item)
    ordered: list[tuple[ChatMessage, dict, str]] = []
    for round_id in group_order:
        ordered.extend(
            sorted(
                grouped[round_id],
                key=lambda item: (
                    item[1].get("round_tool_index")
                    if isinstance(item[1].get("round_tool_index"), int)
                    else 2**31,
                    str(item[0].id),
                ),
            )
        )
    return ordered


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
        # A running marker proves only admission, not whether execution began,
        # completed, or committed an external effect. Never guess by tool name:
        # close every ambiguous call with an explicit provider-visible result
        # and let the next model round inspect state or issue a deliberate retry.
        result_text = (
            "[Recovery blocked automatic replay] The previous execution was "
            "interrupted before its durable result was committed. The platform "
            "did not execute it again. Inspect the target state before choosing "
            "a safe next action."
        )
        llm_view = await finalize_tool_output(
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
                    "assistant_content": payload.get("assistant_content"),
                    "recovery_prefix_messages": payload.get("recovery_prefix_messages") or [],
                    "round_id": payload.get("round_id"),
                    "round_tool_index": payload.get("round_tool_index"),
                },
                turn_anchor_id=anchor.id,
            )
            await done_db.commit()
        completed += 1
    return completed


async def _normalize_completed_tool_rounds_for_recovery(
    anchor: ChatMessage,
    *,
    execution_agent_id: uuid.UUID,
) -> None:
    """Apply the live 64K round budget to crash-interrupted durable rows.

    Done rows are committed after each tool so recovery never repeats a side
    effect. A crash can therefore occur before the live loop's end-of-round
    rewrite. ``round_id`` makes that window recoverable without guessing from
    timestamps or adjacent rows.
    """
    from app.services.llm.client import LLMMessage
    from app.services.llm.tool_output_store import enforce_message_budget

    # Snapshot under a short read transaction, then release the connection
    # before any local/S3 materialization. Exact row state is revalidated under
    # lock immediately before the transactional rewrite.
    async with async_session() as read_db:
        rows = list(
            (
                await read_db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == anchor.agent_id,
                        ChatMessage.conversation_id == anchor.conversation_id,
                        ChatMessage.role == "tool_call",
                        ChatMessage.message_meta["turn_anchor_id"].as_string()
                        == str(anchor.id),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        await read_db.commit()

    grouped: dict[str, list[tuple[uuid.UUID, dict]]] = {}
    for row in rows:
        payload = _tool_payload(row)
        if not payload or payload.get("status") != "done":
            continue
        round_id = str(payload.get("round_id") or "")
        if round_id:
            grouped.setdefault(round_id, []).append((row.id, dict(payload)))

    replacements: dict[uuid.UUID, tuple[str, str]] = {}
    expected_results: dict[uuid.UUID, str] = {}
    for round_rows in grouped.values():
        round_rows.sort(
            key=lambda item: (
                item[1].get("round_tool_index")
                if isinstance(item[1].get("round_tool_index"), int)
                else 2**31,
                str(item[0]),
            )
        )
        tool_calls: list[dict] = []
        messages: list[LLMMessage] = []
        for row_id, payload in round_rows:
            call_id = str(payload.get("call_id") or payload.get("tool_call_id") or row_id)
            args = payload.get("args")
            if args is None:
                args = payload.get("arguments") or {}
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(payload.get("name") or payload.get("tool_name") or "unknown"),
                        "arguments": json.dumps(args, ensure_ascii=False, default=str),
                    },
                }
            )
        messages.append(LLMMessage(role="assistant", content=None, tool_calls=tool_calls))
        for row_id, payload in round_rows:
            call_id = str(payload.get("call_id") or payload.get("tool_call_id") or row_id)
            messages.append(
                LLMMessage(
                    role="tool",
                    tool_call_id=call_id,
                    content=str(payload.get("result") or ""),
                )
            )
        rewrites = await asyncio.wait_for(
            enforce_message_budget(
                messages,
                fresh_start_idx=0,
                agent_id=execution_agent_id,
                session_id=anchor.conversation_id,
            ),
            timeout=RECOVERY_TOOL_MATERIALIZE_TIMEOUT_SECONDS,
        )
        row_by_call_id = {
            str(payload.get("call_id") or payload.get("tool_call_id") or row_id): (row_id, payload)
            for row_id, payload in round_rows
        }
        for rewrite in rewrites:
            matched = row_by_call_id.get(rewrite.tool_call_id)
            if matched is None:
                raise RuntimeError(
                    f"recovery tool rewrite missing row call_id={rewrite.tool_call_id!r}"
                )
            row_id, payload = matched
            replacements[row_id] = (rewrite.tool_call_id, rewrite.final_content)
            expected_results[row_id] = str(payload.get("result") or "")

    if replacements:
        async with async_session() as write_db:
            await rewrite_tool_call_done_results(
                write_db,
                agent_id=anchor.agent_id,
                user_id=anchor.user_id,
                conversation_id=anchor.conversation_id,
                replacements=replacements,
                turn_anchor_id=anchor.id,
                expected_results=expected_results,
            )
            await write_db.commit()


def _turn_status(row: ChatMessage) -> str:
    meta = row.message_meta if isinstance(row.message_meta, dict) else {}
    return str(meta.get("turn_status") or "")


def _metadata_int(meta: dict, key: str) -> int | None:
    try:
        return int(meta.get(key) or 0)
    except (TypeError, ValueError):
        return None


async def _load_recovery_origin(
    db,
    anchor: ChatMessage,
    *,
    for_update: bool = False,
    allow_completed: bool = False,
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
    if fresh_anchor is None:
        return None
    anchor_status = _turn_status(fresh_anchor)
    if anchor_status in {"cancelled", "failed"} or (
        anchor_status == "completed" and not allow_completed
    ):
        return None
    if session is None:
        return _RecoveryOrigin(False, None, None)
    external_conv_id = session.external_conv_id
    if external_conv_id and "__archived_" in external_conv_id:
        return None
    snapshot = conversation_turn_snapshot_for_session(session)
    anchor_meta = dict(fresh_anchor.message_meta or {})
    anchor_generation = _metadata_int(anchor_meta, "turn_generation")
    if anchor_generation is None:
        return None
    if anchor_status == "completed" and allow_completed:
        origin_anchor_id = fresh_anchor.id
        origin_generation = anchor_generation
    else:
        if snapshot.anchor_id is not None and (
            snapshot.anchor_id != fresh_anchor.id
            or snapshot.generation != anchor_generation
        ):
            return None
        origin_anchor_id = snapshot.anchor_id
        origin_generation = snapshot.generation
    return _RecoveryOrigin(
        True,
        session.source_channel,
        external_conv_id,
        origin_anchor_id,
        origin_generation,
    )


async def _load_fresh_recovery_origin(anchor: ChatMessage) -> _RecoveryOrigin | None:
    async with async_session() as db:
        return await _load_recovery_origin(db, anchor)


async def _ensure_recovery_owner(anchor: ChatMessage) -> bool:
    """Admit a legacy markerless anchor or validate its durable current owner."""

    try:
        session_id = uuid.UUID(str(anchor.conversation_id))
    except (TypeError, ValueError):
        return True
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        if session is None or session.agent_id != anchor.agent_id:
            return session is None
        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.anchor_id == anchor.id:
            return snapshot.status in {
                ACTIVE_TURN_STATUS,
                SUSPENDED_TURN_STATUS,
            }
        if snapshot.status in {ACTIVE_TURN_STATUS, SUSPENDED_TURN_STATUS}:
            return False
        try:
            await transition_conversation_turn(
                db,
                agent_id=anchor.agent_id,
                conversation_id=anchor.conversation_id,
                turn_anchor_id=anchor.id,
                status=ACTIVE_TURN_STATUS,
            )
        except (ConversationTurnConflict, LookupError, ValueError):
            await db.rollback()
            return False
        await db.commit()
        return True


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
    await _normalize_completed_tool_rounds_for_recovery(
        anchor,
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


async def _upsert_gateway_direct_reply(
    db,
    *,
    anchor: ChatMessage,
    reply: str,
    execution_agent_id: uuid.UUID,
) -> bool | None:
    """Write a Gateway reply outbox row in the caller's terminal transaction."""
    raw_anchor_meta = getattr(anchor, "message_meta", None)
    anchor_meta = raw_anchor_meta if isinstance(raw_anchor_meta, dict) else {}
    gateway_reply = anchor_meta.get("gateway_direct_reply")
    if not isinstance(gateway_reply, dict):
        return None
    try:
        gateway_message_id = uuid.UUID(str(gateway_reply["message_id"]))
        gateway_agent_id = uuid.UUID(str(gateway_reply["agent_id"]))
        gateway_sender_agent_id = uuid.UUID(
            str(gateway_reply["sender_agent_id"])
        )
    except (KeyError, TypeError, ValueError):
        return False
    if (
        gateway_agent_id != anchor.sender_agent_id
        or gateway_sender_agent_id != execution_agent_id
    ):
        return False
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.gateway_message import GatewayMessage

    await db.execute(
        pg_insert(GatewayMessage)
        .values(
            id=gateway_message_id,
            agent_id=gateway_agent_id,
            sender_agent_id=gateway_sender_agent_id,
            content=reply,
            status="pending",
            conversation_id=anchor.conversation_id,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return True


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
            allow_completed=True,
        )
        if current_origin != expected_origin:
            return False

        gateway_delivery = await _upsert_gateway_direct_reply(
            db,
            anchor=anchor,
            reply=reply,
            execution_agent_id=execution_agent_id,
        )
        if expected_origin.source_channel == "agent":
            if gateway_delivery is not None:
                if gateway_delivery:
                    await db.commit()
                return gateway_delivery
            # A standard native A2A session is itself the durable delivery
            # destination. The terminal assistant row was committed before
            # this origin check; it must not be routed through an IM adapter.
            return True

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
        delivered = await deliver_recovered_reply_to_origin(**delivery_kwargs)
    return delivered


async def _start_recovered_reactions(anchor: ChatMessage) -> ChannelReactions:
    reactions = await load_recovered_channel_reactions(
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
    )
    await run_channel_reaction_hook(
        reactions.on_recover,
        hook_name="recovery_prepare",
        timeout_seconds=3.0,
    )
    await run_channel_reaction_hook(
        reactions.on_consume,
        hook_name="recovery_consume",
    )
    return reactions


async def _finish_recovered_reactions(
    reactions: ChannelReactions,
    value: str | BaseException,
) -> None:
    if isinstance(value, BaseException):
        await run_channel_reaction_hook(
            reactions.on_error,
            value,
            hook_name="recovery_error",
        )
        return
    await run_channel_reaction_hook(
        reactions.on_complete,
        value,
        hook_name="recovery_complete",
    )


async def _persist_and_deliver_recovered_reply(
    anchor: ChatMessage,
    *,
    expected_origin: _RecoveryOrigin,
    execution_agent_id: uuid.UUID,
    reply: str,
) -> bool:
    """Commit the recovered terminal result, then deliver and promote."""

    if not await _recovery_origin_matches(anchor, expected_origin):
        raise _RecoveryFenceLost("recovery owner or route changed")

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
        gateway_outbox = await _upsert_gateway_direct_reply(
            db,
            anchor=anchor,
            reply=reply,
            execution_agent_id=execution_agent_id,
        )
        if gateway_outbox is False:
            raise RuntimeError("invalid Gateway direct reply route")
        await db.commit()
        from app.services.conversation_turn_lifecycle import (
            get_conversation_turn_snapshot,
        )

        turn_snapshot = await get_conversation_turn_snapshot(
            db,
            agent_id=anchor.agent_id,
            conversation_id=anchor.conversation_id,
            turn_anchor_id=anchor.id,
        )
    from app.services.conversation_turn_lifecycle import (
        publish_conversation_turn_event,
    )

    await publish_conversation_turn_event(
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        payload={
            "type": "done",
            "role": "assistant",
            "content": reply,
            "message_id": str(assistant_message_id),
        },
        snapshot=turn_snapshot,
        event_kind="turn_terminal",
    )
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


async def _kick_recovered_promoted_turn(anchor: ChatMessage) -> None:
    """Start a durably promoted successor after prior feedback is cleaned."""

    try:
        from app.services.turn_inbox import kick_promoted_turn_inbox

        await kick_promoted_turn_inbox(
            agent_id=anchor.agent_id,
            session_id=anchor.conversation_id,
        )
    except Exception:  # noqa: BLE001 - durable promotion is independently recoverable
        logger.opt(exception=True).warning(
            "[turn_recovery] promoted turn kick failed after recovery "
            "anchor={}",
            anchor.id,
        )


async def _load_recoverable_anchors(db) -> list[ChatMessage]:
    """Snapshot every recoverable turn with activity inside the safety window.

    A durable session owner is authoritative even when newer user messages are
    queued behind it. The recent message-tail scan remains as a compatibility
    fallback for turns created before the lifecycle pointer existed.

    This query deliberately has no candidate limit. Startup creates one task
    for every eligible turn; the existing workload-capacity layer remains the
    only execution admission boundary.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=_recovery_max_age_hours())
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
    )
    latest_rows = list(result.scalars().all())

    recent_session_ids: set[uuid.UUID] = set()
    for row in latest_rows:
        try:
            recent_session_ids.add(uuid.UUID(str(row.conversation_id)))
        except (TypeError, ValueError):
            continue

    sessions: list[ChatSession] = []
    if recent_session_ids:
        sessions = list(
            (
                await db.execute(
                    select(ChatSession).where(ChatSession.id.in_(recent_session_ids))
                )
            ).scalars()
        )

    authoritative_sessions: dict[
        tuple[uuid.UUID, str],
        tuple[ChatSession, ConversationTurnSnapshot],
    ] = {}
    owner_anchor_ids: set[uuid.UUID] = set()
    for session in sessions:
        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.status not in {ACTIVE_TURN_STATUS, SUSPENDED_TURN_STATUS}:
            continue
        if session.source_channel == "subagent":
            continue
        if snapshot.anchor_id is None:
            continue
        authoritative_sessions[(session.agent_id, str(session.id))] = (
            session,
            snapshot,
        )
        owner_anchor_ids.add(snapshot.anchor_id)

    owner_rows: dict[uuid.UUID, ChatMessage] = {}
    if owner_anchor_ids:
        owner_rows = {
            row.id: row
            for row in (
                (
                    await db.execute(
                        select(ChatMessage).where(ChatMessage.id.in_(owner_anchor_ids))
                    )
                ).scalars()
            )
        }

    anchors: list[ChatMessage] = []
    selected_anchor_ids: set[uuid.UUID] = set()
    for (agent_id, conversation_id), (_session, snapshot) in sorted(
        authoritative_sessions.items(),
        key=lambda item: item[0][1],
    ):
        anchor = owner_rows.get(snapshot.anchor_id)
        meta = (
            dict(anchor.message_meta or {})
            if anchor is not None
            else {}
        )
        if (
            anchor is None
            or anchor.agent_id != agent_id
            or anchor.conversation_id != conversation_id
            or anchor.role not in {"user", "system"}
            or _metadata_int(meta, "turn_generation") != snapshot.generation
            or _metadata_int(meta, "turn_revision") != snapshot.revision
            or str(meta.get("turn_status") or "") != snapshot.status
            or meta.get("consumed_by_onmessage")
            or meta.get("kind") == "on_message_event"
        ):
            logger.warning(
                "[turn_recovery] ignored inconsistent durable owner "
                "conversation={} anchor={}",
                conversation_id,
                snapshot.anchor_id,
            )
            continue
        if await _load_recovery_origin(db, anchor) is None:
            continue
        anchors.append(anchor)
        selected_anchor_ids.add(anchor.id)

    for latest_row in latest_rows:
        # A later queued input must never replace a durable current owner.
        if (
            latest_row.agent_id,
            latest_row.conversation_id,
        ) in authoritative_sessions:
            continue
        if not await _latest_row_needs_recovery(db, latest_row):
            continue
        anchor = await _find_turn_anchor_for_latest(db, latest_row)
        if anchor is None or anchor.id in selected_anchor_ids:
            continue
        if await _load_recovery_origin(db, anchor) is None:
            continue
        anchors.append(anchor)
        selected_anchor_ids.add(anchor.id)
    return sorted(anchors, key=lambda row: (row.created_at, str(row.id)))


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
        if meta.get("artifact_role") == "intermediate_assistant":
            return True
        # Persisted assistant output is the durable completion boundary. Without a
        # separate delivery receipt, startup cannot distinguish "persisted before
        # send" from "already sent"; retrying here duplicates every recent IM reply
        # on each restart. Recover only turns that stopped before assistant output.
        return is_incomplete_delivery_progress(row)
    if row.role == "user":
        return str(meta.get("turn_status") or "") not in TERMINAL_TURN_STATUSES
    if row.role != "tool_call":
        return False
    payload = _tool_payload(row)
    if payload is None:
        return False
    if payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME and payload.get("status") == "pending":
        return False
    return payload.get("status") in {"running", "done", "pending"}


async def _find_turn_anchor_for_latest(db, latest_row: ChatMessage) -> ChatMessage | None:
    candidate = latest_row
    if latest_row.role != "user":
        candidate = (
            await db.execute(
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
        ).scalar_one_or_none()
    if candidate is None:
        return None

    meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
    injected_root_id = meta.get("subagent_turn_anchor_id")
    if not injected_root_id and meta.get("turn_inbox_state") == "delivered":
        injected_root_id = meta.get("turn_inbox_anchor_id")
        try:
            session_id = uuid.UUID(str(candidate.conversation_id))
            inbox_generation = int(meta.get("turn_inbox_generation") or 0)
        except (TypeError, ValueError):
            return None
        session = await db.get(ChatSession, session_id)
        if session is None or session.agent_id != candidate.agent_id:
            return None
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if (
            str(snapshot.anchor_id or "") != str(injected_root_id or "")
            or snapshot.generation != inbox_generation
        ):
            return None
    if not injected_root_id:
        return candidate
    try:
        root_id = uuid.UUID(str(injected_root_id))
    except (TypeError, ValueError):
        return None
    root = await db.get(ChatMessage, root_id)
    if (
        root is None
        or root.role != "user"
        or root.agent_id != candidate.agent_id
        or root.conversation_id != candidate.conversation_id
    ):
        return None
    return root


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


async def _resume_one(
    anchor: ChatMessage,
) -> RecoveryStats:
    """Resume one independently cancellable anchor."""
    result = RecoveryStats()
    try:
        # A recovered turn owns its cancellation lifecycle. Running it in a
        # child task lets one stopped turn remain isolated while an actual
        # shutdown still cancels the whole startup recovery set.
        did_resume = await asyncio.create_task(resume_turn(anchor))
    except asyncio.CancelledError:
        recovery_task = asyncio.current_task()
        if recovery_task is not None and recovery_task.cancelling():
            raise
        result.skipped = 1
        logger.info(
            "[turn_recovery] recovery cancelled for anchor={}",
            anchor.id,
        )
    except Exception as exc:  # noqa: BLE001 - one failed turn must not cancel its batch
        result.failed = 1
        logger.exception(f"[turn_recovery] failed to resume anchor={anchor.id}: {exc}")
    else:
        if did_resume:
            result.resumed = 1
        else:
            result.skipped = 1
    return result


async def startup_turn_resume_once(*, limit: int = 50) -> RecoveryStats:
    """Resume startup-recoverable turn anchors once.

    All app instances may pass through this gate. A renewable Redis lease keeps
    one replica responsible for the complete recovery set without reserving a
    PostgreSQL connection while model turns execute. ``limit`` is retained only
    for bounded cleanup of already-terminal provider feedback; it never limits
    eligible turns. Callers may run this synchronously or as a detached startup
    background task.
    """
    stats = RecoveryStats()
    try:
        async with RedisLeaseLock(
            STARTUP_RECOVERY_LEASE_RESOURCE,
            namespace="turn-recovery",
        ):
            # Reaction callback closures cannot survive process restart. Consume
            # their durable provider markers independently from LLM resumption,
            # including sessions whose terminal assistant row was already saved.
            from app.services.turn_inbox import (
                cleanup_stale_channel_receipt_anchors,
            )

            try:
                await cleanup_stale_channel_receipt_anchors(limit=limit)
            except Exception:  # noqa: BLE001 - reaction cleanup cannot block recovery
                logger.opt(exception=True).warning(
                    "[turn_recovery] startup durable reaction cleanup failed"
                )
            # Scanning is a short read transaction. Close it before admission,
            # model execution, tool execution, or inter-turn waits.
            async with async_session() as db:
                anchors = await _load_recoverable_anchors(db)
            stats.scanned = len(anchors)
            if anchors:
                logger.info(
                    "[turn_recovery] resuming all eligible anchors in parallel: {}",
                    len(anchors),
                )
                tasks: list[asyncio.Task[RecoveryStats]] = []
                async with asyncio.TaskGroup() as task_group:
                    for anchor in anchors:
                        tasks.append(
                            task_group.create_task(
                                _resume_one(anchor),
                                name=f"turn_recovery:{anchor.id}",
                            )
                        )
                for task in tasks:
                    result = task.result()
                    stats.resumed += result.resumed
                    stats.skipped += result.skipped
                    stats.failed += result.failed
    except RedisLeaseBusyError:
        logger.info("[turn_recovery] another replica owns startup recovery")
    return stats


async def resume_turn(anchor: ChatMessage) -> bool:
    """Resume one inferred incomplete user turn via the normal channel LLM path."""

    if not await _ensure_recovery_owner(anchor):
        return False
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
            await _normalize_completed_tool_rounds_for_recovery(
                anchor,
                execution_agent_id=execution_agent_id,
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
            if history[-1].get("role") == "assistant":
                latest_assistant = (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.agent_id == anchor.agent_id,
                            ChatMessage.conversation_id == anchor.conversation_id,
                            ChatMessage.role == "assistant",
                            ChatMessage.compacted_into.is_(None),
                        )
                        .order_by(
                            ChatMessage.created_at.desc(),
                            ChatMessage.id.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                latest_meta = (
                    dict(latest_assistant.message_meta or {})
                    if latest_assistant is not None
                    else {}
                )
                if latest_meta.get("artifact_role") == "intermediate_assistant":
                    latest_assistant = None
                if latest_assistant is not None:
                    # Defensive guard for direct callers and races after anchor
                    # selection: only a terminal assistant means this turn is
                    # complete. A nonterminal intermediate is the recovery
                    # continuation point.
                    return False

        reactions = await _start_recovered_reactions(anchor)
        try:
            async with async_session() as db:
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
                    on_tool_call=reactions.on_tool_call,
                    on_thinking=reactions.on_thinking,
                    on_chunk=reactions.on_chunk,
                )
        except _RecoveryFenceLost as exc:
            await _finish_recovered_reactions(reactions, exc)
            return False
        except BaseException as exc:
            await _finish_recovered_reactions(reactions, exc)
            raise

        try:
            result = (
                await _persist_and_deliver_recovered_reply(
                    anchor,
                    expected_origin=expected_origin,
                    execution_agent_id=execution_agent_id,
                    reply=reply,
                )
                if reply and reply.strip()
                else False
            )
        except _RecoveryFenceLost as exc:
            await _finish_recovered_reactions(reactions, exc)
            return False
        except BaseException as exc:
            await _finish_recovered_reactions(reactions, exc)
            raise
        await _finish_recovered_reactions(reactions, reply or "")
        if result:
            await _kick_recovered_promoted_turn(anchor)
        return result
