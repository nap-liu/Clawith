"""Timezone-aware cron occurrence calculation.

The scheduled occurrence is the single source of truth for due evaluation,
durable ordering, model context, and execution idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.models.trigger import AgentTrigger
from app.services.timezone_utils import get_agent_timezone


@dataclass(frozen=True)
class CronOccurrence:
    """One canonical cron occurrence."""

    scheduled_for: datetime
    local_scheduled_for: datetime
    timezone_name: str


def _resolve_zoneinfo(timezone_name: str | None) -> tuple[str, ZoneInfo]:
    candidate = str(timezone_name or "UTC").strip() or "UTC"
    try:
        return candidate, ZoneInfo(candidate)
    except (KeyError, ValueError):
        return "UTC", ZoneInfo("UTC")


def compute_next_cron_occurrence(
    *,
    expr: str,
    base: datetime,
    timezone_name: str,
) -> CronOccurrence:
    """Return the next occurrence after ``base`` in the requested timezone."""
    resolved_name, tz = _resolve_zoneinfo(timezone_name)
    local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
    local_scheduled_for = croniter(expr, local_base).get_next(datetime)
    if local_scheduled_for.tzinfo is None:
        local_scheduled_for = local_scheduled_for.replace(tzinfo=tz)
    scheduled_for = (
        local_scheduled_for.astimezone(timezone.utc)
        .replace(microsecond=0)
    )
    return CronOccurrence(
        scheduled_for=scheduled_for,
        local_scheduled_for=scheduled_for.astimezone(tz),
        timezone_name=resolved_name,
    )


async def compute_next_trigger_cron_occurrence(
    trigger: AgentTrigger,
) -> CronOccurrence:
    """Resolve a trigger's effective timezone and next canonical occurrence."""
    cfg = trigger.config if isinstance(trigger.config, dict) else {}
    timezone_name = cfg.get("timezone")
    if not timezone_name:
        timezone_name = await get_agent_timezone(trigger.agent_id)
    return compute_next_cron_occurrence(
        expr=str(cfg.get("expr") or "* * * * *"),
        base=trigger.last_fired_at or trigger.created_at,
        timezone_name=str(timezone_name or "UTC"),
    )


def format_cron_timing_context(config: dict, executed_at: datetime) -> str:
    """Render neutral platform timing facts for a scheduled invocation."""
    raw_scheduled_for = config.get("_scheduled_for")
    if not raw_scheduled_for:
        return ""
    try:
        scheduled_for = datetime.fromisoformat(str(raw_scheduled_for))
        if scheduled_for.tzinfo is None:
            return ""
        scheduled_for = scheduled_for.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return ""

    timezone_name, schedule_timezone = _resolve_zoneinfo(
        str(config.get("_scheduled_timezone") or "UTC")
    )
    if executed_at.tzinfo is None:
        raise ValueError("executed_at must be timezone-aware")
    executed_at = executed_at.astimezone(timezone.utc)
    delay_seconds = max(0, int((executed_at - scheduled_for).total_seconds()))
    return (
        "\nSchedule timing (platform facts):"
        f"\n- Scheduled occurrence (UTC): {scheduled_for.isoformat()}"
        f"\n- Scheduled occurrence ({timezone_name}): "
        f"{scheduled_for.astimezone(schedule_timezone).isoformat()}"
        f"\n- Current execution time (UTC): {executed_at.isoformat()}"
        f"\n- Current execution time ({timezone_name}): "
        f"{executed_at.astimezone(schedule_timezone).isoformat()}"
        f"\n- Delay seconds: {delay_seconds}"
        "\nInterpret the task's own temporal wording using these platform facts. "
        "Unless the task explicitly asks for current, live, or execution-time "
        "semantics, anchor relative calendar terms and recurring periods to the "
        "scheduled occurrence. Use the current execution time for live state or "
        "current facts. Explicit time ranges in the task take precedence. Do not "
        "invent a business time window that the task itself does not define."
    )
