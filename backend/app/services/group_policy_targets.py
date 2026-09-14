"""Project existing conversation choices into stable group policy targets.

Reads never populate a discovery table. Rules and selected member references
are materialized only by an explicit policy save or authenticated IM ingress.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import String, case, func, or_, select

from app.models.agent_group import AgentGroup, AgentGroupMember
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.models.org import ChannelUserBinding
from app.services.group_policy_identity import GROUP_PREFIXES, external_group_from_route, stable_group_id, stable_member_id
from app.services.group_policy import installation_scope, scoped_bindings
from app.services.scene_targets import current_target_session, decode_target

async def group_for_target(db, agent, viewer, target_ref):
    identity = decode_target(target_ref, agent)
    if not identity.get("is_group") or identity.get("project_id"):
        raise HTTPException(422, "groupPolicy.unavailable")
    session = await current_target_session(db, agent, identity, viewer=viewer)
    if not session:
        raise HTTPException(422, "groupPolicy.unavailable")
    channel = session.source_channel
    scope = await installation_scope(db, agent, channel)
    if not scope:
        raise HTTPException(422, "groupPolicy.unavailable")
    bound = (session.im_config or {}).get("group_target_id")
    if bound:
        try:
            group_id = uuid.UUID(str(bound))
        except ValueError:
            raise HTTPException(422, "groupPolicy.unavailable") from None
        group = await db.scalar(select(AgentGroup).where(
            AgentGroup.id == group_id, AgentGroup.agent_id == agent.id,
            AgentGroup.tenant_id == agent.tenant_id, AgentGroup.channel == channel,
            AgentGroup.installation_scope == scope,
        ))
        if group:
            return group
    external_id = external_group_from_route(channel, session.external_conv_id)
    # Older threaded transports have no reliable parent identity in a Session.
    # Never silently promote a thread into a group policy target.
    if not external_id:
        raise HTTPException(422, "groupPolicy.unavailable")
    group = await db.scalar(select(AgentGroup).where(
        AgentGroup.agent_id == agent.id, AgentGroup.tenant_id == agent.tenant_id,
        AgentGroup.channel == channel, AgentGroup.installation_scope == scope,
        AgentGroup.external_group_id == external_id,
    ))
    return group or AgentGroup(id=stable_group_id(agent.id, channel, scope, external_id),
        agent_id=agent.id, tenant_id=agent.tenant_id, channel=channel,
        installation_scope=scope, external_group_id=external_id,
        name=session.group_name or session.title or "", rules=[], revision=0)


def group_sessions(group):
    prefix = GROUP_PREFIXES.get(group.channel)
    route = f"{prefix}{group.external_group_id}" if prefix else None
    matches = [ChatSession.im_config["group_target_id"].as_string() == str(group.id)]
    if route:
        matches.extend([ChatSession.external_conv_id == route,
            ChatSession.external_conv_id.startswith(f"{route}__archived_", autoescape=True)])
    return select(ChatSession.id).where(ChatSession.agent_id == group.agent_id,
        ChatSession.project_id.is_(None), ChatSession.is_group.is_(True),
        ChatSession.source_channel == group.channel, or_(*matches))


async def existing_members(db, group, *, q="", offset=0, selected=None):
    """One paginated projection of observed members and existing message senders."""
    if selected is not None and not selected:
        return []
    scoped = scoped_bindings(group).subquery()
    canonical_name = select(func.min(User.display_name)).select_from(User).join(scoped,
        scoped.c.user_id == User.id,
    ).where(User.tenant_id == group.tenant_id, scoped.c.subject == AgentGroupMember.subject,
        scoped.c.id_type == AgentGroupMember.subject_type,
    ).having(func.count(func.distinct(User.id)) == 1).correlate(AgentGroupMember).scalar_subquery()
    display = func.coalesce(func.nullif(canonical_name, ""), AgentGroupMember.name)
    recorded_query = select(AgentGroupMember, display).where(AgentGroupMember.group_id == group.id)
    if selected is not None:
        recorded_query = recorded_query.where(AgentGroupMember.id.in_(selected))
    if q.strip():
        recorded_query = recorded_query.where(display.icontains(q.strip(), autoescape=True))
    recorded_query = recorded_query.order_by(display, AgentGroupMember.subject_type, AgentGroupMember.subject)
    if selected is None:
        recorded_query = recorded_query.limit(offset + 51)
    recorded = [AgentGroupMember(id=item.id, group_id=item.group_id, subject=item.subject,
        subject_type=item.subject_type, name=name or "") for item, name in (await db.execute(recorded_query)).all()]
    sessions = group_sessions(group).subquery()
    participants = select(ChatMessage.user_id).join(sessions,
        ChatMessage.conversation_id == func.cast(sessions.c.id, String),
    ).where(ChatMessage.agent_id == group.agent_id, ChatMessage.role == "user").distinct()
    bindings = scoped_bindings(group).where(ChannelUserBinding.user_id.in_(participants)).subquery()
    priority = case({"sender_id": 0, "open_id": 0, "user_id": 0, "external_id": 0,
                     "staff_id": 1, "union_id": 2}, value=bindings.c.id_type, else_=9)
    ranked = select(bindings.c.id_type, bindings.c.subject, User.display_name.label("name"),
        AgentGroupMember.id.label("member_id"), func.row_number().over(
            partition_by=User.id, order_by=(priority, bindings.c.id),
        ).label("position"),
    ).select_from(User).join(bindings, User.id == bindings.c.user_id).outerjoin(AgentGroupMember,
        (AgentGroupMember.group_id == group.id) & (AgentGroupMember.subject == bindings.c.subject)
        & (AgentGroupMember.subject_type == bindings.c.id_type),
    ).where(User.tenant_id == group.tenant_id).subquery()
    candidates = select(ranked).where(ranked.c.position == 1)
    if q.strip():
        candidates = candidates.where(ranked.c.name.icontains(q.strip(), autoescape=True))
    candidates = candidates.order_by(ranked.c.name, ranked.c.id_type, ranked.c.subject)
    if selected is None:
        candidates = candidates.limit(offset + 51)
    # Two independently ordered prefixes suffice for the merged page; no full
    # group history or full participant roster is loaded for picker searches.
    members = {item.id: item for item in recorded}
    for row in (await db.execute(candidates)).mappings():
        member_id = row["member_id"] or stable_member_id(group.id, row["id_type"], row["subject"])
        if selected is None or member_id in selected:
            members[member_id] = AgentGroupMember(id=member_id, group_id=group.id,
                subject_type=row["id_type"], subject=row["subject"], name=row["name"] or "")
    values = sorted(members.values(), key=lambda item: ((item.name or ""), item.subject_type, item.subject))
    return values if selected is not None else values[offset:offset + 51]
