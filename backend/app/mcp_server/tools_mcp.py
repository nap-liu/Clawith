"""MCP tools for installing/uninstalling MCP servers on agents.

install_agent_mcp_server  — adds a Smithery-hosted MCP server to an agent's toolset
uninstall_agent_mcp_server — removes a previously installed MCP server from an agent

Both are sensitive / confirm-guided: the first call without confirm=True returns a
description of what would happen; call again with confirm=True to execute.
install ↔ uninstall are exact inverses (each is the rollback for the other).
"""

from __future__ import annotations

import uuid as _uuid

from mcp.server.fastmcp import Context
from sqlalchemy import delete, func, or_, select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, needs_confirm, resolve_manageable_agent
from app.mcp_server.auth import resolve_pat_context
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool

_UNAUTH = "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"


def _visible_server_clause(tenant_id):
    """MCP servers a caller may reference: same tenant + global (tenant_id IS NULL) templates."""
    if tenant_id:
        return or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None))
    return MCPServer.tenant_id.is_(None)


async def _resolve_local_mcp_server(db, tenant_id, server_id: str):
    """Resolve a server_id (UUID or name) to a locally-registered MCPServer the
    caller may reference (same tenant + global templates), or None."""
    visible = _visible_server_clause(tenant_id)
    try:
        sid = _uuid.UUID(str(server_id))
        row = (await db.execute(select(MCPServer).where(MCPServer.id == sid, visible))).scalar_one_or_none()
        if row is not None:
            return row
    except (ValueError, TypeError):
        pass
    rows = (await db.execute(select(MCPServer).where(MCPServer.name == server_id, visible))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def _install_local_mcp_server(db, ag, srv, confirm: bool) -> str:
    """Install a locally-registered MCP server onto an agent.

    Prefer associating the server's already-discovered Tool rows (built by the
    admin test-connection flow) — this is the exact inverse of uninstall and
    avoids re-connecting / placeholder resolution. If no tools have been
    discovered yet, fall back to a live direct import for http/sse servers.
    """
    tools = (await db.execute(select(Tool).where(Tool.mcp_server_id == srv.id))).scalars().all()

    if tools:
        guidance = needs_confirm(
            confirm,
            f"将为「{ag.name}」安装本平台已注册的 MCP server「{srv.name}」（关联 {len(tools)} 个已发现的工具）",
        )
        if guidance:
            return guidance

        existing = {
            at.tool_id
            for at in (await db.execute(select(AgentTool).where(AgentTool.agent_id == ag.id))).scalars().all()
        }
        added = 0
        for t in tools:
            if t.id in existing:
                continue
            db.add(
                AgentTool(
                    agent_id=ag.id,
                    tool_id=t.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=ag.id,
                )
            )
            added += 1
        await db.commit()

        names = ", ".join((t.display_name or t.name) for t in tools[:5])
        more = f"…+{len(tools) - 5}" if len(tools) > 5 else ""
        already = f"，另 {len(tools) - added} 个此前已启用" if added < len(tools) else ""
        return (
            f"✅ 已为「{ag.name}」安装本平台已注册的 MCP server「{srv.name}」"
            f"（关联 {added} 个工具{already}：{names}{more}）。\n"
            f'↩ 回滚：uninstall_agent_mcp_server(agent="{ag.name}", server="{srv.name}", confirm=True)'
        )

    # No tools discovered yet.
    if srv.transport in ("http", "sse"):
        guidance = needs_confirm(
            confirm,
            f"将为「{ag.name}」安装本平台已注册的 MCP server「{srv.name}」"
            f"（该 server 尚无已发现工具，将现场直连 {srv.base_url_template} 抓取）",
        )
        if guidance:
            return guidance
        from app.services.resource_discovery import import_mcp_direct

        result = await import_mcp_direct(
            srv.base_url_template,
            ag.id,
            server_name=srv.display_name,
            api_key=srv.credential_template,
            headers=srv.headers_template,
        )
        if isinstance(result, str) and not result.startswith("❌"):
            result += f'\n↩ 回滚：uninstall_agent_mcp_server(agent="{ag.name}", server="{srv.name}", confirm=True)'
        return result

    # stdio without discovered tools — hub-based live import is heavier; guide to admin.
    return (
        f"❌ 「{srv.name}」是 stdio 类 MCP server 且尚无已发现的工具。"
        "请先在管理端对该 server 执行「测试连接」以抓取工具，然后再安装。"
    )


async def install_agent_mcp_server_impl(
    ctx,
    agent: str,
    server_id: str,
    smithery_api_key: str | None = None,
    config: dict | None = None,
    confirm: bool = False,
) -> str:
    """Core logic for install_agent_mcp_server (testable without @mcp.tool decorator)."""
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        # Prefer a locally-registered MCP server (this tenant / global) over Smithery.
        local_srv = await _resolve_local_mcp_server(db, pc.tenant_id, server_id)
        if local_srv is not None:
            return await _install_local_mcp_server(db, ag, local_srv, confirm)

    # Fallback: import an unknown server from the Smithery registry.
    guidance = needs_confirm(confirm, f"将为「{ag.name}」安装 MCP server '{server_id}'（来自 Smithery）")
    if guidance:
        return guidance

    # Build config dict and inject key if provided
    cfg = dict(config or {})
    if smithery_api_key:
        cfg["smithery_api_key"] = smithery_api_key

    from app.services.resource_discovery import import_mcp_from_smithery

    result = await import_mcp_from_smithery(server_id, ag.id, cfg)

    if not result.startswith("❌"):
        result += f'\n↩ 回滚：uninstall_agent_mcp_server(agent="{ag.name}", server="{server_id}")'

    return result


async def uninstall_agent_mcp_server_impl(
    ctx,
    agent: str,
    server: str,
    confirm: bool = False,
) -> str:
    """Core logic for uninstall_agent_mcp_server (testable without @mcp.tool decorator)."""
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err

        # Resolve the MCPServer by name or UUID (optional FK — may be NULL for older rows)
        from app.models.mcp_server import MCPServer

        mcp_server = None
        try:
            srv_uuid = _uuid.UUID(str(server))
            mcp_server = (await db.execute(select(MCPServer).where(MCPServer.id == srv_uuid))).scalar_one_or_none()
        except (ValueError, TypeError):
            pass
        if mcp_server is None:
            rows = (await db.execute(select(MCPServer).where(MCPServer.name == server))).scalars().all()
            if len(rows) == 1:
                mcp_server = rows[0]

        # Find AgentTool rows for this agent that were installed by this agent
        # and whose Tool links back to the target server (by mcp_server_id FK or mcp_server_name string).
        at_q = (
            select(AgentTool, Tool)
            .join(Tool, AgentTool.tool_id == Tool.id)
            .where(
                AgentTool.agent_id == ag.id,
                AgentTool.installed_by_agent_id == ag.id,
            )
        )
        if mcp_server is not None:
            # Prefer FK lookup — but also accept name match for tools that have no FK yet
            from sqlalchemy import or_

            at_q = at_q.where(
                or_(
                    Tool.mcp_server_id == mcp_server.id,
                    Tool.mcp_server_name == mcp_server.name,
                )
            )
        else:
            # No MCPServer row found: match by mcp_server_name string (server arg)
            at_q = at_q.where(Tool.mcp_server_name == server)

        pairs = (await db.execute(at_q)).all()

        if not pairs:
            return (
                f"❌ 该 agent 未安装名为 '{server}' 的 MCP server。"
                "请检查 server 名字是否正确，或先调用 install_agent_mcp_server 安装它。"
            )

        agent_tool_ids = [at.id for at, _t in pairs]
        tool_ids = list({t.id for _at, t in pairs})
        tool_names = [t.name for _at, t in pairs]
        n = len(agent_tool_ids)

        guidance = needs_confirm(confirm, f"将卸载「{ag.name}」的 MCP server '{server}'（移除 {n} 个工具）")
        if guidance:
            return guidance

        # Delete AgentTool assignments first
        await db.execute(delete(AgentTool).where(AgentTool.id.in_(agent_tool_ids)))
        await db.flush()

        # Mirror delete_agent_tool API: delete Tool row if no other agent still has it
        for tool_id in tool_ids:
            remaining = (
                await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id).limit(1))
            ).scalar_one_or_none()
            if remaining is None:
                tool_r = (await db.execute(select(Tool).where(Tool.id == tool_id))).scalar_one_or_none()
                if tool_r and tool_r.type == "mcp":
                    await db.delete(tool_r)

        await db.commit()

    names_preview = ", ".join(tool_names[:5])
    if n > 5:
        names_preview += f"…+{n - 5}"
    return (
        f"✅ 已卸载「{ag.name}」的 '{server}'（移除 {n} 个工具：{names_preview}）。\n"
        f"↩ 回滚：用 install_agent_mcp_server 重新安装"
    )


