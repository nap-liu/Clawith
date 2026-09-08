"""Tenant-safe stable conversation targets and current-session resolution."""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select

from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.project import Project, ProjectMemberSnapshot
from app.models.user import Identity, User
from app.services.channel_user_service import channel_user_service
from app.services.project_service_access import accessible_projects_clause, require_project

PLATFORM_CHANNELS = frozenset({"web", "miniprogram", "wechat_miniprogram"})
INTERNAL_CHANNELS = frozenset({"agent", "trigger", "subagent"})


def encode_target(identity: dict) -> str:
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def decode_target(ref: str, agent: Agent) -> dict:
    try:
        identity = json.loads(ref)
        if identity.get("agent_id") != str(agent.id) or identity.get("tenant_id") != str(agent.tenant_id):
            raise ValueError
        if not isinstance(identity.get("source_channel"), str) or type(identity.get("is_group")) is not bool:
            raise ValueError
        for field in ("project_id", "user_id"):
            if identity.get(field):
                uuid.UUID(identity[field])
        return identity
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(422, detail="sceneAuto.targetUnavailable") from None


async def session_target_identity(db, agent: Agent, session: ChatSession) -> dict | None:
    if not agent.tenant_id or session.source_channel in INTERNAL_CHANNELS:
        return None
    if session.project_id:
        member = await db.scalar(select(ProjectMemberSnapshot.id).join(Project).where(
            ProjectMemberSnapshot.project_id == session.project_id,
            ProjectMemberSnapshot.agent_id == agent.id,
            ProjectMemberSnapshot.is_enabled.is_(True),
            Project.tenant_id == agent.tenant_id,
        ))
        if not member or (not session.is_group and session.agent_id != agent.id):
            return None
    elif session.agent_id != agent.id:
        return None
    identity = {
        "tenant_id": str(agent.tenant_id), "agent_id": str(agent.id),
        "source_channel": session.source_channel, "is_group": bool(session.is_group),
        "project_id": str(session.project_id) if session.project_id else None,
        "user_id": str(session.user_id) if not session.is_group and session.user_id else None,
    }
    if not session.is_group:
        user = await db.scalar(select(User.id).outerjoin(Identity, User.identity_id == Identity.id).where(
            User.id == session.user_id, User.tenant_id == agent.tenant_id,
            User.is_active.is_(True), or_(Identity.id.is_(None), Identity.is_active.is_(True)),
        ))
        if not user:
            return None
    if session.project_id or session.source_channel in PLATFORM_CHANNELS:
        return identity
    route = str(session.external_conv_id or "")
    if not route or "__archived_" in route:
        return None
    config_type = "microsoft_teams" if session.source_channel == "teams" else session.source_channel
    config = await db.scalar(select(ChannelConfig).where(
        ChannelConfig.agent_id == agent.id, ChannelConfig.channel_type == config_type,
        ChannelConfig.is_configured.is_(True),
    ))
    if config is None:
        return None
    scope = await channel_user_service.resolve_installation_scope(db, agent, session.source_channel)
    identity.update({"external_conv_id": route, "channel_config_id": str(config.id), "installation_scope": scope})
    return identity


async def current_target_session(db, agent: Agent, identity: dict, *, viewer: User | None = None):
    if identity.get("tenant_id") != str(agent.tenant_id) or identity.get("agent_id") != str(agent.id):
        return None
    project_id = identity.get("project_id")
    conditions = [
        ChatSession.source_channel == identity["source_channel"],
        ChatSession.is_group.is_(identity["is_group"]),
    ]
    if project_id:
        if viewer is not None:
            await require_project(db, viewer, uuid.UUID(project_id), edit=True)
        conditions.append(ChatSession.project_id == uuid.UUID(project_id))
        if not identity["is_group"]:
            conditions.append(ChatSession.agent_id == agent.id)
    else:
        conditions.extend([ChatSession.agent_id == agent.id, ChatSession.project_id.is_(None)])
    if identity.get("user_id"):
        conditions.append(ChatSession.user_id == uuid.UUID(identity["user_id"]))
    if identity.get("external_conv_id"):
        conditions.append(ChatSession.external_conv_id == identity["external_conv_id"])
    session = await db.scalar(select(ChatSession).where(*conditions).order_by(
        ChatSession.created_at.desc(), ChatSession.id.desc(),
    ).limit(1))
    if session is None or await session_target_identity(db, agent, session) != identity:
        return None
    return session


async def conversation_options(db, agent: Agent, viewer: User, *, kind="all", q="", channel=None, offset=0, limit=50):
    project_member = exists(select(ProjectMemberSnapshot.id).join(Project).where(
        ProjectMemberSnapshot.project_id == ChatSession.project_id,
        ProjectMemberSnapshot.agent_id == agent.id,
        ProjectMemberSnapshot.is_enabled.is_(True),
        accessible_projects_clause(viewer, edit=True),
    ))
    label = func.coalesce(ChatSession.group_name, User.display_name, ChatSession.title)
    filters = [
        or_(and_(ChatSession.agent_id == agent.id, ChatSession.project_id.is_(None)),
            and_(ChatSession.project_id.is_not(None), project_member,
                 or_(ChatSession.is_group.is_(True), ChatSession.agent_id == agent.id))),
        ChatSession.source_channel.notin_(INTERNAL_CHANNELS),
        or_(ChatSession.external_conv_id.is_(None), ~ChatSession.external_conv_id.contains("__archived_", autoescape=True)),
        or_(ChatSession.is_group.is_(True), and_(User.tenant_id == agent.tenant_id, User.is_active.is_(True))),
    ]
    if kind != "all":
        filters.append(ChatSession.is_group.is_(kind == "group"))
    if channel:
        filters.append(ChatSession.source_channel == channel)
    # Rank before search/pagination: an old title may not promote a historical session.
    ranked = select(ChatSession.id.label("id"), label.label("label"), func.row_number().over(
        partition_by=(ChatSession.source_channel, ChatSession.project_id, ChatSession.is_group,
                      ChatSession.user_id, ChatSession.external_conv_id),
        order_by=(ChatSession.created_at.desc(), ChatSession.id.desc()),
    ).label("position")).outerjoin(User, ChatSession.user_id == User.id).where(*filters).subquery()
    query = select(ChatSession, ranked.c.label).join(ranked, ranked.c.id == ChatSession.id).where(ranked.c.position == 1)
    if q.strip():
        query = query.where(ranked.c.label.icontains(q.strip(), autoescape=True))
    rows = (await db.execute(query.order_by(ranked.c.label, ChatSession.id).offset(offset).limit(limit + 1))).all()
    items = []
    for session, name in rows[:limit]:
        identity = await session_target_identity(db, agent, session)
        if identity is None or session.context_terminated_reason:
            continue
        items.append({
            "target_ref": encode_target(identity), "label": str(name or "")[:200],
            "is_group": session.is_group, "source_channel": session.source_channel,
            "current_session_id": str(session.id),
            "available": not bool(session.context_terminated_reason),
        })
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
