"""Idempotent backfill: enable the non-default web_* RPA tools for agents.

web_open / web_eval / web_cdp / web_screenshot are is_default=False, so
seed_builtin_tools() does NOT auto-assign them. This inserts an
AgentTool(enabled=True) row for each (agent, tool) pair that has none yet.

Run inside a backend container:
    python -m scripts.backfill_browser_rpa_tools             # all agents
    python -m scripts.backfill_browser_rpa_tools --dry-run   # report only
    python -m scripts.backfill_browser_rpa_tools --agents <id1> <id2>
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.tool import Tool, AgentTool

RPA_TOOL_NAMES = ["web_open", "web_eval", "web_cdp", "web_screenshot"]


def compute_rpa_rows(agent_ids, tool_ids, existing_pairs):
    return [
        (aid, tid)
        for aid in agent_ids
        for tid in tool_ids
        if (aid, tid) not in existing_pairs
    ]


async def run(dry_run: bool = False, agent_ids: list | None = None) -> int:
    async with async_session() as db:
        tools = (
            await db.execute(
                select(Tool).where(Tool.name.in_(RPA_TOOL_NAMES), Tool.source == "builtin")
            )
        ).scalars().all()
        if len(tools) != len(RPA_TOOL_NAMES):
            found = {t.name for t in tools}
            missing = [n for n in RPA_TOOL_NAMES if n not in found]
            print(f"[backfill] missing seeded tools {missing} — run seeding first.")
            return 0
        tool_ids = [t.id for t in tools]
        if agent_ids:
            target_ids = [uuid.UUID(str(a)) for a in agent_ids]
        else:
            target_ids = [r[0] for r in (await db.execute(select(Agent.id))).fetchall()]
        existing = {
            (at.agent_id, at.tool_id)
            for at in (
                await db.execute(select(AgentTool).where(AgentTool.tool_id.in_(tool_ids)))
            ).scalars().all()
        }
        rows = compute_rpa_rows(target_ids, tool_ids, existing)
        print(
            f"[backfill] {len(rows)} (agent,tool) row(s) to add "
            f"for {len(target_ids)} agent(s) × {len(tool_ids)} tool(s)."
        )
        if dry_run:
            print("[backfill] --dry-run: no writes.")
            return len(rows)
        for agent_id, tool_id in rows:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=True))
        await db.commit()
        print(f"[backfill] inserted {len(rows)} row(s).")
        return len(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--agents", nargs="*", default=None)
    a = p.parse_args()
    asyncio.run(run(dry_run=a.dry_run, agent_ids=a.agents))
