"""MCP configuration tools (write scope, all require manage)."""
from __future__ import annotations

import uuid as _uuid

from mcp.server.fastmcp import Context
from sqlalchemy import select

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
            msg += f"\n⚠ 找不到这些工具（用 list_available_tools 查看）：{', '.join(missing)}"
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
            return "❌ 找不到该触发器（按 name 或 id 指定）。"
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
    poll: {"url": "..."}; on_message: {"from_agent_name": "..."} or {"from_user_name": "..."}; webhook: {} (token auto-issued).
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
    from app.models.org import AgentAgentRelationship
    from app.mcp_server.tools import _resolve_visible_agent

    if mode == "replace":
        from sqlalchemy import delete
        await db.execute(delete(AgentAgentRelationship).where(
            AgentAgentRelationship.agent_id == source_agent.id))
        await db.flush()

    existing_result = await db.execute(select(AgentAgentRelationship).where(
        AgentAgentRelationship.agent_id == source_agent.id))
    existing_by_target = {r.target_agent_id: r for r in existing_result.scalars().all()}

    for link in links:
        ref = link.get("target_agent")
        if not ref:
            errors.append("(A2A 缺少 target_agent)")
            continue
        target = await _resolve_visible_agent(db, current_user, str(ref))
        if target is None:
            errors.append(f"A2A:{ref}")
            continue
        if target.id == source_agent.id:
            errors.append(f"A2A:{ref}(不能关联自身)")
            continue
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


async def _apply_human_links(db, current_user, source_agent, links, mode, added, errors):
    """Wire agent-to-human relationships. Mutates added/errors in place."""
    from app.core.permissions import get_agent_access_level_for_user_id
    from app.models.org import AgentRelationship, OrgMember
    from app.models.user import User

    if mode == "replace":
        from sqlalchemy import delete
        await db.execute(delete(AgentRelationship).where(
            AgentRelationship.agent_id == source_agent.id))
        await db.flush()

    existing_result = await db.execute(select(AgentRelationship).where(
        AgentRelationship.agent_id == source_agent.id))
    existing_by_member = {r.member_id: r for r in existing_result.scalars().all()}

    for link in links:
        ref = link.get("user")
        if not ref:
            errors.append("(human 缺少 user)")
            continue
        ref = str(ref)
        member = None
        if ref.startswith("platform-user:"):
            try:
                platform_user_id = _uuid.UUID(ref.split(":", 1)[1])
            except (ValueError, TypeError):
                errors.append(f"human:{ref}")
                continue
            user_result = await db.execute(select(User).where(
                User.id == platform_user_id,
                User.tenant_id == source_agent.tenant_id,
                User.is_active == True,  # noqa: E712
            ))
            platform_user = user_result.scalar_one_or_none()
            if not platform_user:
                errors.append(f"human:{ref}")
                continue
            if not await get_agent_access_level_for_user_id(db, platform_user.id, source_agent):
                errors.append(f"human:{ref}(无权访问)")
                continue
            member_result = await db.execute(select(OrgMember).where(
                OrgMember.tenant_id == source_agent.tenant_id,
                OrgMember.user_id == platform_user.id,
                OrgMember.status == "active",
            ))
            member = member_result.scalar_one_or_none()
            if not member:
                member = OrgMember(
                    tenant_id=source_agent.tenant_id,
                    user_id=platform_user.id,
                    external_id=f"platform:{platform_user.id}",
                    name=platform_user.display_name or platform_user.username
                    or platform_user.email or str(platform_user.id),
                    email=platform_user.email,
                    avatar_url=platform_user.avatar_url,
                    title=platform_user.title or "",
                    department_path="",
                    status="active",
                )
                db.add(member)
                await db.flush()
        else:
            try:
                member_id = _uuid.UUID(ref)
            except (ValueError, TypeError):
                errors.append(f"human:{ref}")
                continue
            member_result = await db.execute(select(OrgMember).where(OrgMember.id == member_id))
            member = member_result.scalar_one_or_none()

        if not member or member.tenant_id != source_agent.tenant_id or member.status != "active":
            errors.append(f"human:{ref}")
            continue
        if member.user_id and not await get_agent_access_level_for_user_id(db, member.user_id, source_agent):
            errors.append(f"human:{ref}(无权访问)")
            continue

        relation = link.get("relation") or "collaborator"
        description = link.get("description") or ""
        existing = existing_by_member.get(member.id)
        if existing is not None:
            existing.relation = relation
            existing.description = description
            existing.updated_by_user_id = current_user.id
        else:
            row = AgentRelationship(
                agent_id=source_agent.id,
                member_id=member.id,
                relation=relation,
                description=description,
                created_by_user_id=current_user.id,
                updated_by_user_id=current_user.id,
            )
            db.add(row)
            existing_by_member[member.id] = row
        added.append(f"👤{member.name}")


