"""Chat session management API endpoints."""

import uuid
from datetime import datetime, timezone as tz
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    can_view_all_agent_chat_sessions,
    check_agent_access,
    filter_tenant_safe_chat_sessions,
    require_current_agent_tenant,
    require_tenant_safe_chat_session,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.agent import Agent
from app.models.user import Identity, User
from app.services.auth_code_exchange import validate_platform_login_channel

router = APIRouter(prefix="/api/agents", tags=["chat-sessions"])


# Single source of truth lives in app.core.permissions; aliased here so the
# existing call sites (and their "_" private-by-convention name) stay unchanged.
_can_view_all_agent_chat_sessions = can_view_all_agent_chat_sessions


class SessionOut(BaseModel):
    id: str
    agent_id: str
    user_id: Optional[str] = None
    username: Optional[str] = None      # display_name ?? username
    source_channel: str = "web"         # web / feishu / discord / slack / agent
    title: str
    created_at: str
    last_message_at: Optional[str] = None
    message_count: int = 0
    unread_count: int = 0
    is_primary: bool = False
    # Agent-to-agent session fields
    peer_agent_id: Optional[str] = None
    peer_agent_name: Optional[str] = None
    participant_type: str = "user"       # 'user' | 'agent'
    # Group chat session fields
    is_group: bool = False
    group_name: Optional[str] = None

    class Config:
        from_attributes = True