@mcp.tool()
async def install_agent_mcp_server(
    ctx: Context,
    agent: str,
    server_id: str,
    smithery_api_key: str | None = None,
    config: dict | None = None,
    confirm: bool = False,
) -> str:
    """Install a Smithery-hosted MCP server on an agent (requires write-scope PAT + manage access).

    SENSITIVE — confirm-guided: call once to see what will happen, then pass confirm=True to execute.

    agent: agent id or name.
    server_id: Smithery server identifier (e.g. "github", "@smithery-ai/filesystem").
    smithery_api_key: your Smithery API key (https://smithery.ai/account/api-keys); required on first use
      per agent (subsequently stored and reused automatically).
    config: optional extra config dict passed through to import_mcp_from_smithery.
    confirm: pass True to actually execute after reviewing the confirmation prompt.

    Installed tools appear as source="user_installed" Tool rows linked to this agent.
    Rollback: call uninstall_agent_mcp_server(agent=..., server=server_id).
    """
    return await install_agent_mcp_server_impl(
        ctx,
        agent=agent,
        server_id=server_id,
        smithery_api_key=smithery_api_key,
        config=config,
        confirm=confirm,
    )


@mcp.tool()
async def uninstall_agent_mcp_server(
    ctx: Context,
    agent: str,
    server: str,
    confirm: bool = False,
) -> str:
    """Uninstall a previously installed MCP server from an agent (requires write-scope PAT + manage access).

    SENSITIVE — confirm-guided: call once to see what will happen, then pass confirm=True to execute.

    agent: agent id or name.
    server: the Smithery server id or MCPServer name as installed (e.g. "github").
    confirm: pass True to actually execute after reviewing the confirmation prompt.

    Only removes user_installed Tool rows installed by this agent for the specified server — builtin
    and admin-managed tools are never touched.
    Rollback: call install_agent_mcp_server to reinstall the server.
    """
    return await uninstall_agent_mcp_server_impl(ctx, agent=agent, server=server, confirm=confirm)


