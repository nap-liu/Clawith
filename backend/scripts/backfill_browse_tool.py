"""Idempotent backfill: enable the non-default `browse` tool for agents.

`browse` is is_default=False, so seed_builtin_tools() does NOT auto-assign it.
This inserts an AgentTool(enabled=True) row for the browse tool for every
agent (or a given allowlist) that has no row yet.

Run inside a backend container:
    python -m scripts.backfill_browse_tool             # all agents
    python -m scripts.backfill_browse_tool --dry-run   # report only
    python -m scripts.backfill_browse_tool --agents <id1> <id2>
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.tool import Tool, AgentTool


def compute_browse_rows(agent_ids, browse_tool_id, existing_pairs):
    return [
        (aid, browse_tool_id)
        for aid in agent_ids
        if (aid, browse_tool_id) not in existing_pairs
    ]


async def run(dry_run: bool = False, agent_ids: list | None = None) -> int:
    async with async_session() as db:
        tool = (
            await db.execute(select(Tool).where(Tool.name == "browse", Tool.source == "builtin"))
        ).scalar_one_or_none()
        if tool is None:
            print("[backfill] 'browse' tool not seeded yet — run seeding first.")
            return 0
        if agent_ids:
            target_ids = [uuid.UUID(str(a)) for a in agent_ids]
        else:
            target_ids = [r[0] for r in (await db.execute(select(Agent.id))).fetchall()]
        existing = {
            (at.agent_id, at.tool_id)
            for at in (
                await db.execute(select(AgentTool).where(AgentTool.tool_id == tool.id))
            ).scalars().all()
        }
        rows = compute_browse_rows(target_ids, tool.id, existing)
        print(f"[backfill] {len(rows)} agent(s) will get 'browse' (of {len(target_ids)} targeted).")
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
    print(f"[backfill] inserted {asyncio.run(run(dry_run=a.dry_run, agent_ids=a.agents))} row(s).")
