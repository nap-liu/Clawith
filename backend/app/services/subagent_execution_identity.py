"""Keep child execution within its admitted human IM conversation authority."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.models.user import User
from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session
from app.services.execution_identity import ExecutionIdentityError, resolve_execution_user_id
from app.services.session_query import HUMAN_CHANNELS


async def _validate_origin(db, agent, user_id, origin):
    try:
        session_id = uuid.UUID(origin["session_id"])
        anchor_id = uuid.UUID(origin["anchor_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    session = await db.get(ChatSession, session_id)
    anchor = await db.get(ChatMessage, anchor_id)
    user = await db.scalar(select(User).where(User.id == user_id).options(selectinload(User.identity)))
    im_channels = HUMAN_CHANNELS - {"web", "mcp", "miniprogram", "wechat_miniprogram"}
    meta = dict(anchor.message_meta or {}) if anchor else {}
    return bool(
        user and user.is_active and (not user.identity or user.identity.is_active)
        and agent.tenant_id is not None and user.tenant_id == agent.tenant_id
        and session and session.agent_id == agent.id and session.project_id is None
        and session.source_channel in im_channels
        and (session.is_group or session.user_id == user_id)
        and anchor and anchor.agent_id == agent.id and anchor.conversation_id == str(session.id)
        and anchor.role == "user" and anchor.sender_user_id == user_id
        and not meta.get("kind") and not meta.get("trigger_execution_id")
        and meta.get("turn_anchor_id") == str(anchor.id)
        and int(meta.get("turn_generation") or 0) > 0
    )


async def resolve_subagent_execution_user(
    db, agent, user_id, *, parent=None, anchor_id=None, origin=None,
):
    """Return the actual principal and a narrow, durable origin reference.

    Ordinary Agent access stays authoritative without an admitted IM origin.
    The reference is server-created here, never taken from tool arguments.
    """
    try:
        resolved = await resolve_execution_user_id(db, agent, user_id)
        return resolved, None
    except ExecutionIdentityError as denied:
        if parent is not None:
            snapshot = conversation_turn_snapshot_for_session(parent)
            if (snapshot.status != "running" or snapshot.anchor_id != anchor_id
                    or parent.agent_id != agent.id or snapshot.generation < 1):
                raise denied
            origin = {"session_id": str(parent.id), "anchor_id": str(anchor_id)}
            if parent.source_channel == "subagent":
                parent_run = await db.get(SubagentRun, parent.id)
                if parent_run is None or parent_run.execution_user_id != user_id:
                    raise denied
                origin = dict(parent.im_config or {}).get("execution_origin")
        if not await _validate_origin(db, agent, user_id, origin):
            raise denied
        return user_id, dict(origin)
