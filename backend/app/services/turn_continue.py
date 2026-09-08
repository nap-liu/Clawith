"""Explicit continuation of the current conversation's failed model turn."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.conversation_turn_lifecycle import (
    TURN_SESSION_KEY,
    conversation_turn_snapshot_for_session,
    get_conversation_turn_snapshot,
    publish_conversation_turn_event,
)
from app.services.llm.failure_outcome import render_message


async def prepare_continue(db, *, agent_id, session_id, actor_user_id, locale="zh"):
    """Claim the latest failed owner; the authorized ingress owns commit.

    Reopening is deliberately separate from ordinary lifecycle transitions.
    The Session lock serializes new input, STOP, and duplicate commands.
    """
    def result(code):
        return {"action": f"continue_{code}", "message": render_message(f"commands.continue.{code}", locale)}

    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == uuid.UUID(str(session_id)),
            ChatSession.agent_id == agent_id,
        ).with_for_update().execution_options(populate_existing=True)
    )
    if session is None or "__archived_" in (session.external_conv_id or ""):
        return result("unavailable")
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.agent_type == "openclaw":
        return result("unavailable")
    snapshot = conversation_turn_snapshot_for_session(session)
    if snapshot.phase in {"active", "suspended"}:
        return result("busy")
    if snapshot.status != "failed" or snapshot.anchor_id is None:
        return result("unavailable")
    anchor = await db.get(ChatMessage, snapshot.anchor_id, with_for_update=True)
    if anchor is None or anchor.compacted_into is not None:
        return result("unavailable")
    meta = dict(anchor.message_meta or {})
    if (anchor.agent_id != agent_id or anchor.conversation_id != str(session.id)
            or meta.get("turn_status") != "failed"
            or meta.get("turn_generation") != snapshot.generation):
        return result("unavailable")
    failure = await db.scalar(
        select(ChatMessage).where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == str(session.id),
            ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
            ChatMessage.message_meta["llm_failure"]["code"].as_string().is_not(None),
            ChatMessage.message_meta["continued_at"].as_string().is_(None),
        ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1).with_for_update()
    )
    if failure is None:
        return result("unavailable")
    failure_code = (failure.message_meta or {}).get("llm_failure", {}).get("code", "")
    if failure_code == "tool_round_limit" or failure_code.startswith("tool_loop_"):
        return result("unavailable")

    now = datetime.now(UTC).isoformat()
    # Retain the failure as an audit message, without feeding control text back
    # to the model or mistaking it for this resumed attempt's terminal reply.
    failure.message_meta = {
        **dict(failure.message_meta or {}), "continued_at": now,
        "continued_by": str(actor_user_id) if actor_user_id else None,
        "continued_generation": snapshot.generation + 1, "turn_control_only": True,
    }
    meta.update({
        "turn_status": "running", "turn_generation": snapshot.generation + 1,
        "turn_revision": snapshot.revision + 1, "turn_state_token": str(uuid.uuid4()),
    })
    anchor.message_meta = meta
    session.im_config = {**dict(session.im_config or {}), TURN_SESSION_KEY: {
        "turn_anchor_id": str(anchor.id), "generation": meta["turn_generation"],
        "revision": meta["turn_revision"], "status": "running",
    }}
    await db.flush()
    return {**result("accepted"), "_continue_anchor_id": str(anchor.id)}


async def dispatch_continue(anchor_id):
    """Dispatch a committed claim through the existing durable recovery lane."""
    from app.database import async_session
    from app.services.turn_inbox import schedule_durable_turn_resume

    async with async_session() as db:
        anchor = await db.get(ChatMessage, uuid.UUID(str(anchor_id)))
        if anchor is None:
            return
        snapshot = await get_conversation_turn_snapshot(
            db, agent_id=anchor.agent_id, conversation_id=anchor.conversation_id,
            turn_anchor_id=anchor.id,
        )
    await publish_conversation_turn_event(
        agent_id=anchor.agent_id, conversation_id=anchor.conversation_id,
        payload={"type": "status", "content": render_message("commands.continue.accepted")},
        snapshot=snapshot, event_kind="turn_resumed",
    )
    await schedule_durable_turn_resume(anchor)