async def list_mcp_servers_impl(ctx) -> str:
    """Core logic for list_mcp_servers (testable without @mcp.tool decorator)."""
    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return _UNAUTH
        rows = (
            (await db.execute(select(MCPServer).where(_visible_server_clause(pc.tenant_id)).order_by(MCPServer.name)))
            .scalars()
            .all()
        )
        if not rows:
            return (
                "（本租户暂无已注册的 MCP server）"
                "可用 install_agent_mcp_server 从 Smithery 导入，或联系管理员在企业设置中注册。"
            )
        lines = []
        for s in rows:
            cnt = (
                await db.execute(select(func.count()).select_from(Tool).where(Tool.mcp_server_id == s.id))
            ).scalar() or 0
            scope = "全局" if s.tenant_id is None else "本租户"
            lines.append(
                f"- {s.display_name or s.name} (id={s.id}, name={s.name}) — "
                f"transport={s.transport} · 已发现工具={cnt} · {scope}"
            )
        return "本平台已注册的 MCP server（用 install_agent_mcp_server 以 id 或 name 安装到 agent）：\n" + "\n".join(
            lines
        )


@mcp.tool()
async def list_mcp_servers(ctx: Context) -> str:  # noqa: D401
    """List MCP servers already registered on this platform that you can install onto an agent
    (read scope sufficient).

    Shows each server's id, name, transport, and how many tools have been discovered for it.
    Pass a listed id or name as install_agent_mcp_server(server_id=...) to attach that server's
    tools to an agent — no Smithery API key needed for these. Only servers in your tenant (plus
    global templates) are shown."""
    return await list_mcp_servers_impl(ctx)
