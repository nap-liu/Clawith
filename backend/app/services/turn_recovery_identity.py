"""Execution-agent identity validation for interrupted turn recovery."""

from __future__ import annotations

import uuid

from loguru import logger

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.models.trigger_execution import TriggerExecution


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
    """Resolve execution identity through the persisted A2A or Subagent edge."""
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
        # A subscription event is system-owned, not a fabricated peer message.
        # Its durable TriggerExecution supplies the same authenticated edge that
        # ordinary A2A obtains from sender_agent_id.
        background = meta.get("background_execution") or {}
        if background.get("kind") == "trigger" and meta.get("kind") == "on_message_event":
            try:
                execution_id = uuid.UUID(str(background.get("reference_id")))
            except (TypeError, ValueError):
                return None
            execution = await db.get(TriggerExecution, execution_id)
            payload = (execution.payload or {}) if execution is not None else {}
            valid_sender_edge = bool(
                execution is not None
                and execution.agent_id == candidate
                and execution.conversation_id == session_id
                and execution.execution_user_id == anchor.user_id
                and str(execution.id) == str(meta.get("trigger_execution_id"))
                and str(execution.trigger_id) == str(meta.get("trigger_id"))
                and str(payload.get("_origin_session_id")) == str(session_id)
            )
        else:
            valid_sender_edge = anchor.sender_agent_id in participants and anchor.sender_agent_id != candidate
        if (
            session is None
            or session.source_channel != "agent"
            or storage_agent is None
            or execution_agent is None
            or execution_user is None
            or session.agent_id != anchor.agent_id
            or candidate not in participants
            or not valid_sender_edge
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
