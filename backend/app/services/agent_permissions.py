"""Single writer for Agent grants. Callers own authentication and commit."""

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.core.permissions import check_agent_access
from app.models.agent import Agent, AgentPermission
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment
from app.models.user import Identity, User
from app.services.llm.failure_outcome import render_message
from app.schemas.agent_permissions import AgentGrant, AgentPermissionUpdate, legacy_grants


async def load_agent_grants(db, agent_id) -> list[AgentGrant]:
    rows = await db.scalars(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    return [AgentGrant.model_validate(row) for row in rows]


def grant_projection(grants, creator_id=None):
    company = next((g.access_level for g in grants if g.scope_type == "company"), None)
    custom = any(g.scope_type != "company" and g.scope_id != creator_id for g in grants)
    return ("company" if company else "custom" if custom else "private"), company


def normalize_grants(grants) -> list[AgentGrant]:
    normalized = {}
    for grant in grants:
        grant = AgentGrant.model_validate(grant)
        key = (grant.scope_type, grant.scope_id)
        previous = normalized.get(key)
        if previous is None or grant.access_level == "manage":
            normalized[key] = grant
    return sorted(normalized.values(), key=lambda g: (g.scope_type, str(g.scope_id or "")))


async def validate_grant_subjects(db, agent, grants):
    user_ids = {g.scope_id for g in grants if g.scope_type == "user"}
    department_ids = {g.scope_id for g in grants if g.scope_type == "department"}
    if user_ids:
        valid = set(await db.scalars(select(User.id).outerjoin(Identity).where(
            User.id.in_(user_ids), User.tenant_id == agent.tenant_id,
            User.is_active.is_(True), or_(Identity.id.is_(None), Identity.is_active.is_(True)),
        )))
        if valid != user_ids:
            raise ValueError(render_message("agentPermissions.invalidUser"))
    if department_ids:
        valid = set(await db.scalars(select(OrgDepartment.id).outerjoin(
            IdentityProvider, IdentityProvider.id == OrgDepartment.provider_id,
        ).where(
            OrgDepartment.id.in_(department_ids), OrgDepartment.tenant_id == agent.tenant_id,
            OrgDepartment.status == "active",
            or_(OrgDepartment.provider_id.is_(None), IdentityProvider.is_active.is_(True)),
        )))
        if valid != department_ids:
            raise ValueError(render_message("agentPermissions.invalidDepartment"))


async def update_agent_grants(
    db, agent, *, actor_id, data=None, add=(), remove=(),
):
    """Serialize all writers on the Agent; validate before touching its grants."""
    await db.execute(select(Agent.id).where(Agent.id == agent.id).with_for_update())
    actor = await db.scalar(select(User).where(User.id == actor_id).options(
        selectinload(User.identity),
    ).execution_options(populate_existing=True))
    if actor is None or not actor.is_active or (actor.identity and not actor.identity.is_active):
        raise ValueError(render_message("agentPermissions.activeOperator"))
    _, access = await check_agent_access(db, actor, agent.id)
    if access != "manage":
        raise ValueError(render_message("agentPermissions.managerRequired"))
    rows = list(await db.scalars(select(AgentPermission).where(AgentPermission.agent_id == agent.id)))
    before = [AgentGrant.model_validate(row) for row in rows]
    if data is not None:
        update = AgentPermissionUpdate.model_validate(data)
        after = legacy_grants(update, before)
    else:
        additions = [AgentGrant.model_validate(grant) for grant in add]
        replaced = {(g.scope_type, g.scope_id) for g in additions} | set(remove)
        after = [g for g in before if (g.scope_type, g.scope_id) not in replaced] + additions
    after = normalize_grants(after)
    # Existing inactive subjects may remain for audit; only new/changed grants
    # require an active subject. Runtime always checks current directory status.
    unchanged = {g.model_dump_json() for g in before}
    await validate_grant_subjects(db, agent, [g for g in after if g.model_dump_json() not in unchanged])
    before_json = [g.model_dump(mode="json") for g in before]
    after_json = [g.model_dump(mode="json") for g in after]
    if normalize_grants(before) != after:
        existing = {(row.scope_type, row.scope_id): row for row in rows}
        desired = {(g.scope_type, g.scope_id): g for g in after}
        for key, row in existing.items():
            if key not in desired:
                await db.delete(row)
            else:
                row.access_level = desired[key].access_level
        for key, grant in desired.items():
            if key not in existing:
                db.add(AgentPermission(agent_id=agent.id, **grant.model_dump()))
        db.add(AuditLog(
            agent_id=agent.id, user_id=actor_id, action="agent_permissions_updated",
            details={"before": before_json, "after": after_json},
        ))
    # Old clients still receive these fields. They are projections, never inputs
    # to the authorization evaluator. All effective grants live in one table.
    mode, company = grant_projection(after, agent.creator_id)
    agent.access_mode = mode
    agent.company_access_level = company or "use"
    await db.flush()
    await db.refresh(agent, ["company_grant_level"])
    return after
