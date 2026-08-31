"""OKR settings, relationship sync, and period routes."""

from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from loguru import logger
from sqlalchemy import select

from app.api.auth import get_current_user
from app.api.okr_models import OKRSettingsOut, OKRSettingsUpdate, PeriodOut
from app.api.okr_shared import (
    _advance_period,
    _compute_current_period,
    _compute_period_for_date,
    _get_or_create_settings,
    _sync_okr_agent_relationships,
    _sync_okr_report_triggers,
    async_session,
    router,
)
from app.models.okr import OKRObjective


@router.get("/settings", response_model=OKRSettingsOut)
async def get_okr_settings(user=Depends(get_current_user)):
    """Return OKR configuration for the current tenant."""
    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)

        # Also resolve the OKR Agent ID so the UI can show the chat button
        okr_agent_id_str = str(settings.okr_agent_id) if settings.okr_agent_id else None

        await db.commit()
        return OKRSettingsOut(
            enabled=settings.enabled,
            first_enabled_at=settings.first_enabled_at.isoformat() if settings.first_enabled_at else None,
            daily_report_enabled=settings.daily_report_enabled,
            daily_report_time=settings.daily_report_time,
            daily_report_skip_non_workdays=settings.daily_report_skip_non_workdays,
            weekly_report_enabled=False,
            weekly_report_day=0,
            period_frequency=settings.period_frequency,
            period_length_days=settings.period_length_days,
            period_frequency_locked=settings.first_enabled_at is not None,
            okr_agent_id=okr_agent_id_str,
        )


@router.put("/settings", response_model=OKRSettingsOut)
async def update_okr_settings(body: OKRSettingsUpdate, user=Depends(get_current_user)):
    """Update OKR configuration. Org admins only."""
    # Allow org admins and platform admins to modify OKR settings.
    # user.role is the canonical authority; is_admin is not a real field.
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can modify OKR settings")

    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)
        period_is_locked = settings.first_enabled_at is not None

        if period_is_locked:
            if body.period_frequency is not None and body.period_frequency != settings.period_frequency:
                raise HTTPException(
                    400,
                    "OKR period frequency is locked after OKR is first enabled.",
                )
            if body.period_length_days is not None and body.period_length_days != settings.period_length_days:
                raise HTTPException(
                    400,
                    "OKR period length is locked after OKR is first enabled.",
                )

        if body.enabled is not None:
            settings.enabled = body.enabled
        if body.daily_report_enabled is not None:
            settings.daily_report_enabled = body.daily_report_enabled
        if body.daily_report_time is not None:
            settings.daily_report_time = body.daily_report_time
        if body.daily_report_skip_non_workdays is not None:
            settings.daily_report_skip_non_workdays = body.daily_report_skip_non_workdays
        if body.period_frequency is not None:
            settings.period_frequency = body.period_frequency
        if body.period_length_days is not None:
            settings.period_length_days = body.period_length_days

        # Member reporting is daily-only in the redesigned OKR workflow.
        settings.weekly_report_enabled = False
        settings.weekly_report_day = 0

        if body.enabled is True and settings.first_enabled_at is None:
            settings.first_enabled_at = datetime.now(timezone.utc)

        await _sync_okr_report_triggers(db, settings)
        await db.commit()

        # ── Auto-create OKR Agent when first enabled ──────────────────────────
        # If OKR was just turned on and no agent exists yet for this tenant,
        # seed one so the user doesn't see "OKR Agent not found".
        okr_agent_id_str: str | None = str(settings.okr_agent_id) if settings.okr_agent_id else None

        if body.enabled and not settings.okr_agent_id:
            from app.services.agent_seeder import seed_okr_agent_for_tenant
            logger.info(f"[OKR] OKR enabled for tenant {user.tenant_id} — auto-seeding OKR Agent")
            await seed_okr_agent_for_tenant(user.tenant_id, user.id)

            # Re-read settings to pick up the newly written okr_agent_id
            async with async_session() as db2:
                refreshed = await _get_or_create_settings(db2, user.tenant_id)
                await _sync_okr_report_triggers(db2, refreshed)
                await db2.commit()
                okr_agent_id_str = str(refreshed.okr_agent_id) if refreshed.okr_agent_id else None

        return OKRSettingsOut(
            enabled=settings.enabled,
            first_enabled_at=settings.first_enabled_at.isoformat() if settings.first_enabled_at else None,
            daily_report_enabled=settings.daily_report_enabled,
            daily_report_time=settings.daily_report_time,
            daily_report_skip_non_workdays=settings.daily_report_skip_non_workdays,
            weekly_report_enabled=False,
            weekly_report_day=0,
            period_frequency=settings.period_frequency,
            period_length_days=settings.period_length_days,
            period_frequency_locked=settings.first_enabled_at is not None,
            okr_agent_id=okr_agent_id_str,
        )


