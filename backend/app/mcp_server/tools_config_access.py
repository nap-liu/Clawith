from __future__ import annotations

import json
import uuid as _uuid

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.mcp_server._common import authed_write, resolve_manageable_agent


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


async def _upsert_access_permission(db, *, agent_id, scope_type, scope_id, access_level):
    from app.models.agent import AgentPermission

    scope_filter = (
        AgentPermission.scope_id.is_(None)
        if scope_id is None
        else AgentPermission.scope_id == scope_id
    )
    previous_levels = (
        await db.execute(
            select(AgentPermission.access_level).where(
                AgentPermission.agent_id == agent_id,
                AgentPermission.scope_type == scope_type,
                scope_filter,
            )
        )
    ).scalars().all()
    previous = (
        "manage"
        if "manage" in previous_levels
        else ("use" if previous_levels else None)
    )
    statement = pg_insert(AgentPermission).values(
        id=_uuid.uuid4(),
        agent_id=agent_id,
        scope_type=scope_type,
        scope_id=scope_id,
        access_level=access_level,
    )
    if scope_id is None:
        statement = statement.on_conflict_do_update(
            index_elements=[AgentPermission.agent_id, AgentPermission.scope_type],
            index_where=AgentPermission.scope_id.is_(None),
            set_={"access_level": access_level},
        )
    else:
        statement = statement.on_conflict_do_update(
            index_elements=[
                AgentPermission.agent_id,
                AgentPermission.scope_type,
                AgentPermission.scope_id,
            ],
            index_where=AgentPermission.scope_id.is_not(None),
            set_={"access_level": access_level},
        )
    await db.execute(statement)
    return previous


