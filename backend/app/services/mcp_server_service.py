"""MCP server runtime configuration assembly.

Pure functions (no DB / no HTTP). Reads ORM objects, returns a
resolved config object that ``mcp_invoker`` (added later in P3)
will pass through ``placeholder_engine.render`` then to MCPClient.

Composition rules (from design spec §3.3):
* ``system_prompt_block``: APPEND across server → tenant → agent layers
* ``url_template`` / ``headers_template`` / ``credential_template``:
  OVERRIDE — most-specific non-None wins (agent > tenant > server)
* headers_template uses replace (not deep-merge)
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.models.mcp_server import MCPServer, MCPServerOverride


@dataclass(frozen=True)
class ResolvedMCPConfig:
    url_template: str
    headers_template: dict
    credential_template: str | None
    prompt_blocks: list[str]
    transport: str = "http"
    command_template: str | None = None
    args_template: list | None = None
    env_template: dict | None = None


def _override_pick(*candidates):
    """Return first non-None argument, else None."""
    for c in candidates:
        if c is not None:
            return c
    return None


def compose_runtime_config(
    server: MCPServer,
    tenant_override: MCPServerOverride | None,
    agent_override: MCPServerOverride | None,
) -> ResolvedMCPConfig:
    layers = (server, tenant_override, agent_override)

    prompt_blocks = [
        layer.system_prompt_block for layer in layers if layer is not None and (layer.system_prompt_block or "").strip()
    ]

    url_template = _override_pick(
        agent_override.url_template if agent_override else None,
        tenant_override.url_template if tenant_override else None,
        server.base_url_template,
    )
    headers_template = (
        _override_pick(
            agent_override.headers_template if agent_override else None,
            tenant_override.headers_template if tenant_override else None,
            server.headers_template,
        )
        or {}
    )
    credential_template = _override_pick(
        agent_override.credential_template if agent_override else None,
        tenant_override.credential_template if tenant_override else None,
        server.credential_template,
    )

    transport = getattr(server, "transport", "http") or "http"
    command_template = _override_pick(
        getattr(agent_override, "command_template", None) if agent_override else None,
        getattr(tenant_override, "command_template", None) if tenant_override else None,
        getattr(server, "command_template", None),
    )
    args_template = _override_pick(
        getattr(agent_override, "args_template", None) if agent_override else None,
        getattr(tenant_override, "args_template", None) if tenant_override else None,
        getattr(server, "args_template", None),
    )
    env_template = _override_pick(
        getattr(agent_override, "env_template", None) if agent_override else None,
        getattr(tenant_override, "env_template", None) if tenant_override else None,
        getattr(server, "env_template", None),
    )

    return ResolvedMCPConfig(
        url_template=url_template,
        headers_template=headers_template,
        credential_template=credential_template,
        prompt_blocks=prompt_blocks,
        transport=transport,
        command_template=command_template,
        args_template=args_template,
        env_template=env_template,
    )


async def lookup_overrides(
    db,
    server_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
):
    """Fetch (tenant_override, agent_override) for given scopes; either may be None."""
    t_ovr = None
    a_ovr = None
    if tenant_id:
        t_ovr = (await db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == server_id,
                MCPServerOverride.scope_type == "tenant",
                MCPServerOverride.scope_id == tenant_id,
            )
        )).scalar_one_or_none()
    if agent_id:
        a_ovr = (await db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == server_id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == agent_id,
            )
        )).scalar_one_or_none()
    return t_ovr, a_ovr


async def build_placeholder_context_for_call(
    db,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str = "",
    channel_type: str = "web",
):
    """Build a PlaceholderContext for runtime MCP tool invocation.

    Sentinel handling: when ``user_id == agent_id`` (the convention used by
    trigger_daemon-driven LLM calls to mean "no human user"), fall back to
    ``Agent.creator_id`` for ``${user.*}`` resolution. This matches the
    brainstorm decision: cron/interval/poll-triggered MCP calls should
    identify themselves as the agent's owner, not the agent itself.
    """
    from app.models.agent import Agent
    from app.models.user import User
    from sqlalchemy.orm import selectinload
    from app.services.placeholder_engine import PlaceholderContext

    # Load agent (with tenant) and ensure we have it
    agent = (await db.execute(
        select(Agent).where(Agent.id == agent_id)
    )).scalar_one_or_none()
    if agent is None:
        return PlaceholderContext(
            session={"id": session_id or ""},
            channel={"type": channel_type or "web"},
        )

    # Sentinel detection: trigger-daemon path passes user_id=agent_id
    effective_user_id = user_id
    if effective_user_id is None or effective_user_id == agent_id:
        effective_user_id = agent.creator_id

    # Load user (with identity) for ${user.*}
    user_dict: dict[str, str] = {}
    if effective_user_id:
        u = (await db.execute(
            select(User).options(selectinload(User.identity))
              .where(User.id == effective_user_id)
        )).scalar_one_or_none()
        if u is not None:
            user_dict = {
                "id": str(u.id),
                "email": (u.identity.email if u.identity and u.identity.email else ""),
                "phone": (u.identity.phone if u.identity and getattr(u.identity, "phone", None) else ""),
                "name": u.display_name or "",
                "display_name": u.display_name or "",
            }

    # Tenant
    tenant_dict: dict[str, str] = {}
    if agent.tenant_id:
        tenant_dict = {"id": str(agent.tenant_id)}

    # Agent
    agent_dict = {
        "id": str(agent.id),
        "name": agent.name or "",
        "slug": getattr(agent, "slug", "") or "",
    }

    return PlaceholderContext(
        user=user_dict,
        agent=agent_dict,
        tenant=tenant_dict,
        session={"id": session_id or ""},
        channel={"type": channel_type or "web"},
    )


# ─── Bridge helper: find-or-create mcp_servers row ─────────────────────────


def _slugify_server_name(name: str, url_seed: str | None = None) -> str:
    """e.g. 'My RAGFlow' -> 'my_ragflow'.

    Pure-CJK (or otherwise ASCII-free) names slugify to the empty string. A
    bare ``"mcp_server"`` fallback would then collapse EVERY such server to the
    same base name, so their per-server tool names (``mcp_<srv.name>_<tool>``)
    would all collide on the global ``Tool.name`` — the root of the cross-wiring
    bug where one agent's MCP call routed to another's server/credential. When a
    ``url_seed`` is given, fall back to a short stable hash of it instead so each
    distinct endpoint gets a distinct, order-independent, underscore-only base.
    """
    s = re.sub(r"[^a-z0-9_-]", "_", name.lower()).strip("_")
    if s:
        return s
    if url_seed:
        return f"mcp_{hashlib.sha1(url_seed.encode('utf-8')).hexdigest()[:8]}"
    return "mcp_server"


async def persist_stdio_discovered_tools(
    db,
    srv,
    tools: list[dict],
) -> int:
    """Upsert Tool rows for tools discovered from a stdio MCP server.

    Idempotent: keyed on (mcp_server_id, mcp_tool_name).  Re-running discovery
    updates description/parameters_schema but never creates duplicates.

    Naming scheme: ``mcp_{srv.name}_{raw_tool_name}`` — matches the convention
    used by the HTTP MCP flow (see test_tools_mcp_server_bridge.py fixture).
    ``Tool.name`` is globally unique, so srv.name (unique per tenant in
    mcp_servers) makes collisions across servers impossible.

    Returns the number of tools persisted.
    """
    from app.models.tool import Tool
    from sqlalchemy import select

    upserted = 0
    for t in tools:
        raw_name: str = t.get("name") or ""
        description: str = t.get("description") or ""
        input_schema: dict = t.get("inputSchema") or {}
        if not raw_name:
            continue

        tool_name = f"mcp_{srv.name}_{raw_name}"
        display_name = raw_name.replace("_", " ").title()

        existing = (await db.execute(
            select(Tool).where(
                Tool.mcp_server_id == srv.id,
                Tool.mcp_tool_name == raw_name,
            )
        )).scalar_one_or_none()

        if existing is not None:
            # Update mutable fields; do not reset name to avoid breakage
            existing.description = description
            existing.parameters_schema = input_schema
        else:
            new_tool = Tool(
                name=tool_name,
                display_name=display_name,
                description=description,
                type="mcp",
                source="admin",
                tenant_id=srv.tenant_id,
                mcp_server_id=srv.id,
                mcp_server_name=srv.name,
                mcp_tool_name=raw_name,
                parameters_schema=input_schema,
            )
            db.add(new_tool)
        upserted += 1

    await db.flush()
    return upserted


async def upsert_mcp_server_from_tools(
    db,
    tenant_id: uuid.UUID | None,
    server_url: str,
    server_name: str,
    *,
    system_prompt_block: str | None = None,
    headers_template: dict | None = None,
    api_key: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Find or create an mcp_servers row for (tenant_id, server_url).

    Behavior:
    - Existing row: update only fields that are not None (None = "don't touch")
    - New row: create with provided fields; defaults system_prompt_block=None,
      headers_template={}, credential_template=None
    - Returns the row's id.
    - On name collision (uniq violation), suffixes -2/-3 etc. (matches P0a migration).
    """
    existing = (await db.execute(
        select(MCPServer).where(
            MCPServer.base_url_template == server_url,
            (MCPServer.tenant_id == tenant_id) if tenant_id is not None
            else MCPServer.tenant_id.is_(None),
        )
    )).scalar_one_or_none()

    if existing is not None:
        if system_prompt_block is not None:
            existing.system_prompt_block = system_prompt_block
        if headers_template is not None:
            existing.headers_template = headers_template
        if api_key is not None:
            existing.credential_template = api_key  # TODO encrypt at rest in P5
        await db.flush()
        return existing.id

    # Create new — derive unique name (seed the fallback from the URL so
    # ASCII-free names don't all collapse to the same base; see docstring).
    base = _slugify_server_name(server_name, url_seed=server_url)
    name = base
    suffix = 2
    while True:
        clash = (await db.execute(
            select(MCPServer).where(
                MCPServer.name == name,
                (MCPServer.tenant_id == tenant_id) if tenant_id is not None
                else MCPServer.tenant_id.is_(None),
            )
        )).scalar_one_or_none()
        if clash is None:
            break
        name = f"{base}-{suffix}"
        suffix += 1

    new_srv = MCPServer(
        tenant_id=tenant_id,
        name=name,
        display_name=server_name,
        base_url_template=server_url,
        headers_template=headers_template or {},
        credential_template=api_key,
        system_prompt_block=system_prompt_block,
        created_by_user_id=created_by_user_id,
    )
    db.add(new_srv)
    await db.flush()
    return new_srv.id


