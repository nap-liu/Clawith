"""One-time, idempotent backfill: materialize current is_default behavior into
explicit AgentTool rows BEFORE the resolution flip (Task 2) goes live.

For every agent, for every globally-enabled builtin tool with is_default=True
that has no AgentTool row yet, insert one with enabled=True. This is a no-op at
runtime under the OLD code (the row's enabled=True equals what is_default=True
already produced), so it is safe to run on prod ahead of the code deploy.

Guarded by a SystemSetting flag so it runs once: future is_default tools will
NOT be auto-propagated to existing agents (that is the whole point — opt-in).

Run inside a backend container:
    python -m scripts.backfill_tool_assignments            # run once
    python -m scripts.backfill_tool_assignments --dry-run  # report only
    python -m scripts.backfill_tool_assignments --force    # ignore the run-once flag (already-existing AgentTool rows are still skipped)
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.tool import Tool, AgentTool
from app.models.system_settings import SystemSetting
from app.services.tool_enablement import compute_backfill_rows

FLAG_KEY = "tool_enablement_backfill_v1_done"


async def _flag_set(db) -> bool:
    r = await db.execute(select(SystemSetting).where(SystemSetting.key == FLAG_KEY))
    return r.scalar_one_or_none() is not None


async def run(dry_run: bool = False, force: bool = False) -> int:
    async with async_session() as db:
        if not force and await _flag_set(db):
            print(f"[backfill] flag '{FLAG_KEY}' already set — skipping. Use --force to override.")
            return 0

        agents = (await db.execute(select(Agent))).scalars().all()
        default_tools = (
            await db.execute(
                select(Tool).where(
                    Tool.enabled == True,            # noqa: E712
                    Tool.source == "builtin",
                    Tool.is_default == True,          # noqa: E712
                )
            )
        ).scalars().all()
        existing_pairs = {
            (at.agent_id, at.tool_id)
            for at in (await db.execute(select(AgentTool))).scalars().all()
        }

        rows = compute_backfill_rows(agents, default_tools, existing_pairs)
        print(
            f"[backfill] agents={len(agents)} default_builtin_tools={len(default_tools)} "
            f"existing_rows={len(existing_pairs)} rows_to_insert={len(rows)}"
        )
        if dry_run:
            print("[backfill] --dry-run: no writes.")
            return 0

        for agent_id, tool_id in rows:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=True))
        if not await _flag_set(db):
            db.add(SystemSetting(key=FLAG_KEY, value={"inserted": len(rows)}))
        await db.commit()
        print(f"[backfill] inserted {len(rows)} rows; flag '{FLAG_KEY}' set.")
        return 0


if __name__ == "__main__":
    asyncio.run(run(dry_run="--dry-run" in sys.argv, force="--force" in sys.argv))
