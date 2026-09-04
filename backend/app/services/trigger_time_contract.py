"""Timezone contract shared by trigger tools and trigger evaluators."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.timezone_utils import (
    format_datetime_for_agent,
    get_agent_timezone,
    normalize_timezone_name,
    parse_datetime_for_agent,
)


def normalize_trigger_time_config(
    trigger_type: str,
    config: dict[str, Any],
    timezone_name: str,
) -> dict[str, Any]:
    """Return config with absolute instants canonicalized while preserving data."""
    normalized = dict(config)
    configured_timezone = normalized.get("timezone")
    if configured_timezone:
        candidate = str(configured_timezone).strip()
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"invalid IANA timezone: {candidate}") from exc
        normalized["timezone"] = candidate
    effective_timezone = normalize_timezone_name(configured_timezone or timezone_name)
    if trigger_type == "once" and normalized.get("at"):
        try:
            parsed = parse_datetime_for_agent(normalized["at"], effective_timezone)
        except (TypeError, ValueError) as exc:
            raise ValueError("once config.at must be a valid ISO 8601 timestamp") from exc
        normalized["at"] = parsed.isoformat()
        normalized["_input_timezone"] = effective_timezone
    return normalized


async def resolve_once_trigger_at(trigger: Any, config: dict[str, Any]) -> datetime:
    """Resolve new canonical and legacy naive once timestamps consistently."""
    raw = config.get("at")
    if not raw:
        raise ValueError("once trigger has no config.at")
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parse_datetime_for_agent(raw, "UTC")
    timezone_name = str(
        config.get("timezone")
        or config.get("_input_timezone")
        or await get_agent_timezone(trigger.agent_id)
    )
    resolved = parse_datetime_for_agent(raw, timezone_name)
    if resolved is None:
        raise ValueError("once trigger has no config.at")
    return resolved


def project_trigger_config(config: dict[str, Any], timezone_name: str) -> dict[str, Any]:
    """Preserve machine fields and add an unambiguous Agent-local projection."""
    public = {key: value for key, value in config.items() if not str(key).startswith("_")}
    raw_at = public.get("at")
    if raw_at:
        try:
            parsed = parse_datetime_for_agent(raw_at, timezone_name)
            public["at_local"] = format_datetime_for_agent(parsed, timezone_name)
        except (TypeError, ValueError):
            public["at_local"] = "invalid timestamp"
    return public
