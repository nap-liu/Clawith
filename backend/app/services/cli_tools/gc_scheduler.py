"""Daily loop that invokes `gc_cli_binaries`.

The project has no APScheduler; recurring work is run as an asyncio
background task registered in `main.py`'s lifespan, the same way
`trigger_daemon` etc. are. We follow that pattern here.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.database import async_session
from app.services.cli_tools.gc import gc_cli_binaries
from app.services.cli_tools.storage import BinaryStorage

logger = logging.getLogger(__name__)

# One day.
_SLEEP_SECONDS = 24 * 3600
# On first startup, wait a short grace period before the first run so we
# don't pile GC on top of migrations / tool seeding.
_INITIAL_DELAY_SECONDS = 5 * 60


async def cli_tools_gc_loop() -> None:
    """Background task: sleep ~24h, run GC, repeat. Never raises."""
    try:
        await asyncio.sleep(_INITIAL_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    while True:
        try:
            storage = BinaryStorage(root=Path("/data/cli_binaries"))
            async with async_session() as db:
                deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
                logger.info("[cli_tools.gc] pass complete, deleted=%d", deleted)
        except asyncio.CancelledError:
            return
        except Exception:
            # Never let the loop die — log and retry tomorrow.
            logger.exception("[cli_tools.gc] pass failed")

        try:
            await asyncio.sleep(_SLEEP_SECONDS)
        except asyncio.CancelledError:
            return
