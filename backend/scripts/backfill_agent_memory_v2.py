"""One-time, idempotent backfill for the persistent-memory V2 layout.

The script never overwrites an existing memory file. For each active Agent it:

* creates ``memory/memory.md`` only when absent, copying the legacy root
  ``memory.md`` when one exists and otherwise using the empty Core template;
* creates ``memory/MEMORY_INDEX.md`` only when absent;
* optionally removes a legacy root ``memory.md`` after verifying that the
  canonical Core file contains the same bytes.

``MEMORY_INDEX.md`` remains an ordinary workspace file. Nothing at runtime
recreates or protects it after this explicit one-time command.

Run inside a backend container:
    python -m scripts.backfill_agent_memory_v2 --dry-run
    python -m scripts.backfill_agent_memory_v2 --apply
    python -m scripts.backfill_agent_memory_v2 --apply --remove-legacy-root
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.services.agent_memory import CORE_MEMORY_TEMPLATE, MEMORY_INDEX_TEMPLATE
from app.services.storage import get_storage_backend, normalize_storage_key


@dataclass
class BackfillResult:
    agent_id: uuid.UUID
    core_action: str = "unchanged"
    index_action: str = "unchanged"
    legacy_action: str = "unchanged"


async def backfill_agent_memory(
    agent_id: uuid.UUID,
    *,
    apply: bool,
    remove_legacy_root: bool = False,
) -> BackfillResult:
    """Plan or apply the memory-layout backfill for one Agent."""
    storage = get_storage_backend()
    prefix = normalize_storage_key(str(agent_id))
    core_key = normalize_storage_key(f"{prefix}/memory/memory.md")
    index_key = normalize_storage_key(f"{prefix}/memory/MEMORY_INDEX.md")
    legacy_key = normalize_storage_key(f"{prefix}/memory.md")
    result = BackfillResult(agent_id=agent_id)

    core_exists = await storage.is_file(core_key)
    legacy_exists = await storage.is_file(legacy_key)
    if not core_exists:
        if legacy_exists:
            core_bytes = await storage.read_bytes(legacy_key)
            result.core_action = "copy_legacy_root"
        else:
            core_bytes = CORE_MEMORY_TEMPLATE.encode("utf-8")
            result.core_action = "create_template"
        if apply:
            await storage.write_bytes(core_key, core_bytes, content_type="text/markdown; charset=utf-8")
            core_exists = True

    if not await storage.is_file(index_key):
        result.index_action = "create_template"
        if apply:
            await storage.write_text(index_key, MEMORY_INDEX_TEMPLATE, encoding="utf-8")

    if remove_legacy_root and legacy_exists:
        result.legacy_action = "retain_not_verified"
        if apply and core_exists:
            canonical_bytes = await storage.read_bytes(core_key)
            legacy_bytes = await storage.read_bytes(legacy_key)
            if canonical_bytes == legacy_bytes:
                await storage.delete(legacy_key)
                result.legacy_action = "removed_verified_duplicate"

    return result


async def run(*, apply: bool, remove_legacy_root: bool = False) -> list[BackfillResult]:
    async with async_session() as db:
        agent_ids = list(
            (
                await db.execute(
                    select(Agent.id).where(Agent.is_deleted.is_(False)).order_by(Agent.id)
                )
            ).scalars()
        )

    results = [
        await backfill_agent_memory(
            agent_id,
            apply=apply,
            remove_legacy_root=remove_legacy_root,
        )
        for agent_id in agent_ids
    ]
    changed = [
        result
        for result in results
        if (result.core_action, result.index_action, result.legacy_action)
        != ("unchanged", "unchanged", "unchanged")
    ]
    mode = "apply" if apply else "dry-run"
    print(f"[memory-v2] mode={mode} agents={len(results)} changed={len(changed)}")
    for result in changed:
        print(
            f"[memory-v2] agent={result.agent_id} core={result.core_action} "
            f"index={result.index_action} legacy={result.legacy_action}"
        )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--remove-legacy-root", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply, remove_legacy_root=args.remove_legacy_root))
