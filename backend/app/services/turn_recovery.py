"""Startup-time recovery for turns interrupted by a backend restart."""

from __future__ import annotations

import asyncio
import json
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE, Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
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
from app.services.turn_recovery_startup import resume_startup_anchor
from app.services.turn_runtime import deliver_recovered_reply_to_origin, load_turn_runtime
from app.services.turn_recovery_identity import (
    _metadata_execution_agent_id,
    _validated_execution_agent_id,
)
from app.services.workload_capacity import WorkloadKind, get_workload_capacity

STARTUP_RECOVERY_LEASE_RESOURCE = "startup-turn-recovery"
RECOVERY_TOOL_MATERIALIZE_TIMEOUT_SECONDS = 60.0


from app.services.turn_recovery_types import (
    DEFAULT_RECOVERY_MAX_AGE_HOURS,
    RecoveryStats,
    _RecoveryFenceLost,
    _RecoveryOrigin,
    _recovery_max_age_hours,
)


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
    candidates: dict[str, tuple[ChatMessage, dict, str]] = {}
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
        previous = candidates.get(key)
        if previous and not payload.get("session_ref"):
            payload["session_ref"] = previous[1].get("session_ref")
        candidates[key] = (row, payload, key)
    unfinished = [item for key, item in candidates.items() if key not in done_keys]
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
    if anchor_status == "cancelled" or (
        anchor_status in {"completed", "failed"} and not allow_completed
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
    if anchor_status in {"completed", "failed"} and allow_completed:
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


from app.services import turn_recovery_tools as _turn_recovery_tools


async def _complete_unfinished_tool_calls(
    db,
    anchor: ChatMessage,
    *,
    ctx_size: int,
    expected_origin: _RecoveryOrigin,
    execution_agent_id: uuid.UUID | None = None,
    release_db_before_execution: bool = False,
) -> int:
    _turn_recovery_tools.persist_tool_call_row = persist_tool_call_row
    _turn_recovery_tools._recovery_origin_matches = _recovery_origin_matches
    return await _turn_recovery_tools._complete_unfinished_tool_calls(
        db,
        anchor,
        ctx_size=ctx_size,
        expected_origin=expected_origin,
        execution_agent_id=execution_agent_id,
        release_db_before_execution=release_db_before_execution,
    )


_normalize_completed_tool_rounds_for_recovery = (
    _turn_recovery_tools._normalize_completed_tool_rounds_for_recovery
)


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
        release_db_before_execution=True,
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
    """Validate the turn generation, then deliver outside the read transaction."""
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

        from app.services.turn_delivery_recovery import LOCAL_CHANNELS

        if expected_origin.source_channel in LOCAL_CHANNELS and message_id:
            row = await db.get(ChatMessage, uuid.UUID(str(message_id)))
            if (row is not None and row.conversation_id == anchor.conversation_id
                    and row.agent_id == anchor.agent_id
                    and (row.message_meta or {}).get("delivery", {}).get("status") == "sent"):
                # The terminal event already acknowledged this history reply.
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
    return await deliver_recovered_reply_to_origin(**delivery_kwargs)


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
    resume_promoted_turn: bool = True,
) -> bool:
    """Commit the recovered terminal result, then deliver and promote."""
    if not await _recovery_origin_matches(anchor, expected_origin):
        raise _RecoveryFenceLost("recovery owner or route changed")

    from app.services.background_turns import background_execution, complete_background_turn

    if background_execution(anchor):
        return await complete_background_turn(
            anchor, reply=reply, execution_agent_id=execution_agent_id,
            resume_promoted_turn=resume_promoted_turn,
        )

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


from app.services.turn_recovery_scanner import (
    _find_turn_anchor_for_latest,
    _latest_row_needs_recovery,
    _load_recoverable_anchors,
    _tail_has_pending_confirmation,
)


from app.services.turn_recovery_dispatch import (  # noqa: E402 - scanner imports core helpers
    _resume_one as _resume_one,
    startup_turn_resume_once as startup_turn_resume_once,
)


async def resume_turn(anchor: ChatMessage, *, resume_promoted_turn: bool = True) -> bool:
    """Execute or resume one durable turn through the shared channel path."""
    from app.services.background_turns import background_execution, reconcile_background_turn

    reconciled = await reconcile_background_turn(anchor, resume_promoted_turn=resume_promoted_turn)
    if reconciled is not None:
        return reconciled

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
        # OpenClaw executes externally and is explicitly outside native recovery.
        if agent is None or getattr(agent, "agent_type", None) == "openclaw":
            return False
        tenant_id = agent.tenant_id
        ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
        execution = background_execution(anchor)
        workload_kind = WorkloadKind.SCHEDULED if execution.get("kind") in {"trigger", "schedule"} else WorkloadKind.BACKGROUND
        runtime_workspace = None
        if getattr(agent, "scope", None) == "project":
            from app.services.agent_runtime_workspace import resolve_agent_runtime_workspace

            session = await db.get(ChatSession, uuid.UUID(anchor.conversation_id))
            runtime_workspace = resolve_agent_runtime_workspace(
                agent_id=agent.id, agent_scope=agent.scope, agent_project_id=agent.project_id,
                tenant_id=agent.tenant_id, session_project_id=session.project_id,
                session_config=session.im_config,
            )

    async with get_workload_capacity().slot(workload_kind, tenant_id):
        if getattr(agent, "scope", None) == "project":
            from app.services.project_runtime_boundary import project_agent_runtime_allows

            async with async_session() as db:
                if not await project_agent_runtime_allows(db, agent):
                    return False
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
                            ChatMessage.message_meta["turn_control_only"].as_boolean().is_not(True),
                            ChatMessage.message_meta["artifact_role"].as_string().is_distinct_from("command_reply"),
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

        from app.services.background_turns import background_call_options

        call_options = {"turn_type": "recovery", **background_call_options(anchor)}
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
                    runtime_workspace=runtime_workspace,
                    **call_options,
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
                    resume_promoted_turn=resume_promoted_turn,
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
        if result and resume_promoted_turn:
            await _kick_recovered_promoted_turn(anchor)
        return result
