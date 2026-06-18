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


# ── E5: edit_agent_soul ──
async def edit_agent_soul_impl(ctx, agent, personality=None, boundaries=None) -> str:
    from app.services.agent_manager import agent_manager, replace_or_append_section
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if personality is None and boundaries is None:
            return "（未提供 personality 或 boundaries）"
        soul_path = agent_manager._agent_dir(ag.id) / "soul.md"
        if not soul_path.exists():
            return "❌ 该 agent 还没有 soul.md（可能是 openclaw 节点或尚未初始化）。"
        content = soul_path.read_text(encoding="utf-8")
        changed = []
        if personality is not None:
            content = replace_or_append_section(content, "Personality", personality)
            changed.append("Personality")
        if boundaries is not None:
            content = replace_or_append_section(content, "Boundaries", boundaries)
            changed.append("Boundaries")
        soul_path.write_text(content, encoding="utf-8")
        return f"✅ 已更新「{ag.name}」soul.md 的：{', '.join(changed)}。"


@mcp.tool()
async def edit_agent_soul(ctx: Context, agent: str, personality: str | None = None,  # noqa: D401
                          boundaries: str | None = None) -> str:
    """Edit an agent's soul.md Personality / Boundaries sections (write scope + manage).
    agent: id or name. Pass personality and/or boundaries to replace those sections
    (created if absent). Other soul.md content is left untouched."""
    return await edit_agent_soul_impl(ctx, agent=agent, personality=personality, boundaries=boundaries)


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