async def _ensure_access_required_managers(db, agent, actor_id):
    from app.api.agents import _get_active_admin_users

    required_ids = {agent.creator_id or actor_id}
    required_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))
    for user_id in required_ids:
        await _upsert_access_permission(
            db,
            agent_id=agent.id,
            scope_type="user",
            scope_id=user_id,
            access_level="manage",
        )
    return required_ids


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
        payload = {
            "agent_id": str(ag.id),
            "agent_name": ag.name,
            "access_mode": ag.access_mode,
            "company_access_level": ag.company_access_level,
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


async def set_agent_access_mode_impl(
    ctx,
    agent,
    access_mode,
    company_access_level=None,
    confirm=False,
) -> str:
    from app.mcp_server._common import needs_confirm
    from app.services.access_relationships import ensure_access_granted_platform_relationships
    from app.api.relationships import _regenerate_relationships_file

    if access_mode not in ("company", "private", "custom"):
        return "❌ access_mode 只能是 company、private 或 custom。"
    if company_access_level is not None and company_access_level not in ("use", "manage"):
        return "❌ company_access_level 只能是 use 或 manage。"
    if access_mode != "company" and company_access_level is not None:
        return "❌ company_access_level 只在 access_mode=company 时传入。"

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        old_mode = ag.access_mode
        old_company_level = ag.company_access_level
        effective_company_level = company_access_level or old_company_level or "use"
        company_level_suffix = (
            f"；company_access_level: {old_company_level} → {effective_company_level}"
            if access_mode == "company"
            else ""
        )
        guidance = needs_confirm(
            confirm,
            f"将「{ag.name}」访问模式从 {old_mode} 切换为 {access_mode}；"
            "已有用户和部门授权会保留，在 custom 模式下恢复生效"
            f"{company_level_suffix}",
        )
        if guidance is not None:
            return guidance

        ag.access_mode = access_mode
        if access_mode == "company":
            ag.company_access_level = effective_company_level
            await _upsert_access_permission(
                db,
                agent_id=ag.id,
                scope_type="company",
                scope_id=None,
                access_level=effective_company_level,
            )
        else:
            await _ensure_access_required_managers(db, ag, pc.user.id)

        await db.flush()
        relationships_changed = await ensure_access_granted_platform_relationships(
            db, ag, created_by_user_id=pc.user.id
        )
        if relationships_changed:
            await _regenerate_relationships_file(db, ag.id)
        await db.commit()
        rollback_level = (
            f', company_access_level="{old_company_level}"'
            if old_mode == "company"
            else ""
        )
        return (
            f"✅ 已切换「{ag.name}」访问模式：{old_mode} → {access_mode}"
            f"{company_level_suffix}。"
            "用户和部门授权未被删除。\n"
            f"↩ 回滚：set_agent_access_mode(agent=\"{ag.name}\", "
            f"access_mode=\"{old_mode}\"{rollback_level}, "
            "confirm=true)"
        )


async def grant_agent_access_impl(
    ctx,
    agent,
    user_ids=None,
    department_ids=None,
    access_level="use",
    confirm=False,
) -> str:
    from app.mcp_server._common import needs_confirm
    from app.models.org import OrgDepartment
    from app.models.user import User
    from app.services.access_relationships import ensure_access_granted_platform_relationships
    from app.api.relationships import _regenerate_relationships_file

    if access_level not in ("use", "manage"):
        return "❌ access_level 只能是 use 或 manage。"
    parsed_user_ids, error = _parse_access_ids(user_ids, "user_ids")
    if error:
        return error
    parsed_department_ids, error = _parse_access_ids(department_ids, "department_ids")
    if error:
        return error
    if not parsed_user_ids and not parsed_department_ids:
        return "❌ 至少传入一个 user_id 或 department_id。"

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if ag.access_mode != "custom":
            return (
                f"❌ 当前访问模式是 {ag.access_mode}。"
                "请先调用 set_agent_access_mode 切换为 custom，再增量授权。"
            )

        valid_users = set()
        if parsed_user_ids:
            valid_users = set(
                (
                    await db.execute(
                        select(User.id).where(
                            User.id.in_(parsed_user_ids),
                            User.tenant_id == ag.tenant_id,
                            User.is_active == True,  # noqa: E712
                        )
                    )
                ).scalars().all()
            )
        valid_departments = set()
        if parsed_department_ids:
            valid_departments = set(
                (
                    await db.execute(
                        select(OrgDepartment.id).where(
                            OrgDepartment.id.in_(parsed_department_ids),
                            OrgDepartment.tenant_id == ag.tenant_id,
                            OrgDepartment.status == "active",
                        )
                    )
                ).scalars().all()
            )
        invalid_users = set(parsed_user_ids) - valid_users
        invalid_departments = set(parsed_department_ids) - valid_departments
        if invalid_users or invalid_departments:
            return (
                "❌ 授权对象校验失败，本次未执行任何修改。"
                f" invalid_user_ids={[str(v) for v in invalid_users]},"
                f" invalid_department_ids={[str(v) for v in invalid_departments]}"
            )

        guidance = needs_confirm(
            confirm,
            f"将向「{ag.name}」增量授予 {len(valid_users)} 个用户、"
            f"{len(valid_departments)} 个部门（含全部下级）{access_level} 权限；"
            "现有授权不会被删除",
        )
        if guidance is not None:
            return guidance

        required_manager_ids = await _ensure_access_required_managers(db, ag, pc.user.id)
        created = 0
        updated = 0
        for user_id in parsed_user_ids:
            level = "manage" if user_id in required_manager_ids else access_level
            previous = await _upsert_access_permission(
                db,
                agent_id=ag.id,
                scope_type="user",
                scope_id=user_id,
                access_level=level,
            )
            created += previous is None
            updated += previous is not None and previous != level
        for department_id in parsed_department_ids:
            previous = await _upsert_access_permission(
                db,
                agent_id=ag.id,
                scope_type="department",
                scope_id=department_id,
                access_level=access_level,
            )
            created += previous is None
            updated += previous is not None and previous != access_level

        await db.flush()
        relationships_changed = await ensure_access_granted_platform_relationships(
            db, ag, created_by_user_id=pc.user.id
        )
        if relationships_changed:
            await _regenerate_relationships_file(db, ag.id)
        await db.commit()
        return (
            f"✅ 已增量更新「{ag.name}」授权：新增 {created}，权限变更 {updated}，"
            f"保持不变 {len(parsed_user_ids) + len(parsed_department_ids) - created - updated}。"
        )


async def revoke_agent_access_impl(
    ctx,
    agent,
    user_ids=None,
    department_ids=None,
    confirm=False,
) -> str:
    from app.api.agents import _get_active_admin_users
    from app.mcp_server._common import needs_confirm
    from app.models.agent import AgentPermission

    parsed_user_ids, error = _parse_access_ids(user_ids, "user_ids")
    if error:
        return error
    parsed_department_ids, error = _parse_access_ids(department_ids, "department_ids")
    if error:
        return error
    if not parsed_user_ids and not parsed_department_ids:
        return "❌ 至少传入一个 user_id 或 department_id。"

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if ag.access_mode != "custom":
            return (
                f"❌ 当前访问模式是 {ag.access_mode}；增量撤权仅适用于 custom 模式。"
            )
        required_manager_ids = {ag.creator_id}
        required_manager_ids.update(
            admin.id for admin in await _get_active_admin_users(db, ag.tenant_id)
        )
        protected_ids = set(parsed_user_ids) & required_manager_ids
        if protected_ids:
            return (
                "❌ 创建者和公司管理员的 manage 权限不能撤销，本次未执行任何修改："
                f" {[str(value) for value in protected_ids]}"
            )

        conditions = []
        if parsed_user_ids:
            conditions.append(
                (AgentPermission.scope_type == "user")
                & AgentPermission.scope_id.in_(parsed_user_ids)
            )
        if parsed_department_ids:
            conditions.append(
                (AgentPermission.scope_type == "department")
                & AgentPermission.scope_id.in_(parsed_department_ids)
            )
        matched = list(
            (
                await db.execute(
                    select(AgentPermission).where(
                        AgentPermission.agent_id == ag.id,
                        or_(*conditions),
                    )
                )
            ).scalars().all()
        )
        guidance = needs_confirm(
            confirm,
            f"将从「{ag.name}」精确撤销 {len(matched)} 条用户/部门授权；"
            "其他授权保持不变，已有关系不会自动删除",
        )
        if guidance is not None:
            return guidance
        for permission in matched:
            await db.delete(permission)
        await db.commit()
        return f"✅ 已从「{ag.name}」增量撤销 {len(matched)} 条授权；其他授权未变。"
