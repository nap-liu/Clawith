"""Project the shared conversation source into distinct stable groups before paging."""

from sqlalchemy import String, and_, case, func, or_, select

from app.models.agent_group import AgentGroup
from app.models.chat_session import ChatSession
from app.services.group_policy_identity import GROUP_PREFIXES
from app.services.scene_targets import conversation_candidates, encode_target, session_target_identity


async def group_conversation_options(db, agent, viewer, scopes, *, q="", channel="", offset=0, limit=50):
    channels = [item for item, scope in scopes.items() if scope]
    bound_id = ChatSession.im_config["group_target_id"].as_string()
    bound_current = and_(AgentGroup.agent_id == agent.id, AgentGroup.tenant_id == agent.tenant_id,
        AgentGroup.channel == ChatSession.source_channel, or_(*[
            and_(AgentGroup.channel == item, AgentGroup.installation_scope == scopes[item]) for item in channels
        ])) if channels else False
    native_route = case(*[
        (and_(ChatSession.source_channel == item, ChatSession.external_conv_id.startswith(prefix)),
         func.substr(ChatSession.external_conv_id, len(prefix) + 1))
        for item, prefix in GROUP_PREFIXES.items()
    ], else_=None)
    # Invalid/stale bindings and parentless legacy threads cannot become choices.
    external_id = case((bound_current, AgentGroup.external_group_id),
                       (bound_id.is_(None), native_route), else_=None)
    source = conversation_candidates(agent, viewer, kind="group", channel=channel or None,
        channels=channels, include_projects=False).outerjoin(
            AgentGroup, func.cast(AgentGroup.id, String) == bound_id,
        ).add_columns(external_id.label("external_group_id"), func.row_number().over(
            partition_by=(ChatSession.source_channel, external_id),
            order_by=(ChatSession.created_at.desc(), ChatSession.id.desc()),
        ).label("position")).where(external_id.is_not(None), external_id != "").subquery()
    query = select(ChatSession, source.c.label).join(source, source.c.id == ChatSession.id).where(source.c.position == 1)
    if q.strip():
        query = query.where(source.c.label.icontains(q.strip(), autoescape=True))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(source.c.label, ChatSession.id).offset(offset).limit(limit + 1))).all()
    items = []
    for session, label in rows[:limit]:
        identity = await session_target_identity(db, agent, session)
        if identity is not None:
            items.append({"target_ref": encode_target(identity), "label": str(label or "")[:200]})
    return {"items": items, "total": total, "next_offset": offset + limit if len(rows) > limit else None}