async def set_agent_relationships_impl(ctx, agent, agent_links=None, human_links=None, mode="merge") -> str:
    from app.api.relationships import _regenerate_relationships_file
    agent_links = agent_links or []
    human_links = human_links or []
    if mode not in ("merge", "replace"):
        return "❌ mode 只能是 merge 或 replace。"
    if not agent_links and not human_links:
        return "（未提供任何关系）"
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
async def set_agent_relationships(ctx: Context, agent: str, agent_links: list[dict] = [],  # noqa: D401,B006
                                  human_links: list[dict] = [], mode: str = "merge") -> str:
    """Wire an agent's collaboration relationships (write scope + manage).
    agent: id or name of the agent to configure.
    agent_links: list of {"target_agent": id|name, "relation"?: str, "description"?: str} —
      agent-to-agent (A2A) links; the target must be VISIBLE to the calling user.
    human_links: list of {"user": "platform-user:<uuid>" | "<member_id>", "relation"?: str, "description"?: str} —
      agent-to-human links; the user must have access to this agent.
    mode: "merge" (default, upsert) keeps existing links and adds/updates the given ones;
      "replace" wipes existing links of EACH provided kind first, then sets the given ones.
    Targets that can't be found or aren't permitted are skipped (reported), not fatal."""
    return await set_agent_relationships_impl(ctx, agent=agent, agent_links=agent_links,
                                              human_links=human_links, mode=mode)