async def get_or_create_agent_stdio_server(db, agent_id, tenant_id, cfg: dict):
    """Create-or-reuse an agent-private stdio MCPServer from a parsed stdio cfg.

    Name = "{pkgslug}-a{agent8}" so two agents installing the same package get
    distinct servers (distinct creds, no Tool.name collision). Idempotent on name.
    cfg keys: command(str), args(list), env(dict).
    """
    from app.models.mcp_server import MCPServer
    from sqlalchemy import select

    # Derive a package slug from the most descriptive arg (last non-flag) or command.
    args = cfg.get("args") or []
    pkg = next((a for a in reversed(args) if not str(a).startswith("-")), cfg.get("command", "mcp"))
    pkg_slug = re.sub(r"[^a-z0-9]+", "-", str(pkg).lower()).strip("-")[:40] or "mcp"
    agent8 = str(agent_id).replace("-", "")[:8]
    name = f"{pkg_slug}-a{agent8}"

    existing = (await db.execute(
        select(MCPServer).where(
            MCPServer.name == name,
            (MCPServer.tenant_id == tenant_id) if tenant_id is not None
            else MCPServer.tenant_id.is_(None),
        )
    )).scalar_one_or_none()
    if existing is not None:
        # Refresh command/args/env in case the agent changed creds/args.
        existing.command_template = cfg.get("command")
        existing.args_template = cfg.get("args") or []
        existing.env_template = cfg.get("env") or {}
        await db.flush()
        return existing

    srv = MCPServer(
        name=name,
        display_name=pkg_slug,
        tenant_id=tenant_id,
        transport="stdio",
        command_template=cfg.get("command"),
        args_template=cfg.get("args") or [],
        env_template=cfg.get("env") or {},
        base_url_template="",
        headers_template={},
        created_by_user_id=None,
    )
    db.add(srv)
    await db.flush()
    return srv
