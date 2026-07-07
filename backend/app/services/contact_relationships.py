"""Agent-facing contact search and relationship management helpers."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    build_visible_agents_query,
    evaluate_agent_relationship_status,
    evaluate_human_relationship_status,
    is_agent_expired,
)
from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.models.org import AgentAgentRelationship, AgentRelationship, OrgMember
from app.models.user import User


CONTACT_RELATIONS = {
    "direct_leader",
    "collaborator",
    "stakeholder",
    "team_member",
    "subordinate",
    "mentor",
    "other",
    "peer",
    "supervisor",
    "assistant",
}


def _parse_target_id(target_id: str | uuid.UUID | None) -> uuid.UUID | None:
    if isinstance(target_id, uuid.UUID):
        return target_id
    try:
        return uuid.UUID(str(target_id or "").strip())
    except (TypeError, ValueError):
        return None


def _normalize_target_type(target_type: str | None) -> str | None:
    value = (target_type or "").strip().lower()
    return value if value in {"human", "agent"} else None


async def _load_source_agent(db: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    result = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))
    return result.scalar_one_or_none()


async def _load_actor_user(db: AsyncSession, user_id: uuid.UUID | None) -> User | None:
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))  # noqa: E712
    return result.scalar_one_or_none()


def _clean_limit(limit: int | None) -> int:
    try:
        value = int(limit or 20)
    except (TypeError, ValueError):
        return 20
    return max(1, min(value, 50))


def _normalize_relation(relation: str | None) -> str:
    value = (relation or "collaborator").strip()
    return value if value in CONTACT_RELATIONS else "collaborator"


def _provider_type(value: str | None) -> str | None:
    if not value:
        return None
    return "teams" if value == "microsoft_teams" else value


def _is_external_channel(channel: str | None) -> bool:
    return bool(channel and channel not in {"web", "platform"})


def _human_send_hint(name: str, channel: str | None) -> str:
    if channel in {"feishu", "dingtalk", "wecom", "slack", "teams", "wechat"}:
        return f'添加后使用 send_channel_message(member_name="{name}", channel="{channel}", ...)'
    return f'添加后使用 send_platform_message(username="{name}", ...)'


def _agent_send_hint(name: str) -> str:
    return f'添加后使用 send_message_to_agent(agent_name="{name}", ...)'


async def _human_relationship_status(
    db: AsyncSession,
    source_agent: Agent,
    member: OrgMember,
) -> str:
    result = await db.execute(
        select(AgentRelationship).where(
            AgentRelationship.agent_id == source_agent.id,
            AgentRelationship.member_id == member.id,
        )
    )
    rel = result.scalar_one_or_none()
    if not rel:
        return "not_added"
    rel.member = member
    status = await evaluate_human_relationship_status(db, rel, source_agent=source_agent)
    return status["access_status"]


async def _agent_relationship_status(
    db: AsyncSession,
    source_agent: Agent,
    target_agent: Agent,
    current_user_id: uuid.UUID | None,
) -> str:
    result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.agent_id == source_agent.id,
            AgentAgentRelationship.target_agent_id == target_agent.id,
        )
    )
    rel = result.scalar_one_or_none()
    if not rel:
        return "not_added"
    rel.target_agent = target_agent
    status = await evaluate_agent_relationship_status(db, rel, current_user_id=current_user_id)
    return status["access_status"]


async def _member_available_to_agent(db: AsyncSession, source_agent: Agent, member: OrgMember) -> bool:
    return member.tenant_id == source_agent.tenant_id and member.status == "active"


async def search_contacts_for_agent(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    query: str,
    contact_type: str | None = None,
    current_user_id: uuid.UUID | None = None,
    limit: int | None = 20,
) -> list[dict]:
    """Search human org members and visible agents for relationship creation."""
    source_agent = await _load_source_agent(db, agent_id)
    if not source_agent or not source_agent.tenant_id:
        return []

    search_text = (query or "").strip()
    if not search_text:
        return []

    wanted = (contact_type or "all").strip().lower()
    max_rows = _clean_limit(limit)
    results: list[dict] = []

    if wanted in {"all", "human"}:
        pattern = f"%{search_text}%"
        human_query = (
            select(
                OrgMember,
                IdentityProvider.name.label("provider_name"),
                IdentityProvider.provider_type.label("provider_type"),
            )
            .outerjoin(IdentityProvider, OrgMember.provider_id == IdentityProvider.id)
            .where(
                OrgMember.tenant_id == source_agent.tenant_id,
                OrgMember.status == "active",
                or_(
                    OrgMember.name.ilike(pattern),
                    OrgMember.name_translit_full.ilike(pattern),
                    OrgMember.name_translit_initial.ilike(pattern),
                    OrgMember.email.ilike(pattern),
                    OrgMember.department_path.ilike(pattern),
                    OrgMember.phone == search_text,
                ),
            )
            .order_by(OrgMember.name)
            .limit(max_rows)
        )
        human_rows = (await db.execute(human_query)).all()
        external_rows = []
        platform_rows_by_user: dict[uuid.UUID, tuple[OrgMember, str | None, str | None]] = {}
        unlinked_rows = []
        for member, _provider_name, raw_provider_type in human_rows:
            if not await _member_available_to_agent(db, source_agent, member):
                continue
            channel = _provider_type(raw_provider_type)
            if member.user_id and _is_external_channel(channel):
                external_rows.append((member, _provider_name, channel))
            elif member.user_id:
                platform_rows_by_user.setdefault(member.user_id, (member, _provider_name, channel))
            else:
                unlinked_rows.append((member, _provider_name, channel))

        external_user_ids = {member.user_id for member, _provider_name, _channel in external_rows if member.user_id}
        selected_human_rows = [
            *external_rows,
            *unlinked_rows,
            *[
                row
                for user_id, row in platform_rows_by_user.items()
                if user_id not in external_user_ids
            ],
        ]

        for member, _provider_name, channel in selected_human_rows:
            results.append(
                {
                    "id": str(member.id),
                    "type": "human",
                    "name": member.name,
                    "title": member.title or "",
                    "channel": channel or "platform",
                    "department_path": member.department_path or "",
                    "phone": member.phone or "",
                    "relationship_status": await _human_relationship_status(db, source_agent, member),
                    "send_hint": _human_send_hint(member.name, channel),
                }
            )

    if wanted in {"all", "agent"} and len(results) < max_rows:
        actor = await _load_actor_user(db, current_user_id)
        if actor:
            agent_query = build_visible_agents_query(actor, tenant_id=source_agent.tenant_id)
        else:
            agent_query = select(Agent).where(
                Agent.tenant_id == source_agent.tenant_id,
                Agent.is_deleted.is_(False),
                Agent.access_mode == "company",
            )
            current_user_id = source_agent.creator_id
        pattern = f"%{search_text}%"
        agent_query = (
            agent_query.where(
                Agent.id != source_agent.id,
                or_(
                    Agent.name.ilike(pattern),
                    Agent.role_description.ilike(pattern),
                ),
            )
            .order_by(Agent.created_at.desc())
            .limit(max_rows - len(results))
        )
        agents = (await db.execute(agent_query)).scalars().all()
        for target in agents:
            if target.tenant_id != source_agent.tenant_id:
                continue
            if target.status in ("stopped", "error") or is_agent_expired(target):
                continue
            results.append(
                {
                    "id": str(target.id),
                    "type": "agent",
                    "name": target.name,
                    "role_description": target.role_description or "",
                    "relationship_status": await _agent_relationship_status(
                        db,
                        source_agent,
                        target,
                        current_user_id,
                    ),
                    "send_hint": _agent_send_hint(target.name),
                }
            )

    return results[:max_rows]


async def _add_human_contact(
    db: AsyncSession,
    source_agent: Agent,
    member_id: uuid.UUID,
    *,
    relation: str,
    description: str,
    current_user_id: uuid.UUID | None,
) -> dict:
    member_result = await db.execute(select(OrgMember).where(OrgMember.id == member_id))
    member = member_result.scalar_one_or_none()
    if not member or not await _member_available_to_agent(db, source_agent, member):
        return {"status": "error", "reason": "contact_not_available"}

    existing_result = await db.execute(
        select(AgentRelationship).where(
            AgentRelationship.agent_id == source_agent.id,
            AgentRelationship.member_id == member.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    status = "already_added" if existing else "added"
    rel = existing or AgentRelationship(
        agent_id=source_agent.id,
        member_id=member.id,
        created_by_user_id=current_user_id,
    )
    rel.relation = relation
    rel.description = description
    rel.updated_by_user_id = current_user_id
    if existing:
        rel.updated_at = datetime.now(timezone.utc)
    else:
        db.add(rel)
        await db.flush()

    rel.member = member
    status_info = await evaluate_human_relationship_status(db, rel, source_agent=source_agent)
    channel = None
    if member.provider_id:
        provider_result = await db.execute(
            select(IdentityProvider.provider_type).where(IdentityProvider.id == member.provider_id)
        )
        channel = _provider_type(provider_result.scalar_one_or_none())
    return {
        "status": status,
        "id": str(member.id),
        "type": "human",
        "name": member.name,
        "relationship_status": status_info["access_status"],
        "send_hint": _human_send_hint(member.name, channel),
    }


async def _add_agent_contact(
    db: AsyncSession,
    source_agent: Agent,
    target_id: uuid.UUID,
    *,
    relation: str,
    description: str,
    current_user_id: uuid.UUID | None,
) -> dict:
    target_result = await db.execute(
        select(Agent).where(
            Agent.id == target_id,
            Agent.tenant_id == source_agent.tenant_id,
            Agent.is_deleted.is_(False),
        )
    )
    target = target_result.scalar_one_or_none()
    if (
        not target
        or target.id == source_agent.id
        or target.status in ("stopped", "error")
        or is_agent_expired(target)
    ):
        return {"status": "error", "reason": "contact_not_available"}

    actor = await _load_actor_user(db, current_user_id)
    if actor:
        visible_result = await db.execute(
            build_visible_agents_query(actor, tenant_id=source_agent.tenant_id).where(Agent.id == target.id)
        )
        if visible_result.scalar_one_or_none() is None:
            return {"status": "error", "reason": "contact_not_available"}
    elif target.access_mode != "company":
        return {"status": "error", "reason": "contact_not_available"}

    existing_result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.agent_id == source_agent.id,
            AgentAgentRelationship.target_agent_id == target.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    status = "already_added" if existing else "added"
    rel = existing or AgentAgentRelationship(
        agent_id=source_agent.id,
        target_agent_id=target.id,
        created_by_user_id=current_user_id,
    )
    rel.relation = relation
    rel.description = description
    rel.updated_by_user_id = current_user_id
    if existing:
        rel.updated_at = datetime.now(timezone.utc)
    else:
        db.add(rel)
        await db.flush()

    rel.target_agent = target
    status_info = await evaluate_agent_relationship_status(db, rel, current_user_id=current_user_id)
    return {
        "status": status,
        "id": str(target.id),
        "type": "agent",
        "name": target.name,
        "relationship_status": status_info["access_status"],
        "send_hint": _agent_send_hint(target.name),
    }


async def add_contact_for_agent(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    target_type: str,
    target_id: str | uuid.UUID,
    relation: str = "collaborator",
    description: str = "",
    current_user_id: uuid.UUID | None = None,
) -> dict:
    """Create or update a human or A2A relationship for an agent."""
    source_agent = await _load_source_agent(db, agent_id)
    if not source_agent or not source_agent.tenant_id:
        return {"status": "error", "reason": "source_agent_not_available"}

    clean_target_type = _normalize_target_type(target_type)
    parsed_target_id = _parse_target_id(target_id)
    if not clean_target_type or not parsed_target_id:
        return {"status": "error", "reason": "invalid_contact_target"}

    clean_relation = _normalize_relation(relation)
    clean_description = (description or "").strip()[:500]

    if clean_target_type == "human":
        return await _add_human_contact(
            db,
            source_agent,
            parsed_target_id,
            relation=clean_relation,
            description=clean_description,
            current_user_id=current_user_id,
        )
    return await _add_agent_contact(
        db,
        source_agent,
        parsed_target_id,
        relation=clean_relation,
        description=clean_description,
        current_user_id=current_user_id,
    )


async def _remove_human_contact(
    db: AsyncSession,
    source_agent: Agent,
    member_id: uuid.UUID,
) -> dict:
    member_result = await db.execute(
        select(OrgMember).where(
            OrgMember.id == member_id,
            OrgMember.tenant_id == source_agent.tenant_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if not member:
        return {"status": "error", "reason": "contact_not_available"}

    rel_result = await db.execute(
        select(AgentRelationship).where(
            AgentRelationship.agent_id == source_agent.id,
            AgentRelationship.member_id == member.id,
        )
    )
    rel = rel_result.scalar_one_or_none()
    if not rel:
        return {
            "status": "not_found",
            "id": str(member.id),
            "type": "human",
            "name": member.name,
        }

    await db.delete(rel)
    await db.flush()
    return {
        "status": "removed",
        "id": str(member.id),
        "type": "human",
        "name": member.name,
    }


async def _remove_agent_contact(
    db: AsyncSession,
    source_agent: Agent,
    target_id: uuid.UUID,
) -> dict:
    target_result = await db.execute(
        select(Agent).where(
            Agent.id == target_id,
            Agent.tenant_id == source_agent.tenant_id,
            Agent.is_deleted.is_(False),
        )
    )
    target = target_result.scalar_one_or_none()
    if not target or target.id == source_agent.id:
        return {"status": "error", "reason": "contact_not_available"}

    rel_result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.agent_id == source_agent.id,
            AgentAgentRelationship.target_agent_id == target.id,
        )
    )
    rel = rel_result.scalar_one_or_none()
    if not rel:
        return {
            "status": "not_found",
            "id": str(target.id),
            "type": "agent",
            "name": target.name,
        }

    await db.delete(rel)
    await db.flush()
    return {
        "status": "removed",
        "id": str(target.id),
        "type": "agent",
        "name": target.name,
    }


async def remove_contact_for_agent(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    target_type: str,
    target_id: str | uuid.UUID,
    current_user_id: uuid.UUID | None = None,
) -> dict:
    """Remove a human or A2A relationship from an agent."""
    source_agent = await _load_source_agent(db, agent_id)
    if not source_agent or not source_agent.tenant_id:
        return {"status": "error", "reason": "source_agent_not_available"}

    clean_target_type = _normalize_target_type(target_type)
    parsed_target_id = _parse_target_id(target_id)
    if not clean_target_type or not parsed_target_id:
        return {"status": "error", "reason": "invalid_contact_target"}

    if clean_target_type == "human":
        return await _remove_human_contact(db, source_agent, parsed_target_id)
    return await _remove_agent_contact(db, source_agent, parsed_target_id)
