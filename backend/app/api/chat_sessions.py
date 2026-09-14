"""Chat session management API endpoints."""

import base64
import binascii
import json
import re
import uuid
from datetime import datetime
from datetime import timezone as tz
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.permissions import (
    can_modify_other_agent_chat_sessions,
    can_view_all_agent_chat_sessions,
    check_agent_access,
    filter_tenant_safe_chat_sessions,
    require_current_agent_tenant,
    require_tenant_safe_chat_session,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.models.user import Identity, User
from app.services.auth_code_exchange import validate_platform_login_channel
from app.services.chat_message_serializer import (
    merge_tool_call_update_for_client,
    serialize_chat_message_for_client,
    serialize_tool_call_for_client,
)
from app.services.chat_session_service import (
    get_latest_platform_session,
    promote_platform_session,
    lock_platform_sessions,
)

router = APIRouter(prefix="/api/agents", tags=["chat-sessions"])


# Single source of truth lives in app.core.permissions; aliased here so the
# existing call sites (and their "_" private-by-convention name) stay unchanged.
_can_view_all_agent_chat_sessions = can_view_all_agent_chat_sessions


from app.api.chat_session_models import (
    SessionDetailOut,
    SessionOut,
    SessionPageOut,
    SessionRuntimeOut,
)
import app.api.chat_session_access as _chat_session_access
import app.api.chat_session_message_query as _chat_session_message_query

for _compat_symbol in (SessionOut, SessionRuntimeOut, SessionDetailOut, SessionPageOut):
    _compat_symbol.__module__ = __name__


def _encode_session_cursor(
    session: ChatSession | None,
    *,
    scope: Literal["mine", "all"],
    primary_id: uuid.UUID | None = None,
) -> str:
    payload = {
        "v": 1,
        "scope": scope,
        "created_at": session.created_at.isoformat() if session is not None else None,
        "id": str(session.id) if session is not None else None,
        "primary_id": str(primary_id) if primary_id else None,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_session_cursor(
    cursor: str,
    *,
    scope: Literal["mine", "all"],
) -> tuple[datetime | None, uuid.UUID | None, uuid.UUID | None]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(f"{cursor}{padding}").decode("utf-8"))
        if payload.get("v") != 1 or payload.get("scope") != scope:
            raise ValueError("cursor scope mismatch")
        created_at = datetime.fromisoformat(payload["created_at"]) if payload.get("created_at") else None
        session_id = uuid.UUID(payload["id"]) if payload.get("id") else None
        primary_id = uuid.UUID(payload["primary_id"]) if payload.get("primary_id") else None
        if (created_at is None) != (session_id is None):
            raise ValueError("incomplete cursor")
        return created_at, session_id, primary_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid session cursor") from exc


def _session_cursor_filter(created_at: datetime, session_id: uuid.UUID):
    return or_(
        ChatSession.created_at < created_at,
        and_(ChatSession.created_at == created_at, ChatSession.id < session_id),
    )


class CreateSessionIn(BaseModel):
    title: Optional[str] = None
    source_channel: str = "web"


class PatchSessionIn(BaseModel):
    title: str


async def _load_accessible_session(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> tuple[Agent, ChatSession, Literal["mine", "all"]]:
    _chat_session_access.check_agent_access = check_agent_access
    _chat_session_access.require_current_agent_tenant = require_current_agent_tenant
    _chat_session_access.require_tenant_safe_chat_session = require_tenant_safe_chat_session
    _chat_session_access._can_view_all_agent_chat_sessions = (
        _can_view_all_agent_chat_sessions
    )
    return await _chat_session_access._load_accessible_session(
        db, current_user, agent_id, session_id
    )


async def _build_session_detail_out(
    db: AsyncSession,
    session: ChatSession,
    view_scope: Literal["mine", "all"],
    task_id: uuid.UUID | None = None,
) -> SessionDetailOut:
    return await _chat_session_access._build_session_detail_out(
        db, session, view_scope, task_id=task_id
    )


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: str = Query("mine", description="'mine' or 'all'"),
    source_channel: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    cursor: Optional[str] = None,
    paginated: bool = False,
    exclude_mine: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions for an agent. scope=all for org/platform admins and agent_admin."""
    # Verify agent exists
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="未找到数字员工")
    _, agent_access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    source_channel = (source_channel or "").strip() or None
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    use_cursor_pagination = paginated and (cursor is not None or offset == 0)
    group_membership = (
        select(ChatMessage.id)
        .where(
            ChatMessage.conversation_id == cast(ChatSession.id, String),
            ChatMessage.role == "user",
            or_(
                ChatMessage.sender_user_id == current_user.id,
                and_(
                    ChatMessage.sender_user_id.is_(None),
                    ChatMessage.user_id == current_user.id,
                ),
            ),
        )
        .correlate(ChatSession)
        .exists()
    )

    if scope == "all":
        if not _can_view_all_agent_chat_sessions(current_user, agent, agent_access):
            raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

        # Fetch all sessions (including agent-to-agent where this agent is peer)
        all_where = (
            (
                (ChatSession.agent_id == agent_id)
                | ((ChatSession.peer_agent_id == agent_id) & (ChatSession.source_channel == "agent"))
            )
            & (ChatSession.source_channel != "subagent")
            # Project conversations have their own project-scoped navigation
            # and APIs.  Never leak planning, group, or direct project A2A
            # threads into the ordinary Web Agent session picker.
            & ChatSession.project_id.is_(None)
        )
        has_messages = (
            select(ChatMessage.id)
            .where(ChatMessage.conversation_id == cast(ChatSession.id, String))
            .correlate(ChatSession)
            .exists()
        )
        query = select(ChatSession).where(
            all_where,
            or_(ChatSession.is_primary.is_(True), has_messages),
        )
        if exclude_mine:
            query = query.where(
                ChatSession.source_channel != "trigger",
                or_(
                    ChatSession.source_channel == "agent",
                    and_(ChatSession.is_group.is_(True), ~group_membership),
                    and_(
                        ChatSession.is_group.is_(False),
                        ChatSession.user_id.is_not(None),
                        ChatSession.user_id != current_user.id,
                    ),
                ),
            )
        if source_channel:
            query = query.where(ChatSession.source_channel == source_channel)
        cursor_created_at = None
        cursor_session_id = None
        if use_cursor_pagination and cursor:
            cursor_created_at, cursor_session_id, _primary_id = _decode_session_cursor(cursor, scope="all")
            if cursor_created_at is not None and cursor_session_id is not None:
                query = query.where(_session_cursor_filter(cursor_created_at, cursor_session_id))
        if use_cursor_pagination:
            query = query.order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        else:
            query = query.order_by(
                ChatSession.last_message_at.desc().nulls_last(),
                ChatSession.created_at.desc().nulls_last(),
                ChatSession.id.desc(),
            ).offset(offset)
        result = await db.execute(query.limit(limit + 1))
        raw_sessions = list(result.scalars().all())
        has_more = len(raw_sessions) > limit
        sessions = raw_sessions[:limit]
        cursor_anchor = sessions[-1] if sessions else None
        if hasattr(agent, "tenant_id"):
            sessions = await filter_tenant_safe_chat_sessions(
                db, list(sessions), agent.tenant_id
            )
        out = []

        # --- BULK FETCH: message counts, user names, agent names in 3 queries total ---
        session_ids = [str(s.id) for s in sessions]
        session_uuid_ids = [s.id for s in sessions]

        message_counts: dict[str, int] = {}
        unread_counts: dict[str, int] = {}
        if session_ids:
            count_res = await db.execute(
                select(ChatMessage.conversation_id, func.count(ChatMessage.id))
                .where(ChatMessage.conversation_id.in_(session_ids))
                .group_by(ChatMessage.conversation_id)
            )
            for row in count_res.all():
                message_counts[row[0]] = row[1]

            unread_res = await db.execute(
                select(ChatSession.id, func.count(ChatMessage.id))
                .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
                .where(
                    ChatSession.id.in_(session_uuid_ids),
                    ChatSession.user_id == current_user.id,
                    ChatSession.source_channel.notin_(["agent", "trigger"]),
                    ChatSession.is_group.is_(False),
                    ChatMessage.role.in_(["assistant", "system", "tool_call"]),
                    ChatMessage.created_at > func.coalesce(
                        ChatSession.last_read_at_by_user,
                        datetime(1970, 1, 1, tzinfo=tz.utc),
                    ),
                )
                .group_by(ChatSession.id)
            )
            for row in unread_res.all():
                unread_counts[str(row[0])] = int(row[1] or 0)

        # Collect IDs to resolve in bulk
        user_ids = list({s.user_id for s in sessions
                         if not s.is_group and s.source_channel != "agent" and s.user_id})
        user_names: dict[str, str] = {}
        if user_ids:
            user_r = await db.execute(
                select(User.id, func.coalesce(User.display_name, Identity.username))
                .outerjoin(Identity, User.identity_id == Identity.id)
                .where(User.id.in_(user_ids))
            )
            for row in user_r.all():
                user_names[str(row[0])] = row[1] or "Unknown"

        agent_ids_to_fetch: set = set()
        for s in sessions:
            if s.source_channel == "agent" and s.peer_agent_id:
                agent_ids_to_fetch.add(s.agent_id)
                agent_ids_to_fetch.add(s.peer_agent_id)
        agent_names: dict[str, str] = {}
        if agent_ids_to_fetch:
            agent_r = await db.execute(
                select(Agent.id, Agent.name).where(Agent.id.in_(list(agent_ids_to_fetch)))
            )
            for row in agent_r.all():
                agent_names[str(row[0])] = row[1] or "Agent"

        for session in sessions:
            count = message_counts.get(str(session.id), 0)
            if count == 0 and not session.is_primary:
                continue  # hide empty sessions

            display = None
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"

            if session.source_channel == "agent" and session.peer_agent_id:
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                a1_name = agent_names.get(str(session.agent_id), "Agent")
                a2_name = agent_names.get(str(session.peer_agent_id), "Agent")
                peer_agent_name = a2_name
                display = f"Agent {a1_name} - {a2_name}"
            elif session.is_group:
                display = session.group_name or session.title or "Group Chat"
            else:
                display = user_names.get(str(session.user_id), "Unknown")

            out.append(SessionOut(
                id=str(session.id),
                agent_id=str(session.agent_id),
                user_id=str(session.user_id) if session.user_id else None,
                username=display,
                source_channel=session.source_channel,
                title=session.title,
                created_at=session.created_at.isoformat(),
                last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                message_count=count,
                unread_count=unread_counts.get(str(session.id), 0),
                is_primary=bool(session.is_primary),
                peer_agent_id=peer_agent_id,
                peer_agent_name=peer_agent_name,
                participant_type="group" if session.is_group else participant_type,
                is_group=session.is_group,
                group_name=session.group_name,
            ))
        if paginated:
            next_cursor = (
                _encode_session_cursor(cursor_anchor, scope="all")
                if use_cursor_pagination and has_more and cursor_anchor is not None
                else None
            )
            return SessionPageOut(
                items=out,
                has_more=has_more,
                next_offset=None if use_cursor_pagination else (offset + limit if has_more else None),
                next_cursor=next_cursor,
            )
        return out

    else:  # scope == "mine"
        # Group membership signal: at least one user-role message authored by
        # the current user in this session. Mirrors P2P/group-chat client UX —
        # if you have spoken in the group, the conversation surfaces only in
        # the viewer's own list and is excluded from "other sessions".
        has_agent_messages = (
            select(ChatMessage.id)
            .where(
                ChatMessage.conversation_id == cast(ChatSession.id, String),
                ChatMessage.agent_id == agent_id,
            )
            .correlate(ChatSession)
            .exists()
        )
        query = (
            select(ChatSession)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.project_id.is_(None),
                ChatSession.source_channel.notin_(["agent", "trigger", "subagent"]),
                or_(ChatSession.is_primary.is_(True), has_agent_messages),
                or_(
                    and_(
                        ChatSession.is_group.is_(False),
                        ChatSession.user_id == current_user.id,
                    ),
                    and_(
                        ChatSession.is_group.is_(True),
                        group_membership,
                    ),
                ),
            )
        )
        if source_channel:
            query = query.where(ChatSession.source_channel == source_channel)
        primary_id = None
        cursor_anchor = None
        if use_cursor_pagination:
            cursor_created_at = None
            cursor_session_id = None
            if cursor:
                cursor_created_at, cursor_session_id, primary_id = _decode_session_cursor(cursor, scope="mine")
            primary_session = None
            ordinary_query = query
            if cursor is None:
                primary_session = await db.scalar(
                    query.where(ChatSession.is_primary.is_(True))
                    .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
                    .limit(1)
                )
                primary_id = primary_session.id if primary_session else None
                ordinary_query = ordinary_query.where(ChatSession.is_primary.is_(False))
            elif primary_id is not None:
                ordinary_query = ordinary_query.where(ChatSession.id != primary_id)
            if cursor_created_at is not None and cursor_session_id is not None:
                ordinary_query = ordinary_query.where(
                    _session_cursor_filter(cursor_created_at, cursor_session_id)
                )
            ordinary_capacity = limit - (1 if primary_session is not None else 0)
            ordinary_rows = list((await db.scalars(
                ordinary_query
                .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
                .limit(ordinary_capacity + 1)
            )).all())
            has_more = len(ordinary_rows) > ordinary_capacity
            ordinary_sessions = ordinary_rows[:ordinary_capacity]
            sessions = ([primary_session] if primary_session is not None else []) + ordinary_sessions
            cursor_anchor = ordinary_sessions[-1] if ordinary_sessions else None
        else:
            result = await db.execute(
                query
                .order_by(
                    ChatSession.is_primary.desc(),
                    ChatSession.last_message_at.desc().nulls_last(),
                    ChatSession.created_at.desc().nulls_last(),
                    ChatSession.id.desc(),
                )
                .offset(offset)
                .limit(limit + 1)
            )
            raw_sessions = list(result.scalars().all())
            has_more = len(raw_sessions) > limit
            sessions = raw_sessions[:limit]
        if hasattr(agent, "tenant_id"):
            sessions = await filter_tenant_safe_chat_sessions(
                db, list(sessions), agent.tenant_id
            )
        out = []

        # --- BULK FETCH: count total messages and unread messages in two compact queries ---
        session_ids = [str(s.id) for s in sessions]
        session_uuid_ids = [s.id for s in sessions]

        total_counts: dict[str, int] = {}
        unread_counts: dict[str, int] = {}
        if session_ids:
            counts_res = await db.execute(
                select(
                    ChatMessage.conversation_id,
                    func.count(ChatMessage.id)
                ).where(
                    ChatMessage.conversation_id.in_(session_ids),
                    ChatMessage.agent_id == agent_id
                ).group_by(ChatMessage.conversation_id)
            )
            for row in counts_res.all():
                total_counts[row[0]] = int(row[1] or 0)

            unread_res = await db.execute(
                select(ChatSession.id, func.count(ChatMessage.id))
                .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
                .where(
                    ChatSession.id.in_(session_uuid_ids),
                    ChatSession.is_group.is_(False),  # group last_read_at is shared; per-user unread undefined
                    ChatMessage.role.in_(["assistant", "system", "tool_call"]),
                    ChatMessage.created_at > func.coalesce(
                        ChatSession.last_read_at_by_user,
                        datetime(1970, 1, 1, tzinfo=tz.utc),
                    ),
                )
                .group_by(ChatSession.id)
            )
            for row in unread_res.all():
                unread_counts[str(row[0])] = int(row[1] or 0)

        for session in sessions:
            # Hide truly empty / orphan sessions. Onboarding sessions have zero
            # user messages (the agent greets first) but do have assistant
            # turns, so count ALL messages here — not just user ones.
            count = total_counts.get(str(session.id), 0)
            if count == 0 and not session.is_primary:
                continue
            out.append(SessionOut(
                id=str(session.id),
                agent_id=str(session.agent_id),
                user_id=str(session.user_id) if session.user_id else None,
                username=(session.group_name or session.title) if session.is_group else None,
                source_channel=session.source_channel,
                title=session.title,
                created_at=session.created_at.isoformat(),
                last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                message_count=count,
                unread_count=unread_counts.get(str(session.id), 0),
                is_primary=bool(session.is_primary),
                participant_type="group" if session.is_group else "user",
                is_group=bool(session.is_group),
                group_name=session.group_name,
            ))
        if paginated:
            next_cursor = (
                _encode_session_cursor(cursor_anchor, scope="mine", primary_id=primary_id)
                if use_cursor_pagination and has_more
                else None
            )
            return SessionPageOut(
                items=out,
                has_more=has_more,
                next_offset=None if use_cursor_pagination else (offset + limit if has_more else None),
                next_cursor=next_cursor,
            )
        return out


@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionDetailOut)
async def get_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get one accessible session so a URL can restore it without scanning a paged list."""
    _, session, view_scope = await _load_accessible_session(db, current_user, agent_id, session_id)
    return await _build_session_detail_out(db, session, view_scope, task_id=task_id)


@router.get("/{agent_id}/sessions/{session_id}/execution")
async def get_session_execution(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the latest execution provenance for an accessible conversation."""
    from app.models.trigger_execution import TriggerExecution

    await _load_accessible_session(db, current_user, agent_id, session_id)
    execution = (
        await db.execute(
            select(TriggerExecution)
            .where(TriggerExecution.conversation_id == session_id)
            .order_by(TriggerExecution.scheduled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if execution is None:
        return None
    return {
        "id": str(execution.id),
        "source": execution.source,
        "status": execution.status,
        "scheduled_at": execution.scheduled_at.isoformat(),
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        # Standard conversations are visible to session participants. Keep raw
        # provider/internal diagnostics on the manage-only execution endpoint.
        "last_error": "Execution failed. Open execution details for diagnostics."
        if execution.last_error
        else None,
    }


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID,
    body: CreateSessionIn = CreateSessionIn(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new chat session for the current user."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    source_channel = validate_platform_login_channel(body.source_channel)
    await lock_platform_sessions(db, agent_id, current_user.id, source_channel)

    now = datetime.now(tz.utc)
    new_id = uuid.uuid4()
    session = ChatSession(
        id=new_id,
        agent_id=agent_id,
        user_id=current_user.id,
        title=body.title or f"Session {now.strftime('%m-%d %H:%M')}",
        source_channel=source_channel,
        is_primary=False,
        created_at=now,
    )
    db.add(session)
    await db.flush()
    await promote_platform_session(db, session)
    await db.commit()
    await db.refresh(session)
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id) if session.user_id else None,
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=None,
        message_count=0,
        unread_count=0,
        is_primary=bool(session.is_primary),
        participant_type="user",
        is_group=False,
    )


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename a session. Owner, agent creator, or admin may rename others' sessions."""
    agent, agent_access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.source_channel == "subagent":
        await _load_accessible_session(db, current_user, agent_id, session_id)
        raise HTTPException(
            status_code=409,
            detail="Subagent sessions are runtime-owned and read-only; use stop_subagent.",
        )

    if str(session.user_id) != str(current_user.id) and not can_modify_other_agent_chat_sessions(current_user, agent, agent_access):
        raise HTTPException(status_code=403, detail="Not authorized")

    session.title = body.title
    await db.commit()
    return {"id": str(session.id), "title": session.title}


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a chat session and its messages. Owner, agent creator, or admin may delete others' sessions."""
    agent, agent_access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.source_channel == "subagent":
        await _load_accessible_session(db, current_user, agent_id, session_id)
        raise HTTPException(
            status_code=409,
            detail="Subagent sessions are runtime-owned and cannot be deleted.",
        )

    if str(session.user_id) != str(current_user.id) and not can_modify_other_agent_chat_sessions(current_user, agent, agent_access):
        raise HTTPException(status_code=403, detail="Not authorized")

    child_run = (
        await db.execute(
            select(SubagentRun.id)
            .where(SubagentRun.parent_session_id == session_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if child_run is not None:
        raise HTTPException(
            status_code=409,
            detail="Sessions with Subagent audit records cannot be deleted.",
        )

    if session.user_id is not None and not session.is_group:
        await lock_platform_sessions(db, session.agent_id, session.user_id, session.source_channel)
        await db.refresh(session)
    was_primary = bool(session.is_primary)
    owner_user_id = session.user_id
    owner_agent_id = session.agent_id
    owner_source_channel = session.source_channel

    # Delete associated messages first
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(ChatMessage).where(ChatMessage.conversation_id == str(session_id)))
    await db.delete(session)
    await db.flush()
    if was_primary and owner_user_id is not None and not session.is_group:
        successor = await get_latest_platform_session(
            db,
            owner_agent_id,
            owner_user_id,
            source_channel=owner_source_channel,
        )
        if successor:
            await promote_platform_session(db, successor)
    await db.commit()
    return None


async def _get_session_messages_page(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int,
    turn_limit: int | None,
    before: str | None,
    current_user: User,
    db: AsyncSession,
    response: Response | None,
):
    _chat_session_message_query._load_accessible_session = _load_accessible_session
    _chat_session_message_query._split_inline_tools = _split_inline_tools
    return await _chat_session_message_query._get_session_messages_page(
        agent_id, session_id, limit, turn_limit, before, current_user, db, response
    )


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=500, description="Number of messages to return"),
    before: str = Query(None, description="Cursor: ISO timestamp, optionally followed by |message UUID"),
    paginated: bool = Query(
        False,
        description="Return cursor metadata in the response body.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    """Legacy row-count pagination. Kept unchanged for existing clients."""
    rows = await _get_session_messages_page(
        agent_id=agent_id,
        session_id=session_id,
        limit=limit,
        turn_limit=None,
        before=before,
        current_user=current_user,
        db=db,
        response=response,
    )
    if paginated is not True:
        return rows
    return {
        "items": rows,
        "has_more": response.headers.get("X-Message-Has-More") == "true",
        "next_cursor": response.headers.get("X-Message-Next-Cursor") or None,
    }


@router.get("/{agent_id}/sessions/{session_id}/message-turns")
async def get_session_message_turns(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_limit: int = Query(20, ge=1, le=100, description="Number of complete turns to return"),
    before: str = Query(None, description="Cursor: ISO timestamp, optionally followed by |message UUID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    """Turn-boundary pagination for current PC and H5 clients."""
    return await _get_session_messages_page(
        agent_id=agent_id,
        session_id=session_id,
        limit=100,
        turn_limit=turn_limit,
        before=before,
        current_user=current_user,
        db=db,
        response=response,
    )

_split_inline_tools = _chat_session_message_query._split_inline_tools
