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
