"""Agent managers edit multiple participant rules independently for each group."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent_group import AgentGroup, AgentGroupMember
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.user import User
from app.schemas.group_policy import GroupPolicyWrite
from app.services.group_policy import SUPPORTED_CHANNELS, installation_scope, scoped_bindings, observe_group
from app.services.group_policy_targets import existing_members, group_for_target
from app.services.group_policy_directory import group_conversation_options
from app.services.user_output import sanitize_user_visible_text

router = APIRouter(prefix="/agents/{agent_id}/group-policy", tags=["group-policy"])


async def require_manager(db, user, agent_id):
    agent, level = await check_agent_access(db, user, agent_id)
    if agent.tenant_id != user.tenant_id or agent.scope != "standard":
        raise HTTPException(404, "groupPolicy.unavailable")
    if level != "manage":
        raise HTTPException(403, "groupPolicy.forbidden")
    return agent


async def require_group(db, agent, group_id, *, lock=False, target_ref="", viewer=None):
    query = select(AgentGroup).where(AgentGroup.id == group_id,
        AgentGroup.agent_id == agent.id, AgentGroup.tenant_id == agent.tenant_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    group = await db.scalar(query)
    if not group and target_ref:
        group = await group_for_target(db, agent, viewer, target_ref)
        if group.id != group_id:
            raise HTTPException(409, "groupPolicy.conflict")
        if lock:
            group = await observe_group(db, agent, group.channel, group.installation_scope,
                                        group.external_group_id, group.name)
    if not group:
        raise HTTPException(404, "groupPolicy.unavailable")
    return group


def serialize_group(group, scope, name=None):
    return {"id": str(group.id), "name": name if name is not None else group.name, "channel": group.channel,
            "available": scope == group.installation_scope, "rule_count": len(group.rules),
            "enabled_count": sum(rule.get("enabled", True) for rule in group.rules)}


def serialize_member(member):
    return {"id": str(member.id), "name": member.name}


async def member_labels(db, group, members):
    result = {str(member.id): serialize_member(member) for member in members}
    bindings = scoped_bindings(group).subquery()
    rows = await db.execute(select(AgentGroupMember.id, User.id, User.display_name).join(bindings,
        (bindings.c.subject == AgentGroupMember.subject) & (bindings.c.id_type == AgentGroupMember.subject_type),
    ).join(User, User.id == bindings.c.user_id).where(AgentGroupMember.group_id == group.id,
        AgentGroupMember.id.in_([member.id for member in members]), User.tenant_id == group.tenant_id))
    names = {}
    for member_id, user_id, name in rows:
        names.setdefault(str(member_id), {})[user_id] = name
    for member_id, users in names.items():
        if len(users) == 1:
            result[member_id]["name"] = next(iter(users.values())) or result[member_id]["name"]
    return list(result.values())


def group_name_expression():
    label = select(ChatSession.group_name).where(
        ChatSession.agent_id == AgentGroup.agent_id, ChatSession.is_group.is_(True),
        ChatSession.im_config["group_target_id"].as_string() == func.cast(AgentGroup.id, String),
        ChatSession.group_name.is_not(None),
        *[~ChatSession.group_name.startswith(prefix) for prefix in (
            "Feishu Group ", "WeCom Group ", "Slack Channel ", "Discord Channel ", "Teams Group ", "DingTalk Group ",
        )],
    ).order_by(ChatSession.created_at.desc()).limit(1).correlate(AgentGroup).scalar_subquery()
    return func.coalesce(func.nullif(AgentGroup.name, ""), label, "")


async def policy_response(db, agent, group, target_ref=""):
    name = group.name or await db.scalar(select(group_name_expression()).where(AgentGroup.id == group.id))
    selected = {uuid.UUID(value) for rule in group.rules for value in rule.get("member_ids", [])}
    members = await existing_members(db, group, selected=selected)
    bindings = scoped_bindings(group).subquery()
    linked = await db.execute(select(AgentGroupMember.id, User.id).join(bindings,
        (bindings.c.subject == AgentGroupMember.subject) & (bindings.c.id_type == AgentGroupMember.subject_type),
    ).join(User, User.id == bindings.c.user_id).where(AgentGroupMember.group_id == group.id,
        AgentGroupMember.id.in_(selected), User.tenant_id == group.tenant_id))
    identities = {}
    for member_id, user_id in linked:
        identities.setdefault(str(member_id), set()).add(str(user_id))
    canonical = {member_id: next(iter(users)) for member_id, users in identities.items() if len(users) == 1}
    # Present existing verified selections in the same organization picker.
    # Unbound legacy subjects remain explicit selections until removed by a manager.
    rules = [{**rule,
        "member_ids": [value for value in rule.get("member_ids", []) if value not in canonical],
        "user_ids": sorted(set(rule.get("user_ids", [])) | {
            canonical[value] for value in rule.get("member_ids", []) if value in canonical}),
    } for rule in group.rules]
    user_ids = {uuid.UUID(value) for rule in rules for value in rule["user_ids"]}
    users = await db.scalars(select(User).where(User.tenant_id == group.tenant_id, User.id.in_(user_ids)))
    return {"group": {**serialize_group(group, await installation_scope(db, agent, group.channel), name), "target_ref": target_ref},
            "revision": group.revision, "rules": rules,
            "users": [{"id": str(item.id), "name": item.display_name, "avatar_url": item.avatar_url} for item in users],
            "members": await member_labels(db, group, members)}


@router.get("/groups")
async def list_groups(agent_id: uuid.UUID, q: str = Query("", max_length=200),
                      channel: str = Query("", max_length=20), offset: int = Query(0, ge=0),
                      db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    agent = await require_manager(db, user, agent_id)
    scopes = {item: await installation_scope(db, agent, item) for item in SUPPORTED_CHANNELS}
    channels = sorted(item for item, scope in scopes.items() if scope)
    choices = await group_conversation_options(db, agent, user, scopes, q=q, channel=channel, offset=offset)
    items = {}
    for choice in choices["items"]:
        try:
            group = await group_for_target(db, agent, user, choice["target_ref"])
        except HTTPException:
            continue
        items[str(group.id)] = {**serialize_group(group, scopes.get(group.channel), choice["label"]),
                                "target_ref": choice["target_ref"]}
    return {"items": list(items.values()), "total": choices["total"], "channels": channels,
            "next_offset": choices["next_offset"]}


@router.get("/groups/{group_id}")
async def get_policy(agent_id: uuid.UUID, group_id: uuid.UUID, target_ref: str = Query("", max_length=4000),
                     db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    agent = await require_manager(db, user, agent_id)
    return await policy_response(db, agent, await require_group(db, agent, group_id, target_ref=target_ref, viewer=user), target_ref)


@router.put("/groups/{group_id}")
async def save_policy(agent_id: uuid.UUID, group_id: uuid.UUID, data: GroupPolicyWrite,
                      db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    agent = await require_manager(db, user, agent_id)
    group = await require_group(db, agent, group_id, lock=True, target_ref=data.target_ref, viewer=user)
    if data.expected_revision != group.revision:
        raise HTTPException(409, "groupPolicy.conflict")
    if group.installation_scope != await installation_scope(db, agent, group.channel):
        raise HTTPException(422, "groupPolicy.unavailable")
    requested_users = {value for rule in data.rules for value in rule.user_ids}
    valid_users = set(await db.scalars(select(User.id).where(
        User.tenant_id == agent.tenant_id, User.id.in_(requested_users), User.is_active.is_(True),
    )))
    if requested_users != valid_users:
        raise HTTPException(422, "groupPolicy.invalidMembers")
    requested = {value for rule in data.rules for value in rule.member_ids}
    candidates = await existing_members(db, group, selected=requested)
    if requested != {member.id for member in candidates}:
        raise HTTPException(422, "groupPolicy.invalidMembers")
    for member in candidates:
        # Explicit save materializes selected references only, with no history copy.
        await db.merge(member)
    rules = [rule.model_dump(mode="json") for rule in data.rules]
    for rule in rules:
        rule["name"] = sanitize_user_visible_text(rule["name"])
        if not rule["name"].strip():
            raise HTTPException(422, "groupPolicy.invalidRule")
    before = group.rules
    group.rules = rules
    group.revision += 1
    db.add(AuditLog(agent_id=agent.id, user_id=user.id, action="group_policy_updated", details={
        "tenant_id": str(agent.tenant_id), "group_id": str(group.id), "revision": group.revision,
        "before": before, "rules": rules,
    }))
    await db.flush()
    result = await policy_response(db, agent, group, data.target_ref)
    await db.commit()
    return result


@router.get("/groups/{group_id}/members")
async def list_members(agent_id: uuid.UUID, group_id: uuid.UUID, q: str = Query("", max_length=200),
                       offset: int = Query(0, ge=0), target_ref: str = Query("", max_length=4000), db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    agent = await require_manager(db, user, agent_id)
    group = await require_group(db, agent, group_id, target_ref=target_ref, viewer=user)
    rows = await existing_members(db, group, q=q, offset=offset)
    return {"items": await member_labels(db, group, rows[:50]),
            "next_offset": offset + 50 if len(rows) > 50 else None}
