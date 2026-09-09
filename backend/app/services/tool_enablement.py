"""Pure resolution + seed planning for per-agent tool enablement.

Single source of truth for "does this agent have this tool enabled?".

Rule (EXPLICIT-ONLY): a configurable tool is enabled for an agent iff there is
an ``AgentTool`` row with ``enabled=True``. Absence of a row means NOT enabled.
Required protocol tools are the narrow exception and always resolve enabled.
``Tool.is_default`` is NOT consulted at resolution time — it is only a
seed-time template (see the planners below), read once when an agent is
created or during the one-time backfill.

All functions here are pure (no DB I/O) so they can be unit-tested with
SimpleNamespace stubs, matching this repo's test convention.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, or_, select

from app.models.tool import Tool
from app.models.mcp_server import MCPServer

# Protocol tools whose schemas must remain present for every Agent. Keeping
# this set here makes the runtime resolver, management APIs, and startup seed
# agree on one authoritative rule.
REQUIRED_AGENT_TOOL_NAMES = frozenset({"send_media"})

# One user-facing capability in the tool panel. The four protocol functions
# remain separate LLM tools, but their per-Agent enabled state moves together.
SUBAGENT_TOOL_NAMES = frozenset(
    {
        "run_subagent",
        "send_message_to_subagent",
        "stop_subagent",
        "send_message_to_parent",
    }
)


def tool_visibility_clause(tenant_id, assigned_tool_ids):
    """Use one tenant boundary for catalog, scene saves and runtime tools."""
    return and_(
        or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
        or_(Tool.source.in_(["builtin", "admin"]), Tool.id.in_(assigned_tool_ids)),
        or_(
            Tool.type != "mcp",
            Tool.mcp_server_id.is_(None),
            _mcp_server_visible(tenant_id),
        ),
    )


def _mcp_server_visible(tenant_id):
    return select(MCPServer.id).where(
        MCPServer.id == Tool.mcp_server_id,
        or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)),
    ).correlate(Tool).exists()


def tool_visible_to_agent(tool, tenant_id, assigned_tool_ids) -> bool:
    return (
        tool.tenant_id in (None, tenant_id)
        and (tool.source in ("builtin", "admin") or str(tool.id) in assigned_tool_ids)
    )


def tool_is_required(tool_name: str) -> bool:
    """Whether a tool is a platform protocol capability that cannot be disabled."""
    return tool_name in REQUIRED_AGENT_TOOL_NAMES


def resolved_agent_tool_enabled(tool_name: str, assignment: Any | None) -> bool:
    """Resolve the effective per-Agent state, including required tools."""
    return tool_is_required(tool_name) or agent_tool_enabled(assignment)


def agent_tool_enabled(assignment: Any | None) -> bool:
    """True iff an explicit AgentTool assignment exists and is enabled.

    ``assignment`` is the ``AgentTool`` row for a given (agent, tool) pair, or
    ``None`` when no row exists. No ``is_default`` fallback — absence = disabled.
    """
    return bool(assignment is not None and assignment.enabled)


def default_tool_ids_to_seed(
    default_tools: Iterable[Any],
    existing_tool_ids: set[Any],
) -> list[Any]:
    """Tool ids that should get a fresh ``enabled=True`` AgentTool row.

    A tool qualifies when it is ``is_default=True`` and the agent does not
    already have a row for it. ``default_tools`` items need ``.id`` and
    ``.is_default``.
    """
    return [t.id for t in default_tools if t.is_default and t.id not in existing_tool_ids]


def compute_backfill_rows(
    agents: Iterable[Any],
    default_tools: Iterable[Any],
    existing_pairs: set[tuple[Any, Any]],
) -> list[tuple[Any, Any]]:
    """(agent_id, tool_id) pairs needing an ``enabled=True`` row at cutover.

    Preserves current ``is_default`` behavior: for every agent, every
    ``is_default=True`` tool without an existing (agent, tool) row gets a row.
    Tools that are ``is_default=False`` with no row were already effectively
    off and stay off — no row created (keeps "no row = off" clean).

    ``agents`` items need ``.id``; ``default_tools`` items need ``.id`` and
    ``.is_default``; ``existing_pairs`` is a set of ``(agent_id, tool_id)``.
    """
    default_tools = list(default_tools)
    existing_by_agent: dict[Any, set[Any]] = {}
    for aid, tid in existing_pairs:
        existing_by_agent.setdefault(aid, set()).add(tid)
    rows: list[tuple[Any, Any]] = []
    for a in agents:
        existing_ids = existing_by_agent.get(a.id, set())
        for tid in default_tool_ids_to_seed(default_tools, existing_ids):
            rows.append((a.id, tid))
    return rows
