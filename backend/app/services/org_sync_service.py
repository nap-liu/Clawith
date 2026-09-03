"""Provider-neutral directory sync request, progress, and execution service."""

import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.identity import IdentityProvider
from app.models.org import DirectorySyncRun

ACTIVE_RUN_STATUSES = ("pending", "running")


def serialize_sync_run(run: DirectorySyncRun) -> dict[str, Any]:
    """Return the safe polling contract used by API and UI."""
    return {
        "id": str(run.id),
        "tenant_id": str(run.tenant_id),
        "provider_id": str(run.provider_id),
        "trigger_type": run.trigger_type,
        "status": run.status,
        "stage": run.stage,
        "processed_items": run.processed_items,
        "total_items": run.total_items,
        "progress_percent": run.progress_percent,
        "stats": run.stats or {},
        "error_summary": run.error_summary,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


class OrgSyncService:
    """Run every directory adapter through one durable lifecycle."""

    async def request_sync(
        self,
        db: AsyncSession,
        provider_id: uuid.UUID,
        *,
        trigger_type: str,
        created_by_user_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> tuple[DirectorySyncRun, bool]:
        provider = await db.get(IdentityProvider, provider_id)
        if provider is None:
            raise LookupError("Provider not found")
        if provider.tenant_id is None:
            raise ValueError("Provider must be bound to a tenant")
        if not provider.is_active:
            raise ValueError("Provider is inactive")
        if trigger_type not in {"manual", "scheduled"}:
            raise ValueError("Unsupported sync trigger")

        active = await self._active_run(db, provider_id)
        if active is not None:
            return active, False

        run = DirectorySyncRun(
            tenant_id=provider.tenant_id,
            provider_id=provider.id,
            trigger_type=trigger_type,
            status="pending",
            stage="queued",
            processed_items=0,
            progress_percent=0,
            created_by_user_id=created_by_user_id,
        )
        try:
            async with db.begin_nested():
                db.add(run)
                await db.flush()
        except IntegrityError:
            active = await self._active_run(db, provider_id)
            if active is None:
                raise
            return active, False
        if commit:
            await db.commit()
            await db.refresh(run)
            # Starlette background tasks run before request-scoped dependencies
            # are torn down.  End the refresh transaction here so a long
            # provider fetch cannot leave the API session idle in transaction.
            await db.commit()
        return run, True

    async def execute_run(self, run_id: uuid.UUID) -> None:
        """Execute a queued run in an independent session/background task."""
        try:
            async with async_session() as db:
                claimed = await db.execute(
                    select(DirectorySyncRun)
                    .where(
                        DirectorySyncRun.id == run_id,
                        DirectorySyncRun.status == "pending",
                    )
                    .with_for_update(skip_locked=True)
                )
                run = claimed.scalar_one_or_none()
                if run is None:
                    return
                provider = await db.get(IdentityProvider, run.provider_id)
                if provider is None or not provider.is_active or provider.tenant_id != run.tenant_id:
                    await self._finish_failed(db, run, "Provider is unavailable")
                    return

                now = datetime.now(timezone.utc)
                run.status = "running"
                run.stage = "fetching"
                run.progress_percent = 5
                run.started_at = now
                provider.last_sync_attempt_at = now
                await db.commit()

                from app.services.org_sync_adapter import get_org_sync_adapter

                adapter = await get_org_sync_adapter(
                    db,
                    provider.provider_type,
                    tenant_id=provider.tenant_id,
                    provider_id=provider.id,
                    provider=provider,
                )
                if adapter is None:
                    await self._finish_failed(
                        db,
                        run,
                        f"Provider type '{provider.provider_type}' has no directory adapter",
                    )
                    return

                adapter.provider = provider
                adapter.provider_id = provider.id
                adapter.config = provider.config or {}
                adapter.tenant_id = provider.tenant_id

                async def report(stage: str, processed: int, total: int | None, percent: int) -> None:
                    await self._persist_progress(run_id, stage, processed, total, percent)

                adapter.progress_callback = report
                result = await adapter.sync_org_structure(db)
                await db.commit()
                await db.refresh(run)
                await db.refresh(provider)

                errors = result.get("errors") or []
                run.stats = {
                    key: value
                    for key, value in result.items()
                    if key != "errors" and isinstance(value, (str, int, float, bool))
                }
                completed_items = (
                    int(result.get("members") or 0)
                    + int(result.get("departments") or 0)
                )
                run.processed_items = max(run.processed_items, completed_items)
                run.total_items = max(run.total_items or 0, run.processed_items)
                run.progress_percent = 100
                run.finished_at = datetime.now(timezone.utc)
                identity_conflicts = int(result.get("identity_conflicts") or 0)
                if errors:
                    has_results = bool(result.get("members") or result.get("departments"))
                    run.status = "partial_failed" if has_results else "failed"
                    run.stage = "completed_with_errors"
                    run.error_summary = f"Directory sync completed with {len(errors)} error(s)"
                    if identity_conflicts:
                        run.error_summary += (
                            f"; {identity_conflicts} identity match(es) require review"
                        )
                elif identity_conflicts:
                    run.status = "needs_review"
                    run.stage = "completed_needs_review"
                    run.error_summary = (
                        f"{identity_conflicts} identity match(es) require review"
                    )
                else:
                    run.status = "succeeded"
                    run.stage = "completed"
                    provider.last_sync_success_at = run.finished_at
                await db.commit()
        except Exception as exc:
            logger.exception(f"[DirectorySync] Run {run_id} failed: {exc}")
            async with async_session() as failure_db:
                run = await failure_db.get(DirectorySyncRun, run_id)
                if run is not None and run.status in ACTIVE_RUN_STATUSES:
                    await self._finish_failed(failure_db, run, "Directory sync failed")

    async def _persist_progress(
        self,
        run_id: uuid.UUID,
        stage: str,
        processed: int,
        total: int | None,
        percent: int,
    ) -> None:
        async with async_session() as db:
            run = await db.get(DirectorySyncRun, run_id)
            if run is None or run.status != "running":
                return
            run.stage = stage
            run.processed_items = max(run.processed_items, processed)
            run.total_items = total
            run.progress_percent = max(run.progress_percent or 0, min(percent, 99))
            await db.commit()

    async def _finish_failed(
        self,
        db: AsyncSession,
        run: DirectorySyncRun,
        summary: str,
    ) -> None:
        run.status = "failed"
        run.stage = "failed"
        run.error_summary = summary
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()

    async def _active_run(
        self,
        db: AsyncSession,
        provider_id: uuid.UUID,
    ) -> DirectorySyncRun | None:
        result = await db.execute(
            select(DirectorySyncRun)
            .where(
                DirectorySyncRun.provider_id == provider_id,
                DirectorySyncRun.status.in_(ACTIVE_RUN_STATUSES),
            )
            .order_by(DirectorySyncRun.created_at.desc())
        )
        return result.scalars().first()


org_sync_service = OrgSyncService()
