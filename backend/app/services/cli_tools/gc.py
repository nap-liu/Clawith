"""Daily GC for orphaned CLI-tool binaries.

Rule (see spec §5.2):
    A `.bin` file is deleted iff
      (a) no Tool row in the DB references it via config.binary_sha256, AND
      (b) mtime is older than `age_threshold_days`.

The AND is strict: a still-referenced binary is never deleted regardless of age.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool import Tool
from app.services.cli_tools.storage import BinaryStorage

logger = logging.getLogger(__name__)


async def gc_cli_binaries(
    *,
    db: AsyncSession,
    storage: BinaryStorage,
    age_threshold_days: int = 30,
) -> int:
    """Delete orphaned + aged `.bin` files. Returns number of files deleted."""
    result = await db.execute(select(Tool).where(Tool.type == "cli"))
    tools = result.scalars().all()
    referenced: set[str] = set()
    for tool in tools:
        sha = (tool.config or {}).get("binary_sha256")
        if isinstance(sha, str) and len(sha) == 64:
            referenced.add(sha)

    cutoff = time.time() - age_threshold_days * 86400
    to_delete: list[Path] = []
    for orphan_path in storage.iter_orphans(referenced_shas=referenced):
        try:
            if orphan_path.stat().st_mtime <= cutoff:
                to_delete.append(orphan_path)
        except FileNotFoundError:
            continue

    deleted = storage.delete_orphans(to_delete)
    if deleted:
        logger.info("cli_tools.gc deleted %d orphaned binaries", deleted)
    return deleted
