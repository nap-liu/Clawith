from __future__ import annotations

import json
import uuid as _uuid

from sqlalchemy import or_, select

from app.database import async_session
from app.mcp_server._common import authed_write, resolve_manageable_agent, needs_confirm
from app.schemas.agent_permissions import AgentGrant
from app.services.llm.failure_outcome import render_message
from app.services.agent_permissions import (
    grant_projection, load_agent_grants, update_agent_grants, validate_grant_subjects,
)
from app.services.access_relationships import ensure_access_granted_platform_relationships


def _parse_access_ids(values, field_name: str):
    parsed: list[_uuid.UUID] = []
    seen: set[_uuid.UUID] = set()
    for raw in values or []:
        try:
            value = _uuid.UUID(str(raw))
        except (TypeError, ValueError):
            return None, f"❌ {field_name} 包含无效 UUID：{raw!r}。"
        if value not in seen:
            parsed.append(value)
            seen.add(value)
    return parsed, None


async def get_agent_access_impl(ctx, agent) -> str:
    from app.core.permissions import user_can_manage_agent_id
    from app.mcp_server.auth import resolve_pat_context
    from app.mcp_server.tools import _resolve_visible_agent
    from app.models.agent import AgentPermission
    from app.models.org import OrgDepartment
    from app.models.user import User

    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return "❌ 未鉴权：请配置有效的 Bearer PAT。"
        ag = await _resolve_visible_agent(db, pc.user, agent)
        if ag is None or not await user_can_manage_agent_id(db, pc.user.id, ag):
            return "❌ 找不到该 agent，或你没有 manage 权限。"
        permissions = (
            await db.execute(
                select(AgentPermission).where(AgentPermission.agent_id == ag.id)
            )
        ).scalars().all()
        user_ids = [p.scope_id for p in permissions if p.scope_type == "user" and p.scope_id]
        department_ids = [
            p.scope_id for p in permissions if p.scope_type == "department" and p.scope_id
        ]
        users = {}
        departments = {}
        if user_ids:
            users = {
                str(row.id): row.display_name or str(row.id)
                for row in (
                    await db.execute(
                        select(User).where(
                            User.id.in_(user_ids),
                            User.tenant_id == ag.tenant_id,
                        )
                    )
                ).scalars().all()
            }
        if department_ids:
            departments = {
                str(row.id): {"name": row.name, "path": row.path}
                for row in (
                    await db.execute(
                        select(OrgDepartment).where(
                            OrgDepartment.id.in_(department_ids),
                            OrgDepartment.tenant_id == ag.tenant_id,
                        )
                    )
                ).scalars().all()
            }
        mode, company = grant_projection(permissions, ag.creator_id)
        payload = {
            "agent_id": str(ag.id),
            "agent_name": ag.name,
            "access_mode": mode,
            "company_access_level": company,
            "user_grants": [
                {
                    "user_id": str(p.scope_id),
                    "name": users.get(str(p.scope_id)),
                    "access_level": p.access_level,
                }
                for p in permissions
                if p.scope_type == "user" and p.scope_id
            ],
            "department_grants": [
                {
                    "department_id": str(p.scope_id),
                    **departments.get(str(p.scope_id), {}),
                    "access_level": p.access_level,
                    "include_descendants": True,
                }
                for p in permissions
                if p.scope_type == "department" and p.scope_id
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


async def search_agent_access_subjects_impl(
    ctx,
    agent,
    query="",
    subject_type="all",
    limit=20,
) -> str:
    """Search grantable users/departments without exposing another tenant."""
    from app.core.permissions import user_can_manage_agent_id
    from app.mcp_server.auth import resolve_pat_context
    from app.mcp_server.tools import _resolve_visible_agent
    from app.models.identity import IdentityProvider
    from app.models.org import OrgDepartment, OrgMember
    from app.models.user import Identity, User
    from app.services.org_directory import canonical_org_member_id_subquery

    if subject_type not in ("all", "user", "department"):
        return "❌ subject_type 只能是 all、user 或 department。"
    try:
        result_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        return "❌ limit 必须是 1 到 50 的整数。"
    search_text = str(query or "").strip()
    pattern = f"%{search_text}%"

    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return "❌ 未鉴权：请配置有效的 Bearer PAT。"
        ag = await _resolve_visible_agent(db, pc.user, agent)
        if ag is None or not await user_can_manage_agent_id(db, pc.user.id, ag):
            return "❌ 找不到该 agent，或你没有 manage 权限。"

        subjects: list[dict] = []
        if subject_type in ("all", "department"):
            department_query = (
                select(OrgDepartment, IdentityProvider)
                .outerjoin(
                    IdentityProvider,
                    (IdentityProvider.id == OrgDepartment.provider_id)
                    & (
                        (IdentityProvider.tenant_id == ag.tenant_id)
                        | (IdentityProvider.tenant_id.is_(None))
                    ),
                )
                .where(
                    OrgDepartment.tenant_id == ag.tenant_id,
                    OrgDepartment.status == "active",
                )
            )
            if search_text:
                department_query = department_query.where(
                    or_(
                        OrgDepartment.name.ilike(pattern),
                        OrgDepartment.path.ilike(pattern),
                    )
                )
            departments = (
                await db.execute(
                    department_query.order_by(
                        OrgDepartment.path.asc(),
                        IdentityProvider.name.asc().nulls_last(),
                        OrgDepartment.provider_id.asc().nulls_last(),
                    ).limit(result_limit)
                )
            ).all()
            subjects.extend(
                {
                    "subject_type": "department",
                    "department_id": str(department.id),
                    "name": department.name,
                    "path": department.path,
                    "provider_id": str(department.provider_id) if department.provider_id else None,
                    "provider_name": provider.name if provider else None,
                    "provider_type": (
                        getattr(provider.provider_type, "value", provider.provider_type)
                        if provider
                        else None
                    ),
                    "include_descendants": True,
                }
                for department, provider in departments
            )

        if subject_type in ("all", "user"):
            users_by_id: dict[_uuid.UUID, dict] = {}
            canonical = canonical_org_member_id_subquery(
                tenant_id=ag.tenant_id,
                prefer_directory_profile=True,
            )
            directory_user_query = (
                select(User, OrgMember, Identity, IdentityProvider)
                .join(OrgMember, OrgMember.user_id == User.id)
                .join(
                    canonical,
                    (OrgMember.id == canonical.c.om_id) & (canonical.c.rn == 1),
                )
                .outerjoin(Identity, Identity.id == User.identity_id)
                .outerjoin(
                    IdentityProvider,
                    (IdentityProvider.id == OrgMember.provider_id)
                    & (
                        (IdentityProvider.tenant_id == ag.tenant_id)
                        | (IdentityProvider.tenant_id.is_(None))
                    ),
                )
                .where(
                    User.tenant_id == ag.tenant_id,
                    User.is_active == True,  # noqa: E712
                )
            )
            if search_text:
                directory_user_query = directory_user_query.where(
                    or_(
                        User.display_name.ilike(pattern),
                        Identity.email.ilike(pattern),
                        Identity.username.ilike(pattern),
                        OrgMember.name.ilike(pattern),
                        OrgMember.nickname.ilike(pattern),
                        OrgMember.name_translit_full.ilike(pattern),
                        OrgMember.name_translit_initial.ilike(pattern),
                        OrgMember.department_path.ilike(pattern),
                    )
                )
            directory_rows = (
                await db.execute(
                    directory_user_query.order_by(OrgMember.name.asc()).limit(result_limit)
                )
            ).all()
            for user, member, identity, provider in directory_rows:
                users_by_id[user.id] = {
                    "subject_type": "user",
                    "user_id": str(user.id),
                    "name": member.name or user.display_name,
                    "nickname": member.nickname,
                    "email": identity.email if identity else None,
                    "department_path": member.department_path or "",
                    "title": member.title or user.title or "",
                    "provider_id": str(member.provider_id) if member.provider_id else None,
                    "provider_name": provider.name if provider else None,
                    "provider_type": (
                        getattr(provider.provider_type, "value", provider.provider_type)
                        if provider
                        else None
                    ),
                }

            platform_user_query = (
                select(User, Identity)
                .outerjoin(Identity, Identity.id == User.identity_id)
                .where(
                    User.tenant_id == ag.tenant_id,
                    User.is_active == True,  # noqa: E712
                )
            )
            if search_text:
                platform_user_query = platform_user_query.where(
                    or_(
                        User.display_name.ilike(pattern),
                        Identity.email.ilike(pattern),
                        Identity.username.ilike(pattern),
                    )
                )
            platform_rows = (
                await db.execute(
                    platform_user_query.order_by(User.display_name.asc()).limit(result_limit)
                )
            ).all()
            for user, identity in platform_rows:
                users_by_id.setdefault(
                    user.id,
                    {
                        "subject_type": "user",
                        "user_id": str(user.id),
                        "name": user.display_name,
                        "email": identity.email if identity else None,
                        "department_path": "",
                        "title": user.title or "",
                        "provider_id": None,
                        "provider_name": None,
                        "provider_type": None,
                    },
                )
            subjects.extend(list(users_by_id.values())[:result_limit])

        payload = {
            "query": search_text,
            "subject_type": subject_type,
            "items": subjects,
            "limit_per_type": result_limit,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


def _access_message(ctx, key, **values):
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    locale = getattr(request, "headers", {}).get("accept-language", "zh")
    return render_message(f"agentPermissions.{key}", locale).format(**values)


async def _finish_access_update(db, agent, actor_id):
    from app.api.relationships import _regenerate_relationships_file

    if await ensure_access_granted_platform_relationships(db, agent, created_by_user_id=actor_id):
        await _regenerate_relationships_file(db, agent.id)
    await db.commit()


async def set_agent_access_mode_impl(
    ctx, agent, access_mode, company_access_level=None, confirm=False,
) -> str:
    if access_mode not in ("company", "private", "custom"):
        return "❌ " + _access_message(ctx, "invalidScope")
    if company_access_level not in (None, "use", "manage"):
        return "❌ " + _access_message(ctx, "invalidLevel")
    if access_mode != "company" and company_access_level is not None:
        return "❌ " + _access_message(ctx, "companyLevelOnly")
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        summary = _access_message(ctx, "privateSummary" if access_mode == "private" else "scopeSummary")
        level = company_access_level or ag.company_grant_level or "use"
        guidance = needs_confirm(confirm, _access_message(
            ctx, "scopePreview", name=ag.name, mode=access_mode, level=level, summary=summary,
        ))
        if guidance is not None:
            return guidance
        payload = {"scope_type": access_mode}
        if company_access_level is not None:
            payload["access_level"] = company_access_level
        await update_agent_grants(db, ag, actor_id=pc.user.id, data=payload)
        await _finish_access_update(db, ag, pc.user.id)
        return _access_message(ctx, "scopeSaved", name=ag.name, summary=summary)


async def grant_agent_access_impl(
    ctx, agent, user_ids=None, department_ids=None, access_level="use", confirm=False,
) -> str:
    if access_level not in ("use", "manage"):
        return "❌ " + _access_message(ctx, "invalidLevel")
    users, error = _parse_access_ids(user_ids, "user_ids")
    if error:
        return error
    departments, error = _parse_access_ids(department_ids, "department_ids")
    if error:
        return error
    if not users and not departments:
        return "❌ " + _access_message(ctx, "emptySubjects")
    additions = [
        AgentGrant(scope_type=kind, scope_id=sid, access_level=access_level)
        for kind, ids in (("user", users), ("department", departments)) for sid in ids
    ]
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        try:
            await validate_grant_subjects(db, ag, additions)
        except ValueError as exc:
            return f"❌ {exc}"
        guidance = needs_confirm(confirm, _access_message(
            ctx, "grantPreview", name=ag.name, users=len(users), departments=len(departments), level=access_level,
        ))
        if guidance is not None:
            return guidance
        await update_agent_grants(db, ag, actor_id=pc.user.id, add=additions)
        await _finish_access_update(db, ag, pc.user.id)
        return _access_message(ctx, "grantsSaved", name=ag.name, count=len(additions))


async def revoke_agent_access_impl(
    ctx, agent, user_ids=None, department_ids=None, confirm=False,
) -> str:
    users, error = _parse_access_ids(user_ids, "user_ids")
    if error:
        return error
    departments, error = _parse_access_ids(department_ids, "department_ids")
    if error:
        return error
    if not users and not departments:
        return "❌ " + _access_message(ctx, "emptySubjects")
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        removals = {(kind, sid) for kind, ids in (("user", users), ("department", departments)) for sid in ids}
        grants = await load_agent_grants(db, ag.id)
        count = sum((g.scope_type, g.scope_id) in removals for g in grants)
        guidance = needs_confirm(confirm, _access_message(ctx, "revokePreview", name=ag.name, count=count))
        if guidance is not None:
            return guidance
        await update_agent_grants(db, ag, actor_id=pc.user.id, remove=removals)
        await _finish_access_update(db, ag, pc.user.id)
        return _access_message(ctx, "revoked", name=ag.name, count=count)
