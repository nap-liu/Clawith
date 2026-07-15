"""Agent relationship management API — human + agent-to-agent."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.core.permissions import (
    build_visible_agents_query,
    check_agent_access,
    evaluate_agent_relationship_status,
    get_agent_accessible_user_ids,
    get_agent_access_level_for_user_id,
    require_current_agent_tenant,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.org import (
    AgentRelationship,
    AgentAgentRelationship,
    OrgMember,
    RelationshipSuppression,
)
from app.models.user import User
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.org_sync_adapter import derive_member_department_paths

router = APIRouter(prefix="/agents/{agent_id}/relationships", tags=["relationships"])

RELATION_LABELS = {
    "direct_leader": "直属上级",
    "collaborator": "协作伙伴",
    "stakeholder": "利益相关者",
    "team_member": "团队成员",
    "subordinate": "下属",
    "mentor": "导师",
    "other": "其他",
}

AGENT_RELATION_LABELS = {
    "peer": "同级协作",
    "supervisor": "上级数字员工",
    "assistant": "助手",
    "collaborator": "协作伙伴",
    "other": "其他",
}


def _can_manage_relationships(current_user: User, access_level: str) -> bool:
    return access_level == "manage" or current_user.role in ("platform_admin", "org_admin")


def _display_provider_name(provider_name: str | None, provider_type: str | None) -> str | None:
    if not provider_name and not provider_type:
        return None
    if (provider_type or "").lower() in ("web", "platform") or (provider_name or "").lower() == "web":
        return "Platform"
    return provider_name


# ─── Schemas ───────────────────────────────────────────

class RelationshipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    relation: str = "collaborator"
    description: str = ""


class RelationshipBatchIn(BaseModel):
    relationships: list[RelationshipIn]


class AgentRelationshipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    relation: str = "collaborator"
    description: str = ""


class AgentRelationshipBatchIn(BaseModel):
    relationships: list[AgentRelationshipIn]


def _dedupe_human_relationships(items: list[RelationshipIn]) -> list[RelationshipIn]:
    deduped: dict[uuid.UUID, RelationshipIn] = {}
    for item in items:
        deduped[item.user_id] = item
    return list(deduped.values())


def _dedupe_agent_relationships(items: list[AgentRelationshipIn], agent_id: uuid.UUID) -> list[AgentRelationshipIn]:
    deduped: dict[uuid.UUID, AgentRelationshipIn] = {}
    for item in items:
        if item.agent_id == agent_id:
            continue
        deduped[item.agent_id] = item
    return list(deduped.values())


# ─── Human Relationships (existing) ───────────────────

@router.get("/")
async def get_relationships(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all human relationships for this agent."""
    from app.services.recipient_resolver import load_human_recipient_profiles
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, source_agent)
    if await ensure_access_granted_platform_relationships(
        db,
        source_agent,
        created_by_user_id=current_user.id,
    ):
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()
    result = await db.execute(
        select(AgentRelationship).where(AgentRelationship.agent_id == agent_id)
    )
    rows = list(result.scalars().all())
    profiles = await load_human_recipient_profiles(db, source_agent, rows)
    member_paths = await derive_member_department_paths(
        db,
        [profile.member for profile in profiles.values() if profile.member],
    )
    out = []
    for r in rows:
        profile = profiles.get(r.user_id)
        member = profile.member if profile else None
        status_info = (
            {
                "access_allowed": profile.access_status == "active",
                "access_status": profile.access_status,
                "access_status_reason": profile.access_status_reason,
            }
            if profile
            else {
                "access_allowed": False,
                "access_status": "missing_target",
                "access_status_reason": "agent_or_user_not_found",
            }
        )
        out.append({
            "id": str(r.id),
            "user_id": str(r.user_id),
            "relation": r.relation,
            "relation_label": RELATION_LABELS.get(r.relation, r.relation),
            "description": r.description,
            **status_info,
            "channels": list(profile.channels) if profile else [],
            "provider_names": list(profile.provider_names) if profile else [],
            "member": {
                "name": profile.user.display_name,
                "title": member.title if member else "",
                "department_path": member_paths.get(member.id, member.department_path) if member else "",
                "avatar_url": member.avatar_url if member else profile.user.avatar_url,
                "email": member.email if member else None,
                "provider_name": profile.provider_names[0] if profile.provider_names else None,
                "provider_type": profile.channels[0] if profile.channels else None,
                "user_id": str(r.user_id),
                "is_platform_user": bool(profile.user.identity_id),
            } if profile else None,
        })
    return out


