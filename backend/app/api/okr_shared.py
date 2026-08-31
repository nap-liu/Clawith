"""Shared OKR API router and helpers."""

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.okr import OKRObjective, OKRSettings

router = APIRouter(prefix="/api/okr", tags=["okr"])


def _is_okr_admin(user) -> bool:
    return getattr(user, "role", None) in ("org_admin", "platform_admin")


def _dashboard_write_forbidden() -> HTTPException:
    return HTTPException(
        403,
        "Only org admins can modify OKRs in the dashboard. Members should use OKR Agent to manage their own OKRs.",
    )


async def _sync_okr_agent_relationships(db, tenant_id: uuid.UUID, okr_agent_id: uuid.UUID) -> None:
    """Auto-connect the OKR Agent to all active org members and company-visible agents.

    Idempotent — clears existing relationships first for a clean re-sync.
    Rules:
      - Human relationships : every active User in this tenant
      - Agent relationships : every non-system, non-stopped agent in this tenant
                              (excludes the OKR Agent itself)
    """
    from app.models.agent import Agent
    from app.models.org import (
        AgentAgentRelationship,
        AgentRelationship,
        RelationshipSuppression,
    )
    from sqlalchemy import delete as sa_delete

    # 1. Clear existing relationships (clean-slate re-sync)
    await db.execute(sa_delete(AgentRelationship).where(AgentRelationship.agent_id == okr_agent_id))
    await db.execute(sa_delete(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == okr_agent_id))

    from app.models.user import User

    # 2. Link every active canonical tenant user once.
    member_result = await db.execute(
        select(User.id).where(
            User.tenant_id == tenant_id,
            User.is_active == True,  # noqa: E712
        )
    )
    user_ids = [user_id for (user_id,) in member_result.fetchall()]
    if user_ids:
        # This administrator endpoint is an explicit re-add operation. Clear
        # durable suppressions for exactly the canonical users being restored.
        await db.execute(
            sa_delete(RelationshipSuppression).where(
                RelationshipSuppression.agent_id == okr_agent_id,
                RelationshipSuppression.target_type == "user",
                RelationshipSuppression.target_id.in_(user_ids),
            )
        )
    for user_id in user_ids:
        db.add(AgentRelationship(
            agent_id=okr_agent_id,
            user_id=user_id,
            relation="team_member",
            description="OKR tracking — auto-linked via Sync Relationships",
        ))

    # 3. Link all company-visible non-system agents as collaborators.
    agent_result = await db.execute(
        select(Agent.id).where(
            Agent.tenant_id == tenant_id,
            Agent.id != okr_agent_id,
            Agent.is_system == False,  # noqa: E712
            Agent.status.notin_(["stopped", "error"]),
            Agent.access_mode == "company",
        )
    )
    agent_ids = [agent_id for (agent_id,) in agent_result.fetchall()]
    if agent_ids:
        await db.execute(
            sa_delete(RelationshipSuppression).where(
                RelationshipSuppression.agent_id == okr_agent_id,
                RelationshipSuppression.target_type == "agent",
                RelationshipSuppression.target_id.in_(agent_ids),
            )
        )
    for agent_id in agent_ids:
        db.add(AgentAgentRelationship(
            agent_id=okr_agent_id,
            target_agent_id=agent_id,
            relation="collaborator",
        ))

    # 4. Regenerate the OKR Agent's relationships file (best-effort)
    try:
        from app.api.relationships import _regenerate_relationships_file
        await _regenerate_relationships_file(db, okr_agent_id)
    except Exception:
        pass  # non-critical; agent picks it up on next heartbeat


