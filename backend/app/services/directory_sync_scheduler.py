"""Low-cost scheduler for provider-scoped directory synchronization."""

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.identity import IdentityProvider
from app.models.org import DirectorySyncRun
from app.services.directory_sync_policy import next_sync_time
from app.services.org_sync_service import org_sync_service

DIRECTORY_SYNC_POLL_SECONDS = 60
DIRECTORY_SYNC_CLAIM_LIMIT = 20
DIRECTORY_SYNC_STALE_AFTER = timedelta(hours=6)


async def run_due_directory_syncs_once() -> int:
    """Atomically advance due schedules, then queue their common sync runs."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        queued_run_ids = []
        async with db.begin():
            stale_result = await db.execute(
                select(DirectorySyncRun)
                .where(
                    DirectorySyncRun.status == "running",
                    DirectorySyncRun.started_at < now - DIRECTORY_SYNC_STALE_AFTER,
                )
                .with_for_update(skip_locked=True)
            )
            for stale in stale_result.scalars():
                stale.status = "failed"
                stale.stage = "failed"
                stale.error_summary = "Directory sync worker stopped before completion"
                stale.finished_at = now

            result = await db.execute(
                select(IdentityProvider)
                .where(
                    IdentityProvider.is_active.is_(True),
                    IdentityProvider.sync_enabled.is_(True),
                    IdentityProvider.tenant_id.is_not(None),
                    IdentityProvider.next_sync_at.is_not(None),
                    IdentityProvider.next_sync_at <= now,
                )
                .order_by(IdentityProvider.next_sync_at)
                .limit(DIRECTORY_SYNC_CLAIM_LIMIT)
                .with_for_update(skip_locked=True)
            )
            providers = list(result.scalars())
            for provider in providers:
                if provider.sync_interval_value is None or provider.sync_interval_unit is None:
                    provider.sync_enabled = False
                    provider.next_sync_at = None
                    continue
                run, created = await org_sync_service.request_sync(
                    db,
                    provider.id,
                    trigger_type="scheduled",
                    commit=False,
                )
                if created:
                    queued_run_ids.append(run.id)
                provider.next_sync_at = next_sync_time(
                    now,
                    provider.sync_interval_value,
                    provider.sync_interval_unit,
                )
        pending = await db.execute(
            select(DirectorySyncRun.id)
            .where(DirectorySyncRun.status == "pending")
            .order_by(DirectorySyncRun.created_at)
            .limit(DIRECTORY_SYNC_CLAIM_LIMIT)
        )
        run_ids = list(dict.fromkeys([*queued_run_ids, *pending.scalars().all()]))
        for run_id in run_ids:
            asyncio.create_task(
                org_sync_service.execute_run(run_id),
                name=f"directory-sync-{run_id}",
            )
        return len(queued_run_ids)


async def directory_sync_scheduler_loop() -> None:
    """Continuously claim due providers; failures never stop later schedules."""
    while True:
        try:
            queued = await run_due_directory_syncs_once()
            if queued:
                logger.info(f"[DirectorySync] Queued {queued} scheduled provider run(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"[DirectorySync] Scheduler iteration failed: {exc}")
        await asyncio.sleep(DIRECTORY_SYNC_POLL_SECONDS)
