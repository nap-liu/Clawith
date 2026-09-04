"""Validation for user-editable scheduling values."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.timezone_utils import parse_datetime_for_agent
from app.services.trigger_runtime.cron_schedule import compute_next_cron_occurrence


def validate_schedule_value(kind: str, value: str, timezone_name: str | None = None) -> bool:
    """Validate with the same parsers used by trigger execution."""
    candidate_timezone = str(timezone_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(candidate_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return False

    try:
        if kind == "cron":
            compute_next_cron_occurrence(
                expr=str(value).strip(),
                base=datetime.now(timezone.utc),
                timezone_name=candidate_timezone,
            )
            return True
        if kind == "datetime":
            return parse_datetime_for_agent(value, candidate_timezone) is not None
        if kind == "timezone":
            return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return False
