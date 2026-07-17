"""MCP configuration tools (write scope, all require manage)."""
from __future__ import annotations

import json
import uuid as _uuid

from mcp.server.fastmcp import Context
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, resolve_manageable_agent, resolve_tenant_tool
from app.models.tool import AgentTool


# ── E1: set_agent_tools ──
async def _apply_tool(db, agent_id, tool, enabled: bool):
    row = (await db.execute(select(AgentTool).where(
        AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))).scalar_one_or_none()
    if row:
        row.enabled = enabled
    else:
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=enabled))


async def set_agent_tools_impl(ctx, agent, enable=None, disable=None) -> str:
    enable = enable or []
    disable = disable or []
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        applied, missing = [], []
        for ref in enable:
            tool = await resolve_tenant_tool(db, ag.tenant_id, ref)
            if tool is None:
                missing.append(ref)
                continue
            await _apply_tool(db, ag.id, tool, True)
            applied.append(f"+{tool.name}")
        for ref in disable:
            tool = await resolve_tenant_tool(db, ag.tenant_id, ref)
            if tool is None:
                missing.append(ref)
                continue
            await _apply_tool(db, ag.id, tool, False)
            applied.append(f"-{tool.name}")
        await db.commit()
        msg = f"✅ 已更新「{ag.name}」工具：{', '.join(applied) or '（无变化）'}。"
        if missing:
            msg += (
                f"\n⚠ 找不到这些工具（已跳过）：{', '.join(missing)}。"
                "用 list_available_tools 查看可用工具的名字或 id，并重试。"
            )
        return msg


# ── E2: set_agent_trigger ──
async def set_agent_trigger_impl(ctx, agent, name, type, config, reason,
                                 focus_ref=None, webhook_mode=None) -> str:
    from app.services.agent_tools import _handle_set_trigger
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        agent_id = ag.id  # capture UUID before leaving the session scope
    arguments = {"name": name, "type": type, "config": config, "reason": reason}
    if focus_ref is not None:
        arguments["focus_ref"] = focus_ref
    if webhook_mode is not None:
        arguments["webhook_mode"] = webhook_mode
    # _handle_set_trigger validates type/config and persists in its own session; returns a string.
    return await _handle_set_trigger(agent_id, arguments, user_id=pc.user.id)


# ── E3: delete_agent_trigger ──
async def delete_agent_trigger_impl(ctx, agent, trigger) -> str:
    from app.models.trigger import AgentTrigger
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        q = select(AgentTrigger).where(AgentTrigger.agent_id == ag.id)
        row = None
        try:
            tid = _uuid.UUID(str(trigger))
            row = (await db.execute(q.where(AgentTrigger.id == tid))).scalar_one_or_none()
        except (ValueError, TypeError):
            row = (await db.execute(q.where(AgentTrigger.name == trigger))).scalar_one_or_none()
        if row is None:
            return (
                f"❌ 找不到该触发器（你传入了 {trigger!r}，按 name 或 id 指定）。"
                "用 list_agent_triggers 查看该 agent 的触发器及其 id/名字，再重试。"
            )
        tname = row.name
        await db.delete(row)
        await db.commit()
        return f"✅ 已删除「{ag.name}」的触发器：{tname}。"


@mcp.tool()
async def set_agent_tools(ctx: Context, agent: str, enable: list[str] = [], disable: list[str] = []) -> str:  # noqa: D401,B006
    """Enable or disable tools on an agent (write scope + manage).
    agent: id or name. enable/disable: lists of tool ids or names (see list_available_tools)."""
    return await set_agent_tools_impl(ctx, agent=agent, enable=enable, disable=disable)


