"""Channel and MCP extension prompt collection for agent context."""

import uuid

from app.config import get_settings
from sqlalchemy import select
from app.models.tool import Tool, AgentTool
from app.services.turn_tool_settings import current_tool_settings, effective_mcp_override


def _enabled_tools_query(agent_id):
    scope = current_tool_settings(agent_id)
    if scope is not None:
        return select(Tool).where(Tool.id.in_([item["tool_id"] for item in scope.assignments]), Tool.enabled.is_(True))
    return select(Tool).join(AgentTool, AgentTool.tool_id == Tool.id).where(
        AgentTool.agent_id == agent_id, AgentTool.enabled.is_(True), Tool.enabled.is_(True),
    )


async def _collect_channel_prompts(agent_id: uuid.UUID) -> list[str]:
    """Collect channel-driven prompt blocks for an agent.

    Per-agent override in ``channel_configs.system_prompt_block`` wins;
    otherwise falls back to the type-level default in
    ``channel_type_defaults``. Shared by both the legacy and new MCP
    collector paths so both stay in lockstep.
    """
    from app.database import async_session
    from app.models.channel_config import ChannelConfig
    from app.models.channel_type_default import ChannelTypeDefault
    from sqlalchemy import select

    blocks: list[str] = []

    async with async_session() as db:
        ch_rows = await db.execute(
            select(ChannelConfig)
            .where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.is_configured == True,  # noqa: E712
            )
            .order_by(ChannelConfig.channel_type)
        )
        channel_configs = ch_rows.scalars().all()

        if channel_configs:
            type_defaults_rows = await db.execute(
                select(ChannelTypeDefault).where(
                    ChannelTypeDefault.channel_type.in_([c.channel_type for c in channel_configs])
                )
            )
            type_defaults: dict[str, str] = {
                row.channel_type: (row.system_prompt_block or "").strip() for row in type_defaults_rows.scalars().all()
            }

            for cfg in channel_configs:
                override = (cfg.system_prompt_block or "").strip()
                if override:
                    blocks.append(override)
                    continue
                fallback = type_defaults.get(cfg.channel_type, "")
                if fallback:
                    blocks.append(fallback)

    return blocks


async def _collect_extension_prompts_legacy(agent_id: uuid.UUID) -> list[str]:
    """Legacy collector: reads prompt blocks from ``tools.system_prompt_block``
    and ``tools.mcp_server_instructions`` (pre-P0b path).

    Two sources, in this output order:

    1. ``tools.system_prompt_block`` — DBA-fillable per-tool prompts (e.g.
       the ragflow citation rules). Emitted in tool-name order so the
       static prompt prefix is byte-stable across requests; that prefix
       stability is what lets prompt caching land hits.
    2. ``tools.mcp_server_instructions`` — server-provided instructions
       captured during the MCP ``initialize`` handshake. Deduplicated by
       ``mcp_server_url``; servers without instructions contribute
       nothing.

    Channel prompts are always appended via the shared
    ``_collect_channel_prompts`` helper.
    """
    from app.database import async_session

    blocks: list[str] = []

    async with async_session() as db:
        # Tool-driven blocks. We sort by Tool.name so identical agent
        # configurations always produce a byte-identical prefix.
        tool_rows = await db.execute(
            _enabled_tools_query(agent_id)
            .order_by(Tool.name)
        )
        tools = tool_rows.scalars().all()

        for tool in tools:
            block = (tool.system_prompt_block or "").strip()
            if block:
                blocks.append(block)

        # MCP server instructions — one per distinct server URL.
        seen_servers: set[str] = set()
        for tool in tools:
            if tool.type != "mcp":
                continue
            url = (tool.mcp_server_url or "").strip()
            if not url or url in seen_servers:
                continue
            seen_servers.add(url)
            instr = (tool.mcp_server_instructions or "").strip()
            if instr:
                blocks.append(instr)

    return blocks + (await _collect_channel_prompts(agent_id))


async def _collect_mcp_prompts_from_servers(agent_id: uuid.UUID) -> list[str]:
    """New collector: read from mcp_servers + mcp_server_overrides.

    Output ordering: server.name asc (deterministic for prompt-cache
    byte stability). Per-server prompt = server.system_prompt_block +
    tenant_override.system_prompt_block + agent_override.system_prompt_block,
    appended with blank lines and empty layers skipped.

    P0b: NO placeholder rendering yet (PROMPT_SAFE_ROOTS work happens
    when prompt-context-aware vars become available; for foundation we
    just append raw text and assert byte-stability vs old path).
    """
    from app.database import async_session
    from app.models.agent import Agent
    from app.models.mcp_server import MCPServer, MCPServerOverride

    blocks: list[str] = []
    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        tenant_id = agent.tenant_id if agent is not None else None

        # Distinct server ids enabled for this agent
        srv_id_rows = await db.execute(
            _enabled_tools_query(agent_id).with_only_columns(Tool.mcp_server_id)
            .where(Tool.mcp_server_id.is_not(None))
            .distinct()
        )
        server_ids = [r[0] for r in srv_id_rows.all() if r[0] is not None]
        if not server_ids:
            return []

        servers = (
            (await db.execute(select(MCPServer).where(MCPServer.id.in_(server_ids)).order_by(MCPServer.name)))
            .scalars()
            .all()
        )

        # Bulk-load overrides for these servers
        ovr_rows = (
            (await db.execute(select(MCPServerOverride).where(MCPServerOverride.mcp_server_id.in_(server_ids))))
            .scalars()
            .all()
        )
        ovr_index: dict[tuple[uuid.UUID, str, uuid.UUID], MCPServerOverride] = {
            (o.mcp_server_id, o.scope_type, o.scope_id): o for o in ovr_rows
        }

        for srv in servers:
            t_ovr = ovr_index.get((srv.id, "tenant", tenant_id)) if tenant_id else None
            a_ovr = effective_mcp_override(agent_id, srv.id, ovr_index.get((srv.id, "agent", agent_id)))
            parts = [
                (srv.system_prompt_block or "").strip(),
                (t_ovr.system_prompt_block or "").strip() if t_ovr else "",
                (a_ovr.system_prompt_block or "").strip() if a_ovr else "",
            ]
            merged = "\n\n".join(p for p in parts if p)
            if merged:
                blocks.append(merged)

            # Server instructions captured during initialize handshake
            instr = (srv.instructions or "").strip()
            if instr:
                blocks.append(instr)

    return blocks


async def _collect_extension_prompts(agent_id: uuid.UUID) -> list[str]:
    """Dispatch to the new server-table-based collector by default.
    Set ``MCP_USE_LEGACY_COLLECTOR=1`` env to fall back to the
    pre-P0b path that reads tools.system_prompt_block directly.

    Channel-driven blocks (channel_configs / channel_type_defaults)
    use the same path in both modes — only the MCP source differs.
    """
    if get_settings().MCP_USE_LEGACY_COLLECTOR:
        return await _collect_extension_prompts_legacy(agent_id)
    mcp_blocks = await _collect_mcp_prompts_from_servers(agent_id)
    channel_blocks = await _collect_channel_prompts(agent_id)
    return mcp_blocks + channel_blocks