@router.post("/sync-relationships")
async def sync_okr_relationships(user=Depends(get_current_user)):
    """Manually re-sync the OKR Agent's relationship network.

    Connects the OKR Agent to all active canonical tenant users
    and all company-visible agents in this tenant. Idempotent — safe to call
    multiple times; existing relationships are replaced.

    Org admins and platform admins only.
    """
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can sync OKR relationships")

    async with async_session() as db:
        # Locate the OKR Agent from settings
        settings = await _get_or_create_settings(db, user.tenant_id)
        if not settings.okr_agent_id:
            raise HTTPException(
                404,
                "当前租户未找到 OKR 数字员工，请先在企业设置中启用 OKR。",
            )
        okr_agent_id = settings.okr_agent_id

        await _sync_okr_agent_relationships(db, user.tenant_id, okr_agent_id)
        await db.commit()

    return {"status": "ok", "okr_agent_id": str(okr_agent_id)}


@router.get("/periods", response_model=list[PeriodOut])
async def list_periods(user=Depends(get_current_user)):
    """Return OKR periods from first enablement through the next period.

    Periods are computed from the tenant's locked OKR cadence. Once OKR has
    been enabled for a tenant, the first enabled period remains the start of
    the selectable history even if OKR is later disabled and re-enabled.
    """
    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)
        first_enabled_at = settings.first_enabled_at
        if first_enabled_at is None and settings.enabled:
            earliest_result = await db.execute(
                select(OKRObjective.period_start)
                .where(OKRObjective.tenant_id == user.tenant_id)
                .order_by(OKRObjective.period_start.asc())
                .limit(1)
            )
            earliest_period_start = earliest_result.scalar_one_or_none()
            if earliest_period_start:
                first_enabled_at = datetime.combine(
                    earliest_period_start,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
            else:
                first_enabled_at = datetime.now(timezone.utc)
            settings.first_enabled_at = first_enabled_at
        await db.commit()

    freq = settings.period_frequency
    length = settings.period_length_days

    def _period_label(start: date, freq: str) -> str:
        if freq == "monthly":
            return start.strftime("%b %Y")
        elif freq == "quarterly":
            q = (start.month - 1) // 3 + 1
            return f"Q{q} {start.year}"
        else:
            end = start + timedelta(days=(length or 90) - 1)
            return f"{start.isoformat()} – {end.isoformat()}"

    cur_start, _ = _compute_current_period(freq, length)
    first_anchor = (first_enabled_at.date() if first_enabled_at else date.today())
    start, _ = _compute_period_for_date(freq, length, first_anchor)
    final_start, _ = _advance_period(cur_start, freq, length, 1)

    all_periods: list[tuple[date, date]] = []
    cursor_start = start
    guard = 0
    while cursor_start <= final_start and guard < 600:
        period_start, period_end = _compute_period_for_date(freq, length, cursor_start)
        all_periods.append((period_start, period_end))
        cursor_start, _ = _advance_period(period_start, freq, length, 1)
        guard += 1

    return [
        PeriodOut(
            start=s.isoformat(),
            end=e.isoformat(),
            label=_period_label(s, freq),
            is_current=(s == cur_start),
        )
        for s, e in all_periods
    ]