@router.get("/member-candidates")
async def search_human_relationship_candidates(
    agent_id: uuid.UUID,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search org members that are eligible for this agent's human relationships."""
    from app.models.identity import IdentityProvider

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    search_text = (search or "").strip()
    access_mode = getattr(agent, "access_mode", None) or "company"
    LinkedUser = aliased(User)

    query = (
        select(
            OrgMember,
            IdentityProvider.name.label("provider_name"),
            IdentityProvider.provider_type,
            LinkedUser.id.label("linked_user_id"),
            LinkedUser.identity_id.label("linked_identity_id"),
        )
        .outerjoin(IdentityProvider, OrgMember.provider_id == IdentityProvider.id)
        .outerjoin(
            LinkedUser,
            and_(
                OrgMember.user_id == LinkedUser.id,
                LinkedUser.tenant_id == agent.tenant_id,
                LinkedUser.is_active == True,  # noqa: E712
            ),
        )
        .where(
            OrgMember.tenant_id == agent.tenant_id,
            OrgMember.status == "active",
            LinkedUser.id.isnot(None),
        )
    )
    if search_text:
        pattern = f"%{search_text}%"
        query = query.where(
            or_(
                OrgMember.name.ilike(pattern),
                OrgMember.name_translit_full.ilike(pattern),
                OrgMember.name_translit_initial.ilike(pattern),
                OrgMember.email.ilike(pattern),
            )
        )

    allowed_user_ids: set[uuid.UUID] | None = None
    if access_mode != "company":
        allowed_user_ids = await get_agent_accessible_user_ids(db, agent)
        query = query.where(
            or_(
                LinkedUser.identity_id.is_(None),
                LinkedUser.id.in_(allowed_user_ids),
            )
        )

    result = await db.execute(query.order_by(OrgMember.name).limit(200))
    rows = result.all()
    deduped_filtered = []
    by_user_id: dict[
        uuid.UUID,
        tuple[OrgMember, str | None, str | None, uuid.UUID | None, uuid.UUID | None],
    ] = {}
    for row in rows:
        member, provider_name, provider_type, linked_user_id, _linked_identity_id = row
        if not linked_user_id:
            deduped_filtered.append(row)
            continue
        existing = by_user_id.get(linked_user_id)
        if not existing:
            by_user_id[linked_user_id] = row
            continue
        existing_type = (existing[2] or "").lower()
        current_type = (provider_type or "").lower()
        if existing_type in ("", "web", "platform") and current_type not in ("", "web", "platform"):
            by_user_id[linked_user_id] = row
    filtered = [*deduped_filtered, *by_user_id.values()]

    filtered = sorted(filtered, key=lambda row: (row[0].name or "").lower())[:100]
    member_paths = await derive_member_department_paths(
        db,
        [m for m, _provider_name, _provider_type, _linked_user_id, _linked_identity_id in filtered],
    )
    org_member_candidates = [
        {
            "user_id": str(linked_user_id),
            "name": m.name,
            "email": m.email,
            "title": m.title,
            "department_path": member_paths.get(m.id, m.department_path),
            "avatar_url": m.avatar_url,
            "provider_name": _display_provider_name(provider_name, provider_type) if m.provider_id else None,
            "provider_type": "platform" if (provider_type or "").lower() == "web" else provider_type if m.provider_id else None,
            "is_platform_user": bool(linked_identity_id),
            "platform_access_level": (
                await get_agent_access_level_for_user_id(db, linked_user_id, agent)
                if linked_user_id
                else None
            ),
        }
        for m, provider_name, provider_type, linked_user_id, linked_identity_id in filtered
    ]
    return sorted(org_member_candidates, key=lambda item: (item.get("name") or "").lower())[:100]


@router.put("/")
async def save_relationships(
    agent_id: uuid.UUID,
    data: RelationshipBatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all human relationships for this agent."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, _agent)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    existing_result = await db.execute(select(AgentRelationship).where(AgentRelationship.agent_id == agent_id))
    existing_by_user = {r.user_id: r for r in existing_result.scalars().all()}
    requested = _dedupe_human_relationships(data.relationships)
    requested_user_ids = {item.user_id for item in requested}

    for removed_user_id in set(existing_by_user) - requested_user_ids:
        await db.execute(
            pg_insert(RelationshipSuppression)
            .values(
                id=uuid.uuid4(),
                agent_id=agent_id,
                target_type="user",
                target_id=removed_user_id,
                created_by_user_id=current_user.id,
            )
            .on_conflict_do_nothing(
                index_elements=["agent_id", "target_type", "target_id"]
            )
        )
    if requested_user_ids:
        await db.execute(
            delete(RelationshipSuppression).where(
                RelationshipSuppression.agent_id == agent_id,
                RelationshipSuppression.target_type == "user",
                RelationshipSuppression.target_id.in_(requested_user_ids),
            )
        )

    await db.execute(
        delete(AgentRelationship).where(AgentRelationship.agent_id == agent_id)
    )

    for r in requested:
        user_result = await db.execute(
            select(User).where(
                User.id == r.user_id,
                User.tenant_id == _agent.tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        target_user = user_result.scalar_one_or_none()
        if not target_user:
            raise HTTPException(status_code=400, detail="Relationship user is not available")
        # Login-capable platform users remain bounded by agent access. External-
        # only users have no login Identity and are channel contacts, not viewers.
        if target_user.identity_id and not await get_agent_access_level_for_user_id(db, target_user.id, _agent):
            raise HTTPException(status_code=403, detail="Platform user does not have access to this agent")

        member_result = await db.execute(
            select(OrgMember)
            .where(
                OrgMember.tenant_id == _agent.tenant_id,
                OrgMember.user_id == target_user.id,
                OrgMember.status == "active",
            )
            .order_by(OrgMember.provider_id.is_(None), OrgMember.synced_at.asc())
            .limit(1)
        )
        member = member_result.scalar_one_or_none()
        if not member:
            from app.services.registration_service import registration_service

            member = await registration_service.ensure_web_org_member(db, target_user)
        existing = existing_by_user.get(target_user.id)
        db.add(AgentRelationship(
            agent_id=agent_id,
            user_id=target_user.id,
            member_id=member.id if member else None,
            relation=r.relation,
            description=r.description,
            created_by_user_id=getattr(existing, "created_by_user_id", None) or current_user.id,
            updated_by_user_id=current_user.id,
        ))

    await db.flush()

    # Regenerate file with both types
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{rel_id}")
async def delete_relationship(
    agent_id: uuid.UUID,
    rel_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single human relationship."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, _agent)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")
    result = await db.execute(
        select(AgentRelationship).where(AgentRelationship.id == rel_id, AgentRelationship.agent_id == agent_id)
    )
    rel = result.scalar_one_or_none()
    if rel:
        await db.execute(
            pg_insert(RelationshipSuppression)
            .values(
                id=uuid.uuid4(),
                agent_id=agent_id,
                target_type="user",
                target_id=rel.user_id,
                created_by_user_id=current_user.id,
            )
            .on_conflict_do_nothing(
                index_elements=["agent_id", "target_type", "target_id"]
            )
        )
        await db.delete(rel)
        await db.flush()
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()

    return {"status": "ok"}


# ─── Agent-to-Agent Relationships (new) ───────────────

@router.get("/agent-candidates")
async def search_visible_agents(
    agent_id: uuid.UUID,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search manageable agent candidates for relationship creation."""
    source_agent, access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, source_agent)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    stmt = build_visible_agents_query(current_user, tenant_id=source_agent.tenant_id).where(Agent.id != agent_id)
    if search:
        stmt = stmt.where(
            or_(
                Agent.name.ilike(f"%{search}%"),
                Agent.role_description.ilike(f"%{search}%"),
            )
        )

    # Candidates are everything VISIBLE to the user — managing the target is not
    # required to relate the (managed) source agent to it. ``can_manage`` is a
    # cheap display hint (creator/admin) so the UI need not issue per-agent queries.
    result = await db.execute(stmt.order_by(Agent.created_at.desc()).limit(50))
    agents = result.scalars().all()
    is_admin = current_user.role in ("platform_admin", "org_admin")
    return [
        {
            "agent_id": str(agent.id),
            "name": agent.name,
            "role_description": agent.role_description or "",
            "avatar_url": agent.avatar_url or "",
            "creator_id": str(agent.creator_id),
            "access_mode": getattr(agent, "access_mode", None) or "company",
            "can_manage": bool(is_admin or agent.creator_id == current_user.id),
        }
        for agent in agents
    ]


@router.get("/agents")
async def get_agent_relationships(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all agent-to-agent relationships."""
    source_agent, _access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, source_agent)
    result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    rels = result.scalars().all()
    out = []
    for r in rels:
        status_info = await evaluate_agent_relationship_status(db, r, current_user_id=current_user.id)
        out.append({
            "id": str(r.id),
            "agent_id": str(r.target_agent_id),
            "relation": r.relation,
            "relation_label": AGENT_RELATION_LABELS.get(r.relation, r.relation),
            "description": r.description,
            **status_info,
            "target_agent": {
                "agent_id": str(r.target_agent.id),
                "name": r.target_agent.name,
                "role_description": r.target_agent.role_description or "",
                "avatar_url": r.target_agent.avatar_url or "",
                "access_mode": getattr(r.target_agent, "access_mode", None) or "company",
            } if r.target_agent else None,
        })
    return out


@router.get("/agents/candidates")
async def get_agent_relationship_candidates(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Backward-compatible alias for searchable agent candidates."""
    return await search_visible_agents(
        agent_id=agent_id,
        search=None,
        current_user=current_user,
        db=db,
    )


@router.put("/agents")
async def save_agent_relationships(
    agent_id: uuid.UUID,
    data: AgentRelationshipBatchIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace all agent-to-agent relationships."""
    source_agent, access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, source_agent)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")

    existing_result = await db.execute(select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == agent_id))
    existing_by_target = {r.target_agent_id: r for r in existing_result.scalars().all()}
    requested = _dedupe_agent_relationships(data.relationships, agent_id)
    requested_agent_ids = {item.agent_id for item in requested}
    for removed_agent_id in set(existing_by_target) - requested_agent_ids:
        await db.execute(
            pg_insert(RelationshipSuppression)
            .values(
                id=uuid.uuid4(),
                agent_id=agent_id,
                target_type="agent",
                target_id=removed_agent_id,
                created_by_user_id=current_user.id,
            )
            .on_conflict_do_nothing(
                index_elements=["agent_id", "target_type", "target_id"]
            )
        )
    if requested_agent_ids:
        await db.execute(
            delete(RelationshipSuppression).where(
                RelationshipSuppression.agent_id == agent_id,
                RelationshipSuppression.target_type == "agent",
                RelationshipSuppression.target_id.in_(requested_agent_ids),
            )
        )

    await db.execute(
        delete(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == agent_id)
    )

    for r in requested:
        target_id = r.agent_id
        existing = existing_by_target.get(target_id)
        if existing is None:
            # Only *newly added* targets are visibility-checked (same gate as the
            # agent-candidates list). Targets that were ALREADY linked pass through:
            # GET /agents returns relationships unfiltered, so the user may be replaying
            # a list that includes inherited rows (e.g. an admin-created link to someone
            # else's private agent) which are not visible to them. Re-checking those here
            # would block the user from saving a list they were shown (read/write asymmetry).
            target_result = await db.execute(
                build_visible_agents_query(current_user, tenant_id=source_agent.tenant_id).where(Agent.id == target_id)
            )
            if target_result.scalar_one_or_none() is None:
                # Visible is enough; managing the target is not required since the
                # relationship is directional (source -> target) and the user already
                # manages the source (enforced by _can_manage_relationships above).
                raise HTTPException(status_code=403, detail="Target agent is not visible to the current user")
        db.add(AgentAgentRelationship(
            agent_id=agent_id,
            target_agent_id=target_id,
            relation=r.relation,
            description=r.description,
            created_by_user_id=getattr(existing, "created_by_user_id", None) or current_user.id,
            updated_by_user_id=current_user.id,
        ))

    await db.flush()
    await _regenerate_relationships_file(db, agent_id)
    await db.commit()
    return {"status": "ok"}


@router.delete("/agents/{rel_id}")
async def delete_agent_relationship(
    agent_id: uuid.UUID,
    rel_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single agent-to-agent relationship."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, _agent)
    if not _can_manage_relationships(current_user, access_level):
        raise HTTPException(status_code=403, detail="Only org admins or managers can modify relationships")
    result = await db.execute(
        select(AgentAgentRelationship).where(
            AgentAgentRelationship.id == rel_id,
            AgentAgentRelationship.agent_id == agent_id,
        )
    )
    rel = result.scalar_one_or_none()
    if rel:
        await db.execute(
            pg_insert(RelationshipSuppression)
            .values(
                id=uuid.uuid4(),
                agent_id=agent_id,
                target_type="agent",
                target_id=rel.target_agent_id,
                created_by_user_id=current_user.id,
            )
            .on_conflict_do_nothing(
                index_elements=["agent_id", "target_type", "target_id"]
            )
        )
        await db.delete(rel)
        await db.flush()
        await _regenerate_relationships_file(db, agent_id)
        await db.commit()

    return {"status": "ok"}


# ─── relationships.md Generation ──────────────────────

async def _regenerate_relationships_file(db: AsyncSession, agent_id: uuid.UUID):
    """Obsolete. relationships.md is no longer generated as relationships are read directly from the database."""
    pass