async def _get_or_create_settings(db, tenant_id: uuid.UUID) -> OKRSettings:
    """Return the OKRSettings row for this tenant, creating it if missing."""
    result = await db.execute(
        select(OKRSettings).where(OKRSettings.tenant_id == tenant_id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = OKRSettings(tenant_id=tenant_id)
        db.add(settings)
        await db.flush()
    return settings


async def _sync_okr_report_triggers(db, settings: OKRSettings) -> None:
    """Keep OKR Agent system triggers aligned with tenant report settings."""
    if not settings.okr_agent_id:
        return

    from app.models.trigger import AgentTrigger
    from app.models.agent import Agent
    from app.services.focus_service import ensure_focus_item

    okr_agent = await db.get(Agent, settings.okr_agent_id)
    if okr_agent is None:
        logger.warning("[OKR] Cannot sync report triggers: OKR Agent is missing")
        return
    creator_id = okr_agent.creator_id

    system_focus_ref = await ensure_focus_item(
        settings.okr_agent_id,
        focus_ref="system:okr_reports",
        description="OKR 自动汇总、日报收集与周期报告",
        system=True,
        db=db,
    )

    daily_hour, daily_minute = 18, 0
    try:
        daily_hour_str, daily_minute_str = settings.daily_report_time.split(":", 1)
        daily_hour = max(0, min(23, int(daily_hour_str)))
        daily_minute = max(0, min(59, int(daily_minute_str)))
    except Exception:
        logger.warning(f"[OKR] Invalid daily_report_time {settings.daily_report_time}; using 18:00")

    trigger_result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == settings.okr_agent_id,
            AgentTrigger.name.in_(
                [
                    "daily_okr_collection",
                    "daily_okr_report",
                    "weekly_okr_report",
                    "biweekly_okr_checkin",
                    "monthly_okr_report",
                ]
            ),
        )
    )
    triggers = {trigger.name: trigger for trigger in trigger_result.scalars().all()}

    def _ensure_trigger(name: str, *, config: dict, reason: str, is_enabled: bool) -> AgentTrigger:
        trigger = triggers.get(name)
        if trigger is None:
            trigger = AgentTrigger(
                agent_id=settings.okr_agent_id,
                created_by_user_id=creator_id,
                execution_user_id=creator_id,
                name=name,
                type="cron",
                config=config,
                reason=reason,
                cooldown_seconds=3600,
                is_system=True,
                focus_ref=system_focus_ref,
                is_enabled=is_enabled,
            )
            db.add(trigger)
            triggers[name] = trigger
            return trigger
        trigger.created_by_user_id = trigger.created_by_user_id or creator_id
        trigger.execution_user_id = trigger.execution_user_id or creator_id
        trigger.config = config
        trigger.reason = reason
        trigger.is_enabled = is_enabled
        trigger.focus_ref = trigger.focus_ref or system_focus_ref
        return trigger

    _ensure_trigger(
        "daily_okr_collection",
        config={"expr": f"{daily_minute} {daily_hour} * * *"},
        is_enabled=bool(settings.enabled and settings.daily_report_enabled),
        reason=(
            "System trigger: daily OKR collection. When daily reporting is enabled, "
            "the OKR Agent should collect today's final daily update only from members "
            "and agents already in its relationship list."
        ),
    )

    _ensure_trigger(
        "daily_okr_report",
        config={"expr": "0 9 * * *"},
        is_enabled=bool(settings.enabled),
        reason=(
            "System trigger: generate the company daily report at 09:00 for the previous day."
        ),
    )

    _ensure_trigger(
        "weekly_okr_report",
        config={"expr": "0 9 * * 1"},
        is_enabled=bool(settings.enabled),
        reason=(
            "System trigger: generate the company weekly report at 09:00 every Monday "
            "for the previous week."
        ),
    )

    biweekly = triggers.get("biweekly_okr_checkin")
    if biweekly:
        biweekly.is_enabled = bool(settings.enabled)
        biweekly.reason = (
            "System trigger: fires on the 1st and 15th of every month at 10:00 "
            "to perform the mandatory bi-weekly OKR check-in."
        )

    _ensure_trigger(
        "monthly_okr_report",
        config={"expr": "0 9 1 * *"},
        is_enabled=bool(settings.enabled),
        reason=(
            "System trigger: generate the company monthly report at 09:00 on the 1st "
            "for the previous month."
        ),
    )


def _compute_current_period(
    frequency: str, length_days: int | None
) -> tuple[date, date]:
    """Compute the start and end dates of the current OKR period.

    This is a simple deterministic calculation from today's date so the
    frontend and API always agree on what "the current period" is.
    """
    today = date.today()
    if frequency == "monthly":
        start = today.replace(day=1)
        # Last day of this month
        if today.month == 12:
            end = today.replace(month=12, day=31)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        # Align to multiples of length_days from the Unix epoch
        epoch = date(1970, 1, 1)
        days_since_epoch = (today - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        # Default: quarterly (Q1/Q2/Q3/Q4)
        quarter = (today.month - 1) // 3 + 1
        start = date(today.year, (quarter - 1) * 3 + 1, 1)
        if quarter == 4:
            end = date(today.year, 12, 31)
        else:
            end = date(today.year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


def _compute_period_for_date(
    frequency: str, length_days: int | None, target: date
) -> tuple[date, date]:
    """Compute the OKR period containing a specific date."""
    if frequency == "monthly":
        start = target.replace(day=1)
        if target.month == 12:
            end = target.replace(month=12, day=31)
        else:
            end = target.replace(month=target.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        epoch = date(1970, 1, 1)
        days_since_epoch = (target - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        quarter = (target.month - 1) // 3 + 1
        start = date(target.year, (quarter - 1) * 3 + 1, 1)
        if quarter == 4:
            end = date(target.year, 12, 31)
        else:
            end = date(target.year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


def _advance_period(
    start: date, frequency: str, length_days: int | None, steps: int = 1
) -> tuple[date, date]:
    """Move a period start forward by a fixed number of OKR periods."""
    if frequency == "monthly":
        month_index = start.year * 12 + (start.month - 1) + steps
        year = month_index // 12
        month = month_index % 12 + 1
        return _compute_period_for_date(frequency, length_days, date(year, month, 1))
    if frequency == "custom" and length_days:
        next_start = start + timedelta(days=length_days * steps)
        return next_start, next_start + timedelta(days=length_days - 1)
    quarter = (start.month - 1) // 3
    quarter_index = start.year * 4 + quarter + steps
    year = quarter_index // 4
    next_quarter = quarter_index % 4 + 1
    return _compute_period_for_date(frequency, length_days, date(year, (next_quarter - 1) * 3 + 1, 1))