@mcp.tool()
async def set_agent_trigger(ctx: Context, agent: str, name: str, type: str, config: dict,  # noqa: D401
                            reason: str, focus_ref: str | None = None, webhook_mode: str | None = None) -> str:
    """Create or update an autonomous trigger on an agent (write scope + manage).
    type: cron | once | interval | poll | on_message | webhook.
    config per type — cron: {"expr": "0 9 * * *"}; once: {"at": "<iso8601>"}; interval: {"minutes": N};
    poll: {"url": "..."}; on_message: {"from_agent_id": "<Agent UUID>"} or {"from_user_id": "<User UUID>"}; webhook: {} (token auto-issued).
    reason explains what the trigger should do. For webhook triggers the returned text includes the callback URL."""
    return await set_agent_trigger_impl(ctx, agent=agent, name=name, type=type, config=config,
                                        reason=reason, focus_ref=focus_ref, webhook_mode=webhook_mode)


@mcp.tool()
async def delete_agent_trigger(ctx: Context, agent: str, trigger: str) -> str:  # noqa: D401
    """Delete an autonomous trigger from an agent (write scope + manage).
    agent: id or name. trigger: the trigger's id or name."""
    return await delete_agent_trigger_impl(ctx, agent=agent, trigger=trigger)


# ── E4: set_agent_relationships ──
async def _apply_a2a_links(db, current_user, source_agent, links, mode, added, errors):
    """Wire agent-to-agent (A2A) relationships. Mutates added/errors in place."""
    from app.models.agent import Agent  # noqa: F401  (kept for clarity)
    from app.models.org import AgentAgentRelationship, RelationshipSuppression
    from app.mcp_server.tools import _resolve_visible_agent

    existing_result = await db.execute(select(AgentAgentRelationship).where(
        AgentAgentRelationship.agent_id == source_agent.id))
    existing_by_target = {r.target_agent_id: r for r in existing_result.scalars().all()}
    requested_target_ids: set[_uuid.UUID] = set()

    for link in links:
        ref = link.get("agent_id")
        if not ref:
            errors.append("(A2A 缺少 agent_id)")
            continue
        try:
            target_id = _uuid.UUID(str(ref))
        except (ValueError, TypeError):
            errors.append(f"A2A:{ref}(agent_id 必须是 UUID)")
            continue
        target = await _resolve_visible_agent(db, current_user, str(target_id))
        if target is None:
            errors.append(f"A2A:{ref}")
            continue
        if target.id == source_agent.id:
            errors.append(f"A2A:{ref}(不能关联自身)")
            continue
        requested_target_ids.add(target.id)
        await db.execute(
            delete(RelationshipSuppression).where(
                RelationshipSuppression.agent_id == source_agent.id,
                RelationshipSuppression.target_type == "agent",
                RelationshipSuppression.target_id == target.id,
            )
        )
        relation = link.get("relation") or "collaborator"
        description = link.get("description") or ""
        existing = existing_by_target.get(target.id)
        if existing is not None:
            existing.relation = relation
            existing.description = description
            existing.updated_by_user_id = current_user.id
        else:
            row = AgentAgentRelationship(
                agent_id=source_agent.id,
                target_agent_id=target.id,
                relation=relation,
                description=description,
                created_by_user_id=current_user.id,
                updated_by_user_id=current_user.id,
            )
            db.add(row)
            existing_by_target[target.id] = row
        added.append(f"🤖{target.name}")

    if mode == "replace":
        removed_target_ids = set(existing_by_target) - requested_target_ids
        for target_id in removed_target_ids:
            await db.execute(
                pg_insert(RelationshipSuppression)
                .values(
                    id=_uuid.uuid4(),
                    agent_id=source_agent.id,
                    target_type="agent",
                    target_id=target_id,
                    created_by_user_id=current_user.id,
                )
                .on_conflict_do_nothing(
                    index_elements=["agent_id", "target_type", "target_id"]
                )
            )
        if removed_target_ids:
            await db.execute(
                delete(AgentAgentRelationship).where(
                    AgentAgentRelationship.agent_id == source_agent.id,
                    AgentAgentRelationship.target_agent_id.in_(removed_target_ids),
                )
            )


