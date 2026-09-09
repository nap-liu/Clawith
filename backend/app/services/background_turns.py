"""Background adapters for the existing durable conversation executor."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.conversation_turn_lifecycle import transition_conversation_turn


def background_execution(anchor: ChatMessage) -> dict:
    return dict((anchor.message_meta or {}).get("background_execution") or {})


async def initialize_background_turn(
    db, *, session, anchor, kind, reference_id, settings=None, completion=None,
):
    """Persist execution options with the original input, in the caller's transaction."""
    if background_execution(anchor):
        return anchor
    await db.flush()
    anchor.message_meta = {
        **dict(anchor.message_meta or {}),
        "source_channel": session.source_channel,
        "background_execution": {
            "kind": kind,
            "reference_id": str(reference_id),
            "settings": settings or {},
            "completion": completion or {},
            "finalized": False,
            "delivered": False,
        },
    }
    execution_agent_id = (settings or {}).get("execution_agent_id")
    if execution_agent_id:
        anchor.message_meta = {**anchor.message_meta, "execution_agent_id": str(execution_agent_id)}
    await transition_conversation_turn(
        db, agent_id=session.agent_id, conversation_id=str(session.id),
        turn_anchor_id=anchor.id, status="running",
    )
    return anchor


def background_call_options(anchor: ChatMessage) -> dict:
    """Reuse the ordinary channel caller's options without a second model loop."""
    execution = background_execution(anchor)
    settings = execution.get("settings") or {}
    names = (
        "model_override_id", "temperature_override", "reasoning_effort_override",
        "include_soul", "include_memory", "max_tool_rounds_override", "prepared_turn_context",
    )
    options = {name: settings[name] for name in names if name in settings}
    if execution:
        options["turn_type"] = execution["kind"]
    return options


async def background_reply(db, anchor):
    return (await db.execute(select(ChatMessage).where(
        ChatMessage.conversation_id == anchor.conversation_id,
        ChatMessage.role == "assistant",
        ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
        ChatMessage.message_meta["turn_status"].as_string().in_(("completed", "failed")),
    ).order_by(ChatMessage.created_at, ChatMessage.id).limit(1))).scalar_one_or_none()


async def _lock_anchor(db, anchor):
    # Lifecycle/STOP writers always lock Session before its anchor.
    await db.get(ChatSession, uuid.UUID(anchor.conversation_id), with_for_update=True)
    return await db.get(ChatMessage, anchor.id, with_for_update=True)


async def _finalize(db, anchor, reply_row):
    execution = background_execution(anchor)
    if execution.get("finalized"):
        return
    kind = execution["kind"]
    if kind == "trigger":
        from app.services.trigger_turn_completion import finalize_trigger_turn

        await finalize_trigger_turn(db, anchor, reply_row)
    elif kind in {"task", "schedule", "oneshot", "heartbeat"}:
        from app.services.background_task_completion import (
            finalize_task_turn, finalize_schedule_turn, finalize_oneshot_turn, finalize_heartbeat_turn,
        )

        finalizer = {
            "task": finalize_task_turn,
            "schedule": finalize_schedule_turn,
            "oneshot": finalize_oneshot_turn,
            "heartbeat": finalize_heartbeat_turn,
        }[kind]
        await finalizer(db, anchor, reply_row)
    # Finalizers may add durable destination references to the completion data.
    execution = background_execution(anchor)
    execution["finalized"] = True
    anchor.message_meta = {**dict(anchor.message_meta or {}), "background_execution": execution}


async def _deliver(anchor, reply_row, *, resume_promoted_turn: bool = True) -> bool:
    execution = background_execution(anchor)
    if execution.get("delivered"):
        return True
    if reply_row is not None:
        from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

        await publish_committed_turn_terminal(
            agent_id=anchor.agent_id,
            conversation_id=anchor.conversation_id,
            turn_anchor_id=anchor.id,
            message_id=reply_row.id,
            content=reply_row.content,
            resume_promoted_turn=resume_promoted_turn,
        )
    if execution["kind"] == "trigger" and reply_row is not None:
        from app.services.trigger_turn_completion import deliver_trigger_turn

        if not await deliver_trigger_turn(anchor, reply_row):
            return False
    async with async_session() as db:
        current = await _lock_anchor(db, anchor)
        current_execution = background_execution(current)
        current_execution["delivered"] = True
        current.message_meta = {**dict(current.message_meta or {}), "background_execution": current_execution}
        await db.commit()
    return True


async def reconcile_background_turn(anchor, *, resume_promoted_turn: bool = True) -> bool | None:
    """Finish an existing terminal result; None means the model still needs to run."""
    if not background_execution(anchor):
        return None
    async with async_session() as db:
        current = await _lock_anchor(db, anchor)
        status = (current.message_meta or {}).get("turn_status")
        if status not in {"completed", "failed", "cancelled"}:
            return None
        reply = await background_reply(db, current)
        if status != "cancelled" and reply is None:
            return False
        await _finalize(db, current, reply)
        await db.commit()
    return await _deliver(current, reply, resume_promoted_turn=resume_promoted_turn)


async def complete_background_turn(
    anchor, *, reply, execution_agent_id, resume_promoted_turn: bool = True,
) -> bool:
    """Persist the ordinary reply and business completion atomically."""
    from app.services.chat_history import persist_assistant_reply_row

    async with async_session() as db:
        current = await _lock_anchor(db, anchor)
        status = (current.message_meta or {}).get("turn_status")
        reply_row = await background_reply(db, current)
        if status == "cancelled":
            reply_row = None
        elif reply_row is None:
            completion = background_execution(current).get("completion") or {}
            message_id = completion.get("final_message_id")
            participant_id = completion.get("participant_id")
            result_id = await persist_assistant_reply_row(
                db, agent_id=current.agent_id, user_id=current.user_id,
                conversation_id=current.conversation_id, content=reply,
                turn_anchor_id=current.id, sender_agent_id=execution_agent_id,
                message_id=uuid.UUID(message_id) if message_id else None,
                participant_id=uuid.UUID(participant_id) if participant_id else None,
                message_meta=completion.get("message_meta"),
            )
            await db.flush()
            reply_row = await db.get(ChatMessage, result_id)
        await _finalize(db, current, reply_row)
        await db.commit()
    return await _deliver(current, reply_row, resume_promoted_turn=resume_promoted_turn)


async def run_background_turn(anchor_id) -> str | None:
    """First execution and restart recovery use exactly the same executor."""
    from app.services.turn_recovery_startup import resume_startup_anchor

    async with async_session() as db:
        anchor = await db.get(ChatMessage, uuid.UUID(str(anchor_id)))
    if anchor is None:
        return None
    await resume_startup_anchor(anchor)
    async with async_session() as db:
        reply = await background_reply(db, anchor)
    if reply is None:
        return None
    metadata = dict(reply.message_meta or {})
    failure = dict(metadata.get("llm_failure") or {})
    code = metadata.get("error_code") or failure.get("code")
    if code:
        from app.services.llm.failure_outcome import LLMFailure

        return LLMFailure(
            reply.content,
            code=str(code),
            message_key=str(failure.get("message_key") or "errors.modelTurnFailed"),
            retryable=bool(failure.get("retryable", False)),
            allow_failover=bool(failure.get("allow_failover", False)),
            details={key: value for key, value in failure.items()
                     if key not in {"code", "message_key", "retryable", "allow_failover"}},
        )
    return reply.content