# ── E6: set_agent_access ──
async def set_agent_access_impl(ctx, agent, access_mode, access_level="use",
                                grant_user_ids=None, confirm=False) -> str:
    import uuid as _uuid_mod
    from sqlalchemy import delete as sql_delete
    from app.models.agent import AgentPermission
    from app.services.access_relationships import ensure_access_granted_platform_relationships
    from app.api.agents import _get_active_admin_users
    from app.api.relationships import _regenerate_relationships_file
    from app.mcp_server._common import needs_confirm

    grant_user_ids = grant_user_ids or []
    if access_mode not in ("company", "private", "custom"):
        return "❌ access_mode 只能是 company、private 或 custom。"

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        # Capture BEFORE state
        old_mode = ag.access_mode
        old_level = ag.company_access_level
        existing_perms = (await db.execute(
            select(AgentPermission).where(AgentPermission.agent_id == ag.id)
        )).scalars().all()
        old_user_ids = [str(p.scope_id) for p in existing_perms if p.scope_type == "user" and p.scope_id]

        # Build preview for confirm guard
        grant_count_hint = f"，授权 {len(grant_user_ids)} 人" if access_mode == "custom" and grant_user_ids else ""
        preview = (
            f"将把「{ag.name}」访问模式改为 {access_mode}"
            f"（access_level={access_level}{grant_count_hint}）；"
            f"当前为 {old_mode}"
        )
        guidance = needs_confirm(confirm, preview)
        if guidance is not None:
            return guidance

        # Validate grant_user_ids are UUIDs
        parsed_grant_ids: list[_uuid_mod.UUID] = []
        for uid_str in grant_user_ids:
            try:
                parsed_grant_ids.append(_uuid_mod.UUID(str(uid_str)))
            except (ValueError, TypeError):
                return f"❌ grant_user_ids 包含无效 UUID：{uid_str}"

        # Delete existing permissions
        await db.execute(sql_delete(AgentPermission).where(AgentPermission.agent_id == ag.id))

        # Insert new permissions (mirror update_agent_permissions REST logic)
        if access_mode == "company":
            ag.access_mode = "company"
            ag.company_access_level = access_level
            db.add(AgentPermission(agent_id=ag.id, scope_type="company", access_level=access_level))
        elif access_mode == "private":
            ag.access_mode = "private"
            ag.company_access_level = access_level
            creator_id = ag.creator_id or pc.user.id
            db.add(AgentPermission(agent_id=ag.id, scope_type="user", scope_id=creator_id, access_level="manage"))
        elif access_mode == "custom":
            ag.access_mode = "custom"
            ag.company_access_level = access_level
            seen_user_ids: set[_uuid_mod.UUID] = set()
            creator_id = ag.creator_id or pc.user.id
            required_manager_ids = {creator_id}
            required_manager_ids.update(admin.id for admin in await _get_active_admin_users(db, ag.tenant_id))
            # Add explicitly granted users
            for uid in parsed_grant_ids:
                if uid in seen_user_ids:
                    continue
                lvl = access_level
                if uid in required_manager_ids:
                    lvl = "manage"
                seen_user_ids.add(uid)
                db.add(AgentPermission(agent_id=ag.id, scope_type="user", scope_id=uid, access_level=lvl))
            # Ensure creator + admins always have manage
            for uid in required_manager_ids:
                if uid not in seen_user_ids:
                    db.add(AgentPermission(agent_id=ag.id, scope_type="user", scope_id=uid, access_level="manage"))

        await db.flush()
        relationships_changed = await ensure_access_granted_platform_relationships(
            db, ag, created_by_user_id=pc.user.id
        )
        if relationships_changed:
            await _regenerate_relationships_file(db, ag.id)
        await db.commit()

    old_user_hint = f"，旧授权 user_ids={old_user_ids}" if old_user_ids else ""
    revert = (
        f"↩ 回滚：set_agent_access(agent=\"{ag.name}\", "
        f"access_mode=\"{old_mode}\", access_level=\"{old_level}\"{old_user_hint})"
    )
    return (
        f"✅ 已更新「{ag.name}」访问：{old_mode} → {access_mode}"
        f"（access_level={access_level}）\n{revert}"
    )


@mcp.tool()
async def set_agent_access(ctx: Context, agent: str, access_mode: str,  # noqa: D401
                           access_level: str = "use", grant_user_ids: list[str] = [],  # noqa: B006
                           confirm: bool = False) -> str:
    """Set agent access permissions (write scope + manage). SENSITIVE — requires confirm=True.

    access_mode: "company" (all tenant members), "private" (creator only), or
      "custom" (explicit user list; grant_user_ids are UUIDs of users to grant).
    access_level: "use" (default) or "manage" for company/custom modes.
    grant_user_ids: list of user UUIDs (required for custom mode); creator + admins
      are always kept as managers regardless.
    confirm: must be True to execute; first call without confirm shows a preview and
      does NOT make any changes.

    Returns before→after summary and a revert hint so you can self-rollback."""
    return await set_agent_access_impl(ctx, agent=agent, access_mode=access_mode,
                                       access_level=access_level,
                                       grant_user_ids=grant_user_ids, confirm=confirm)


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
            return "❌ 找不到该 agent，或你无权访问。"
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
            return "❌ 找不到该触发器（按 name 或 id 指定）。"

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
            return f"（未提供任何更改字段：触发器「{row.name}」未变动）"

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
            return "❌ 仅管理员可修改 allow_network（网络访问）设置。"
        tool_row = await resolve_tenant_tool(db, ag.tenant_id, tool)
        if tool_row is None:
            return "❌ 找不到该工具（用 list_available_tools 查看）。"
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
