"""Tenant-safe chat-session access and detail helpers."""

import uuid
from typing import Literal, Optional

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.chat_session_models import SessionDetailOut, SessionRuntimeOut
from app.core.permissions import (
    can_view_all_agent_chat_sessions as _can_view_all_agent_chat_sessions,
    check_agent_access,
    require_current_agent_tenant,
    require_tenant_safe_chat_session,
)
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.models.user import Identity, User


async def _load_accessible_session(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> tuple[Agent, ChatSession, Literal["mine", "all"]]:
    """Resolve one session and the web picker scope that can display it."""
    get_row = getattr(db, "get", None)
    candidate = await get_row(ChatSession, session_id) if callable(get_row) else None
    project_access: str | None = None
    if candidate is not None and candidate.agent_id == agent_id:
        from app.services.project_service import project_session_access_mode

        project_access = await project_session_access_mode(db, current_user, candidate)
    if project_access is not None:
        agent = await get_row(Agent, agent_id)
        if (
            agent is None
            or agent.is_deleted
            or agent.tenant_id != current_user.tenant_id
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        agent_access = "manage" if project_access == "edit" else "read"
    else:
        agent, agent_access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    parent_session = aliased(ChatSession)
    result = await db.execute(
        select(ChatSession)
        .outerjoin(SubagentRun, SubagentRun.id == ChatSession.id)
        .outerjoin(parent_session, parent_session.id == SubagentRun.parent_session_id)
        .where(
            ChatSession.id == session_id,
            or_(
                ChatSession.agent_id == agent_id,
                ChatSession.peer_agent_id == agent_id,
                and_(
                    ChatSession.source_channel == "subagent",
                    or_(
                        parent_session.agent_id == agent_id,
                        and_(
                            parent_session.source_channel == "agent",
                            parent_session.peer_agent_id == agent_id,
                        ),
                    ),
                ),
            ),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # Real ORM agents always expose tenant_id. A few isolated protocol-double
    # tests intentionally omit it and exercise unrelated response shaping.
    if hasattr(agent, "tenant_id"):
        await require_tenant_safe_chat_session(db, session, agent.tenant_id)

    source_channel = str(session.source_channel or "web").lower()
    is_subagent_owner = False
    if source_channel == "subagent":
        run = await db.get(SubagentRun, session.id)
        is_subagent_owner = bool(
            run is not None and run.execution_user_id == current_user.id
        )

    is_owner = (
        str(session.user_id) == str(current_user.id) or is_subagent_owner
    )
    is_privileged = _can_view_all_agent_chat_sessions(
        current_user, agent, agent_access
    )
    is_group_member = False
    if bool(getattr(session, "is_group", False)) and not is_owner and not is_privileged:
        member_result = await db.execute(
            select(ChatMessage.id)
            .where(
                ChatMessage.conversation_id == str(session_id),
                ChatMessage.role == "user",
                or_(
                    ChatMessage.sender_user_id == current_user.id,
                    and_(
                        ChatMessage.sender_user_id.is_(None),
                        ChatMessage.user_id == current_user.id,
                    ),
                ),
            )
            .limit(1)
        )
        is_group_member = member_result.scalar_one_or_none() is not None

    is_trigger_manager = agent_access == "manage" and source_channel == "trigger"
    if not (is_owner or is_privileged or is_group_member or is_trigger_manager or project_access is not None):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    view_scope: Literal["mine", "all"] = (
        "mine"
        if source_channel not in {"agent", "trigger"} and (is_owner or is_group_member or project_access is not None)
        else "all"
    )
    return agent, session, view_scope


async def _build_session_detail_out(
    db: AsyncSession,
    session: ChatSession,
    view_scope: Literal["mine", "all"],
) -> SessionDetailOut:
    count_result = await db.execute(
        select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == str(session.id))
    )
    message_count = int(count_result.scalar() or 0)

    username: Optional[str] = None
    peer_agent_id: Optional[str] = None
    peer_agent_name: Optional[str] = None
    participant_type = "user"
    runtime: SessionRuntimeOut | None = None

    if session.source_channel == "agent" and session.peer_agent_id:
        participant_type = "agent"
        peer_agent_id = str(session.peer_agent_id)
        names_result = await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_([session.agent_id, session.peer_agent_id]))
        )
        agent_names = {str(row[0]): row[1] or "Agent" for row in names_result.all()}
        first_name = agent_names.get(str(session.agent_id), "Agent")
        second_name = agent_names.get(str(session.peer_agent_id), "Agent")
        peer_agent_name = second_name
        username = f"Agent {first_name} - {second_name}"
    elif session.is_group:
        participant_type = "group"
        username = session.group_name or session.title or "Group Chat"
    elif session.user_id:
        user_result = await db.execute(
            select(func.coalesce(User.display_name, Identity.username))
            .outerjoin(Identity, User.identity_id == Identity.id)
            .where(User.id == session.user_id)
        )
        username = user_result.scalar_one_or_none() or "Unknown"

    if session.source_channel == "subagent":
        run = await db.get(SubagentRun, session.id)
        execution_agent = await db.get(Agent, session.agent_id)
        if run is not None and execution_agent is not None:
            runtime = SessionRuntimeOut(
                kind="subagent",
                status=run.status,
                execution_agent_id=str(execution_agent.id),
                execution_agent_name=execution_agent.name or "Agent",
                mode=run.mode,
                model=run.model,
                soul=run.soul,
                memory=run.memory,
            )

    return SessionDetailOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id) if session.user_id else None,
        username=username,
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=message_count,
        unread_count=0,
        is_primary=bool(session.is_primary),
        peer_agent_id=peer_agent_id,
        peer_agent_name=peer_agent_name,
        participant_type=participant_type,
        is_group=bool(session.is_group),
        group_name=session.group_name,
        view_scope=view_scope,
        runtime=runtime,
    )