async def _apply_human_links(db, current_user, source_agent, links, mode, added, errors):
    """Wire agent-to-human relationships. Mutates added/errors in place."""
    from app.core.permissions import get_agent_access_level_for_user_id
    from app.models.org import AgentRelationship, OrgMember, RelationshipSuppression
    from app.models.user import User

    existing_result = await db.execute(select(AgentRelationship).where(
        AgentRelationship.agent_id == source_agent.id))
    existing_by_member = {r.user_id: r for r in existing_result.scalars().all()}
    requested_user_ids: set[_uuid.UUID] = set()

    for link in links:
        ref = link.get("user_id")
        if not ref:
            errors.append("(human 缺少 user_id)")
            continue
        ref = str(ref)
        try:
            user_id = _uuid.UUID(ref)
        except (ValueError, TypeError):
            errors.append(f"human:{ref}")
            continue
        user_result = await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == source_agent.tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        target_user = user_result.scalar_one_or_none()
        if not target_user:
            errors.append(f"human:{ref}")
            continue
        if target_user.identity_id and not await get_agent_access_level_for_user_id(
            db, target_user.id, source_agent
        ):
            errors.append(f"human:{ref}(无权访问)")
            continue
        requested_user_ids.add(target_user.id)
        await db.execute(
            delete(RelationshipSuppression).where(
                RelationshipSuppression.agent_id == source_agent.id,
                RelationshipSuppression.target_type == "user",
                RelationshipSuppression.target_id == target_user.id,
            )
        )
        member_result = await db.execute(
            select(OrgMember)
            .where(
                OrgMember.tenant_id == source_agent.tenant_id,
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

        relation = link.get("relation") or "collaborator"
        description = link.get("description") or ""
        existing = existing_by_member.get(target_user.id)
        if existing is not None:
            existing.relation = relation
            existing.description = description
            existing.updated_by_user_id = current_user.id
        else:
            row = AgentRelationship(
                agent_id=source_agent.id,
                user_id=target_user.id,
                member_id=member.id,
                relation=relation,
                description=description,
                created_by_user_id=current_user.id,
                updated_by_user_id=current_user.id,
            )
            db.add(row)
            existing_by_member[target_user.id] = row
        added.append(f"👤{member.name}")

    if mode == "replace":
        removed_user_ids = set(existing_by_member) - requested_user_ids
        for user_id in removed_user_ids:
            await db.execute(
                pg_insert(RelationshipSuppression)
                .values(
                    id=_uuid.uuid4(),
                    agent_id=source_agent.id,
                    target_type="user",
                    target_id=user_id,
                    created_by_user_id=current_user.id,
                )
                .on_conflict_do_nothing(
                    index_elements=["agent_id", "target_type", "target_id"]
                )
            )
        if removed_user_ids:
            await db.execute(
                delete(AgentRelationship).where(
                    AgentRelationship.agent_id == source_agent.id,
                    AgentRelationship.user_id.in_(removed_user_ids),
                )
            )


async def set_agent_relationships_impl(ctx, agent, agent_links=None, human_links=None, mode="merge") -> str:
    from app.api.relationships import _regenerate_relationships_file
    agent_links = agent_links or []
    human_links = human_links or []
    if mode not in ("merge", "replace"):
        return (
            f"❌ mode 取值无效（你传入了 {mode!r}），只能是 merge（增量 upsert，默认）"
            "或 replace（整组替换）。"
        )
    if not agent_links and not human_links:
        return "（未提供任何关系：请在 agent_links 或 human_links 中至少传一项。）"
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        added: list[str] = []
        errors: list[str] = []
        if agent_links:
            await _apply_a2a_links(db, pc.user, ag, agent_links, mode, added, errors)
        if human_links:
            await _apply_human_links(db, pc.user, ag, human_links, mode, added, errors)
        await db.flush()
        await _regenerate_relationships_file(db, ag.id)
        await db.commit()
        msg = f"✅ 已为「{ag.name}」设置关系（{mode}）：{', '.join(added) or '（无变化）'}"
        if errors:
            msg += f"\n⚠ 跳过（找不到/无权）：{', '.join(errors)}"
        return msg


@mcp.tool()
async def set_agent_relationships(ctx: Context, agent_id: str, agent_links: list[dict] = [],  # noqa: D401,B006
                                  human_links: list[dict] = [], mode: str = "merge") -> str:
    """Wire an agent's collaboration relationships (write scope + manage).
    agent_id: canonical id of the agent to configure.
    agent_links: list of {"agent_id": "<uuid>", "relation"?: str, "description"?: str} —
      agent-to-agent (A2A) links; the target must be VISIBLE to the calling user.
    human_links: list of {"user_id": "<uuid>", "relation"?: str, "description"?: str} —
      agent-to-human links; the user must have access to this agent.
    mode: "merge" (default, upsert) keeps existing links and adds/updates the given ones;
      "replace" wipes existing links of EACH provided kind first, then sets the given ones.
    Targets that can't be found or aren't permitted are skipped (reported), not fatal."""
    try:
        canonical_agent_id = str(_uuid.UUID(agent_id))
    except (TypeError, ValueError):
        return "❌ agent_id 必须是完整的平台 UUID，不能使用名称。"
    return await set_agent_relationships_impl(ctx, agent=canonical_agent_id, agent_links=agent_links,
                                              human_links=human_links, mode=mode)


# ── E6: explicit, incremental access management ──
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
        from sqlalchemy import or_
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


@mcp.tool()
async def get_agent_access(ctx: Context, agent: str) -> str:  # noqa: D401
    """Inspect the complete access policy for an agent you manage.

    Returns access_mode plus exact user and department UUID grants. Call this
    before changing access so incremental mutations can target known IDs."""
    return await get_agent_access_impl(ctx, agent=agent)


@mcp.tool()
async def search_agent_access_subjects(  # noqa: D401
    ctx: Context,
    agent: str,
    query: str = "",
    subject_type: str = "all",
    limit: int = 20,
) -> str:
    """Search grantable users and departments for an agent you manage.

    subject_type: all | user | department. Search accepts names, email, username,
    pinyin, or department path and returns tenant-scoped UUIDs for grant_agent_access.
    Results include provider_id, provider_name, and provider_type so same-name
    departments from different directories remain distinguishable. limit is per
    subject type and capped at 50. This tool is read-only."""
    return await search_agent_access_subjects_impl(
        ctx,
        agent=agent,
        query=query,
        subject_type=subject_type,
        limit=limit,
    )


@mcp.tool()
async def set_agent_access_mode(  # noqa: D401
    ctx: Context,
    agent: str,
    access_mode: str,
    company_access_level: str | None = None,
    confirm: bool = False,
) -> str:
    """Switch only an agent's access mode (write scope + manage + confirm).

    access_mode: company | private | custom. Existing user and department grants
    are preserved, never replaced; they are effective only while mode is custom.
    company_access_level is optional and only valid for company mode; omitting it
    preserves the previously configured company level."""
    return await set_agent_access_mode_impl(
        ctx,
        agent=agent,
        access_mode=access_mode,
        company_access_level=company_access_level,
        confirm=confirm,
    )


@mcp.tool()
async def grant_agent_access(  # noqa: D401,B006
    ctx: Context,
    agent: str,
    user_ids: list[str] = [],
    department_ids: list[str] = [],
    access_level: str = "use",
    confirm: bool = False,
) -> str:
    """Incrementally add or update custom access grants (write + manage + confirm).

    Requires custom mode. user_ids and department_ids must be platform UUIDs from
    the same tenant. Department grants always include descendants. Existing grants
    not named in this call are preserved. The request is validated atomically."""
    return await grant_agent_access_impl(
        ctx,
        agent=agent,
        user_ids=user_ids,
        department_ids=department_ids,
        access_level=access_level,
        confirm=confirm,
    )


@mcp.tool()
async def revoke_agent_access(  # noqa: D401,B006
    ctx: Context,
    agent: str,
    user_ids: list[str] = [],
    department_ids: list[str] = [],
    confirm: bool = False,
) -> str:
    """Incrementally remove exact custom grants (write scope + manage + confirm).

    Only the listed UUID grants are removed. Creator and active company-admin
    grants are protected. Other grants and relationship records stay unchanged."""
    return await revoke_agent_access_impl(
        ctx,
        agent=agent,
        user_ids=user_ids,
        department_ids=department_ids,
        confirm=confirm,
    )


# ── E7: list_agent_triggers ──
async def list_agent_triggers_impl(ctx, agent) -> str:
    from app.mcp_server.auth import resolve_pat_context
    from app.mcp_server.tools import _resolve_visible_agent
    from app.models.trigger import AgentTrigger

    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"
        ag = await _resolve_visible_agent(db, pc.user, agent)
        if ag is None:
            return (
                "❌ 找不到该 agent，或你无权访问。"
                "请用 list_agents 查看你可访问的 agent 列表，并以其 id 或准确名字重试。"
            )
        result = await db.execute(
            select(AgentTrigger)
            .where(AgentTrigger.agent_id == ag.id)
            .order_by(AgentTrigger.created_at.desc())
        )
        triggers = result.scalars().all()

    if not triggers:
        return f"「{ag.name}」暂无触发器。"

    lines = [f"「{ag.name}」的触发器列表（共 {len(triggers)} 个）："]
    for t in triggers:
        expires = t.expires_at.isoformat() if t.expires_at else "无"
        last_fired = t.last_fired_at.isoformat() if t.last_fired_at else "从未"
        lines.append(
            f"• {t.name} (id={t.id}) type={t.type} enabled={t.is_enabled}"
            f" config={t.config} reason={t.reason!r}"
            f" max_fires={t.max_fires} cooldown={t.cooldown_seconds}s"
            f" fire_count={t.fire_count} last_fired={last_fired} expires={expires}"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_agent_triggers(ctx: Context, agent: str) -> str:  # noqa: D401
    """List all autonomous triggers for an agent (read scope sufficient).
    agent: id or name. Returns each trigger's id, type, enabled state, config, reason,
    max_fires, cooldown, fire_count, last_fired, and expiry."""
    return await list_agent_triggers_impl(ctx, agent=agent)


# ── E8: update_agent_trigger ──
async def update_agent_trigger_impl(ctx, agent, trigger, is_enabled=None, config=None,
                                    reason=None, max_fires=None, cooldown_seconds=None,
                                    expires_at=None) -> str:
    from app.models.trigger import AgentTrigger

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        # Resolve trigger by id or name
        q = select(AgentTrigger).where(AgentTrigger.agent_id == ag.id)
        row = None
        try:
            tid = _uuid.UUID(str(trigger))
            row = (await db.execute(q.where(AgentTrigger.id == tid))).scalar_one_or_none()
        except (ValueError, TypeError):
            row = (await db.execute(q.where(AgentTrigger.name == trigger))).scalar_one_or_none()
        if row is None:
            return (
                f"❌ 找不到该触发器（你传入了 {trigger!r}，按 name 或 id 指定）。"
                "用 list_agent_triggers 查看该 agent 的触发器及其 id/名字，再重试。"
            )

        # Capture before-values of fields being changed
        changes: list[str] = []
        revert_parts: list[str] = []

        if is_enabled is not None:
            old = row.is_enabled
            row.is_enabled = is_enabled
            changes.append(f"is_enabled: {old} → {is_enabled}")
            revert_parts.append(f"is_enabled={old}")

        if config is not None:
            old = row.config
            row.config = config
            changes.append(f"config: {old} → {config}")
            revert_parts.append(f"config={old!r}")

        if reason is not None:
            old = row.reason
            row.reason = reason
            changes.append(f"reason: {old!r} → {reason!r}")
            revert_parts.append(f"reason={old!r}")

        if max_fires is not None:
            old = row.max_fires
            row.max_fires = max_fires
            changes.append(f"max_fires: {old} → {max_fires}")
            revert_parts.append(f"max_fires={old}")

        if cooldown_seconds is not None:
            old = row.cooldown_seconds
            row.cooldown_seconds = cooldown_seconds
            changes.append(f"cooldown_seconds: {old} → {cooldown_seconds}")
            revert_parts.append(f"cooldown_seconds={old}")

        if expires_at is not None:
            from datetime import datetime
            old_dt = row.expires_at
            row.expires_at = datetime.fromisoformat(expires_at)
            old_str = old_dt.isoformat() if old_dt else "None"
            changes.append(f"expires_at: {old_str} → {expires_at}")
            revert_parts.append(f"expires_at={old_str!r}")

        if not changes:
            return (
                f"（未提供任何更改字段：触发器「{row.name}」未变动。"
                "请至少传一个要修改的字段，如 is_enabled、config 或 reason。）"
            )

        await db.commit()

    revert_hint = (
        f"↩ 回滚：update_agent_trigger(agent=\"{ag.name}\", "
        f"trigger=\"{row.name}\", {', '.join(revert_parts)})"
    )
    return (
        f"✅ 已更新「{ag.name}」触发器「{row.name}」：\n"
        + "\n".join(f"  {c}" for c in changes)
        + f"\n{revert_hint}"
    )


@mcp.tool()
async def update_agent_trigger(ctx: Context, agent: str, trigger: str,  # noqa: D401
                               is_enabled: bool | None = None,
                               config: dict | None = None,
                               reason: str | None = None,
                               max_fires: int | None = None,
                               cooldown_seconds: int | None = None,
                               expires_at: str | None = None) -> str:
    """Update an existing trigger on an agent (write scope + manage).
    agent: id or name. trigger: the trigger's id or name.
    Provide only the fields you want to change; all others are left untouched.
    expires_at: ISO-8601 datetime string.
    Returns before→after summary and a revert hint for self-rollback."""
    return await update_agent_trigger_impl(ctx, agent=agent, trigger=trigger,
                                           is_enabled=is_enabled, config=config,
                                           reason=reason, max_fires=max_fires,
                                           cooldown_seconds=cooldown_seconds,
                                           expires_at=expires_at)


# ── E9: set_agent_tool_config ──
async def set_agent_tool_config_impl(ctx, agent, tool, config) -> str:
    from app.api.tools import _encrypt_sensitive_fields
    from app.models.tool import Tool  # noqa: F401  (Tool loaded via resolve_tenant_tool)
    if not isinstance(config, dict):
        return "❌ config 必须是对象（键值对）。"
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if "allow_network" in config and pc.user.role not in ("platform_admin", "org_admin"):
            return (
                "❌ 仅管理员可修改 allow_network（网络访问）设置。"
                "请联系 platform_admin 或 org_admin 进行操作。"
            )
        tool_row = await resolve_tenant_tool(db, ag.tenant_id, tool)
        if tool_row is None:
            return (
                f"❌ 找不到该工具（你传入了 {tool!r}）。"
                "用 list_available_tools 查看可用工具的名字或 id，并重试。"
            )
        # Capture prior config keys for rollback hint (do NOT decrypt/echo secret values)
        at = (await db.execute(select(AgentTool).where(
            AgentTool.agent_id == ag.id, AgentTool.tool_id == tool_row.id))).scalar_one_or_none()
        prior_keys = sorted((at.config or {}).keys()) if at and at.config else []
        encrypted = _encrypt_sensitive_fields(config, tool_row.config_schema)
        if at:
            at.config = encrypted
        else:
            db.add(AgentTool(agent_id=ag.id, tool_id=tool_row.id, enabled=True, config=encrypted))
        await db.commit()
        new_keys = sorted(config.keys())
        return (f"✅ 已设置「{ag.name}」工具 {tool_row.name} 的配置：键 {new_keys}。"
                f"\n↩ 回滚：之前的配置键为 {prior_keys or '（无）'}；用 set_agent_tool_config 传回旧值即可"
                f"（敏感值出于安全未回显，需自行重填）。")


@mcp.tool()
async def set_agent_tool_config(ctx: Context, agent: str, tool: str, config: dict) -> str:  # noqa: D401
    """Set a per-agent config override for an enabled tool (write scope + manage).
    agent: id or name. tool: tool id or name (see list_available_tools). config: a dict of
    config keys → values (e.g. API keys, params). Sensitive fields are encrypted at rest.
    'allow_network' is admin-only. Returns changed keys + a rollback hint (secret values are
    not echoed back for security)."""
    return await set_agent_tool_config_impl(ctx, agent=agent, tool=tool, config=config)
