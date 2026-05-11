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

    return ResolvedMCPConfig(
        url_template=url_template,
        headers_template=headers_template,
        credential_template=credential_template,
        prompt_blocks=prompt_blocks,
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
