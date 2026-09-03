"""Small, provider-neutral directory scheduling policy."""

import calendar
from datetime import datetime, timedelta, timezone


SYNC_INTERVAL_UNITS = frozenset({"hour", "day", "week", "month"})
MAX_SYNC_INTERVAL_VALUE = 365


def validate_sync_policy(enabled: bool, value: int | None, unit: str | None) -> None:
    """Validate the fixed scheduling dimensions exposed by every provider."""
    if not enabled and value is None and unit is None:
        return
    if value is None or isinstance(value, bool) or not 1 <= value <= MAX_SYNC_INTERVAL_VALUE:
        raise ValueError(f"sync_interval_value must be between 1 and {MAX_SYNC_INTERVAL_VALUE}")
    if unit not in SYNC_INTERVAL_UNITS:
        raise ValueError("sync_interval_unit must be hour, day, week, or month")


def next_sync_time(after: datetime, value: int, unit: str) -> datetime:
    """Return the next UTC run time, using calendar semantics for months."""
    validate_sync_policy(True, value, unit)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    after = after.astimezone(timezone.utc)
    if unit == "hour":
        return after + timedelta(hours=value)
    if unit == "day":
        return after + timedelta(days=value)
    if unit == "week":
        return after + timedelta(weeks=value)

    month_index = after.year * 12 + (after.month - 1) + value
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(after.day, calendar.monthrange(year, month)[1])
    return after.replace(year=year, month=month, day=day)