class SessionDetailOut(SessionOut):
    view_scope: Literal["mine", "all"]


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
    """Resolve one session and the web picker scope that can display it."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # Real ORM agents always expose tenant_id. A few isolated protocol-double
    # tests intentionally omit it and exercise unrelated response shaping.
    if hasattr(agent, "tenant_id"):
        await require_tenant_safe_chat_session(db, session, agent.tenant_id)

    is_owner = str(session.user_id) == str(current_user.id)
    is_privileged = _can_view_all_agent_chat_sessions(current_user, agent)
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

    if not (is_owner or is_privileged or is_group_member):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    source_channel = str(session.source_channel or "web").lower()
    view_scope: Literal["mine", "all"] = (
        "mine"
        if source_channel not in {"agent", "trigger"} and (is_owner or is_group_member)
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
    )


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: str = Query("mine", description="'mine' or 'all'"),
    source_channel: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions for an agent. scope=all for org/platform admins and agent_admin."""
    # Verify agent exists
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    source_channel = (source_channel or "").strip() or None
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    if scope == "all":
        if not _can_view_all_agent_chat_sessions(current_user, agent):
            raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

        # Fetch all sessions (including agent-to-agent where this agent is peer)
        all_where = (
            (ChatSession.agent_id == agent_id)
            | ((ChatSession.peer_agent_id == agent_id) & (ChatSession.source_channel == "agent"))
        )
        query = select(ChatSession).where(all_where)
        if source_channel:
            query = query.where(ChatSession.source_channel == source_channel)
        result = await db.execute(
            query
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        sessions = result.scalars().all()
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
                    ChatSession.is_group == False,
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
            if count == 0:
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
        return out

    else:  # scope == "mine"
        # Group membership signal: at least one user-role message authored by
        # the current user in this session. Mirrors P2P/group-chat client UX —
        # if you have spoken in the group, the conversation surfaces in your
        # own session list (you don't need admin scope=all to see it).
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
        query = (
            select(ChatSession)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.source_channel.notin_(["agent", "trigger"]),  # Exclude agent-to-agent and reflection sessions
                or_(
                    and_(
                        ChatSession.is_group == False,
                        ChatSession.user_id == current_user.id,
                    ),
                    and_(
                        ChatSession.is_group == True,
                        group_membership,
                    ),
                ),
            )
        )
        if source_channel:
            query = query.where(ChatSession.source_channel == source_channel)
        result = await db.execute(
            query
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        sessions = result.scalars().all()
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
                    ChatSession.is_group == False,  # group last_read_at is shared; per-user unread undefined
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
            if count == 0:
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
        return out


@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionDetailOut)
async def get_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get one accessible session so a URL can restore it without scanning a paged list."""
    _, session, view_scope = await _load_accessible_session(db, current_user, agent_id, session_id)
    return await _build_session_detail_out(db, session, view_scope)


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
        is_primary=False,
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
    agent, _ = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent):
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
    agent, _ = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Delete associated messages first
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(ChatMessage).where(ChatMessage.conversation_id == str(session_id)))
    await db.delete(session)
    await db.commit()
    return None


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=500, description="Number of messages to return"),
    before: str = Query(None, description="Cursor: return messages created before this timestamp (ISO format)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get chat messages for a specific session."""
    _, session, _ = await _load_accessible_session(db, current_user, agent_id, session_id)

    # Query messages by conversation_id only (agent-to-agent uses session_agent_id)
    # Optimized: use a single query with ORDER BY and LIMIT instead of subquery
    from sqlalchemy import desc
    query = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(session_id))
        # id tiebreak: own-transaction tool_call/assistant rows can share a
        # created_at microsecond; keep the render order deterministic. Fetched
        # desc + reversed below, so the page is the newest `limit` rows.
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(limit)
    )
    # Apply cursor filter if `before` timestamp is provided
    if before:
        from datetime import datetime as dt
        try:
            before_dt = dt.fromisoformat(before.replace('Z', '+00:00'))
            query = query.where(ChatMessage.created_at < before_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid `before` timestamp format. Use ISO 8601.")
    msgs_result = await db.execute(query)
    messages = list(reversed(msgs_result.scalars().all()))

    # Reading your own first-party/channel session should clear its unread state.
    if str(session.user_id) == str(current_user.id) and not session.is_group and session.source_channel not in ("agent", "trigger"):
        session.last_read_at_by_user = datetime.now(tz.utc)
        await db.commit()

    # Resolve canonical sender IDs and display names in batches. Participant is
    # consulted only as a one-release read bridge for legacy A2A rows; its ID is
    # never returned to callers.
    from app.models.agent import Agent
    from app.models.participant import Participant

    participant_ids = {m.participant_id for m in messages if m.participant_id}
    legacy_participants: dict[str, tuple[str, uuid.UUID, str]] = {}
    if participant_ids:
        p_result = await db.execute(
            select(Participant.id, Participant.type, Participant.ref_id, Participant.display_name)
            .where(Participant.id.in_(participant_ids))
        )
        legacy_participants = {
            str(pid): (ptype, ref_id, display_name or "Unknown")
            for pid, ptype, ref_id, display_name in p_result.all()
        }

    user_ids_seen: set[uuid.UUID] = set()
    agent_ids_seen: set[uuid.UUID] = set()
    for message in messages:
        sender_user_id = getattr(message, "sender_user_id", None)
        sender_agent_id = getattr(message, "sender_agent_id", None)
        if sender_user_id:
            user_ids_seen.add(sender_user_id)
        elif sender_agent_id:
            agent_ids_seen.add(sender_agent_id)
        elif message.participant_id and str(message.participant_id) in legacy_participants:
            participant_type, ref_id, _display = legacy_participants[str(message.participant_id)]
            if participant_type == "user":
                user_ids_seen.add(ref_id)
            elif participant_type == "agent":
                agent_ids_seen.add(ref_id)
        elif message.role == "user" and session.source_channel != "agent":
            legacy_user_id = getattr(message, "user_id", None) or session.user_id
            if legacy_user_id:
                user_ids_seen.add(legacy_user_id)
        elif message.role in {"assistant", "tool_call"}:
            agent_ids_seen.add(getattr(message, "agent_id", None) or agent_id)

    user_name_cache: dict[str, str] = {}
    needs_sender_names = bool(getattr(session, "is_group", False)) or session.source_channel == "agent"
    if needs_sender_names and user_ids_seen:
        u_rows = await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids_seen)))
        user_name_cache = {str(uid): (name or "Unknown") for uid, name in u_rows.all()}
    agent_name_cache: dict[str, str] = {}
    if needs_sender_names and agent_ids_seen:
        a_rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids_seen)))
        agent_name_cache = {str(aid): (name or "Unknown") for aid, name in a_rows.all()}

    out = []
    tool_call_positions: dict[str, int] = {}
    for m in messages:
        sender_user_id = getattr(m, "sender_user_id", None)
        sender_agent_id = getattr(m, "sender_agent_id", None)
        legacy_sender_name = None
        if not sender_user_id and not sender_agent_id and m.participant_id:
            legacy = legacy_participants.get(str(m.participant_id))
            if legacy:
                participant_type, ref_id, legacy_sender_name = legacy
                if participant_type == "user":
                    sender_user_id = ref_id
                elif participant_type == "agent":
                    sender_agent_id = ref_id
        if not sender_user_id and not sender_agent_id:
            if m.role == "user" and session.source_channel != "agent":
                sender_user_id = getattr(m, "user_id", None) or session.user_id
            elif m.role in {"assistant", "tool_call"}:
                sender_agent_id = getattr(m, "agent_id", None) or agent_id
        sender_name = legacy_sender_name
        if sender_user_id:
            sender_name = user_name_cache.get(str(sender_user_id), sender_name)
        elif sender_agent_id:
            sender_name = agent_name_cache.get(str(sender_agent_id), sender_name)

        if m.role == "tool_call":
            from app.services.chat_history import parse_tool_call_for_display
            entry: dict = {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
            # Canonical tool events persist the model call_id, shared by their append-only
            # running/done rows. Pending confirmation rows intentionally have no call_id;
            # their database row id remains the resolve handle.
            entry["toolCallId"] = str(m.id)
            parsed = parse_tool_call_for_display(m.content)
            if parsed:
                entry["content"] = ""
                entry.update(parsed)
            if entry.get("toolName") == "request_confirmation":
                entry["toolCallId"] = str(m.id)
            if sender_name:
                entry["sender_name"] = sender_name
            if sender_user_id:
                entry["sender_user_id"] = str(sender_user_id)
            if sender_agent_id:
                entry["sender_agent_id"] = str(sender_agent_id)
            tool_call_id = entry["toolCallId"]
            previous_position = tool_call_positions.get(tool_call_id)
            if previous_position is None:
                tool_call_positions[tool_call_id] = len(out)
                out.append(entry)
            else:
                previous_entry = out[previous_position]
                if previous_entry.get("toolStatus") == "done" and entry.get("toolStatus") == "running":
                    continue
                # Keep the logical call at its original timeline position while replacing
                # the durable running marker with the latest status/result.
                entry["created_at"] = previous_entry.get("created_at") or entry["created_at"]
                out[previous_position] = entry
            continue

        # For agent sessions, parse inline tool_code blocks from assistant messages
        if session.source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            parts = _split_inline_tools(m.content)
            for part in parts:
                if sender_name:
                    part["sender_name"] = sender_name
                if sender_user_id:
                    part["sender_user_id"] = str(sender_user_id)
                if sender_agent_id:
                    part["sender_agent_id"] = str(sender_agent_id)
                out.append(part)
        else:
            entry = {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
            if hasattr(m, 'thinking') and m.thinking:
                entry["thinking"] = m.thinking
            if sender_name:
                entry["sender_name"] = sender_name
            if sender_user_id:
                entry["sender_user_id"] = str(sender_user_id)
            if sender_agent_id:
                entry["sender_agent_id"] = str(sender_agent_id)
            out.append(entry)

    # NB: confirmation cards are NOT merged here any more — a card is just a
    # `request_confirmation` tool_call row, already returned as a normal tool_call
    # message above (the frontend renders that specific tool as the card).

    return out


import re

def _split_inline_tools(content: str) -> list[dict]:
    """Parse assistant content containing inline ```tool_code blocks.

    Splits into alternating text segments and tool_call entries.
    Format: ```tool_code\ntool_name\n``` ```json\n{args}\n```
    """
    # Pattern: ```tool_code\n<name>\n``` optionally followed by ```json\n<args>\n```
    pattern = re.compile(
        r'```tool_code\s*\n\s*(\w+)\s*\n```'        # tool name
        r'(?:\s*```json\s*\n(.*?)\n```)?',            # optional JSON args
        re.DOTALL
    )

    parts: list[dict] = []
    last_end = 0

    for match in pattern.finditer(content):
        # Text before this tool call
        text_before = content[last_end:match.start()].strip()
        if text_before:
            parts.append({"role": "assistant", "content": text_before})

        tool_name = match.group(1)
        args_str = match.group(2)
        tool_args = None
        if args_str:
            try:
                import json
                tool_args = json.loads(args_str.strip())
            except Exception:
                tool_args = {"raw": args_str.strip()}

        parts.append({
            "role": "tool_call",
            "content": "",
            "toolName": tool_name,
            "toolArgs": tool_args,
            "toolStatus": "done",
            "toolResult": "",
        })
        last_end = match.end()

    # Trailing text after last tool
    trailing = content[last_end:].strip()
    if trailing:
        parts.append({"role": "assistant", "content": trailing})

    # If no matches found, return the whole content as-is
    if not parts:
        parts.append({"role": "assistant", "content": content})

    return parts
