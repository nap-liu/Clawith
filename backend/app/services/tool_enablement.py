"""Pure resolution + seed planning for per-agent tool enablement.

Single source of truth for "does this agent have this tool enabled?".

Rule (EXPLICIT-ONLY): a tool is enabled for an agent iff there is an
``AgentTool`` row with ``enabled=True``. Absence of a row means NOT enabled.
``Tool.is_default`` is NOT consulted at resolution time — it is only a
seed-time template (see the planners below), read once when an agent is
created or during the one-time backfill.

All functions here are pure (no DB I/O) so they can be unit-tested with
SimpleNamespace stubs, matching this repo's test convention.
"""
from __future__ import annotations

from typing import Any, Iterable


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
