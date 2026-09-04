"""Timezone utilities for resolving and projecting Agent-local time."""

import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session


# Common timezones for frontend dropdown
COMMON_TIMEZONES = [
    "UTC",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Singapore",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Moscow",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Australia/Sydney",
    "Pacific/Auckland",
]


async def get_agent_timezone(agent_id: uuid.UUID) -> str:
    """Resolve effective timezone for an agent.

    Priority: agent.timezone → tenant.timezone → 'UTC'
    """
    from app.models.agent import Agent
    from app.models.tenant import Tenant

    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return "UTC"
        tenant = await db.get(Tenant, agent.tenant_id) if agent.tenant_id else None
        return get_agent_timezone_sync(agent, tenant)


async def get_agent_timezone_in_session(db: AsyncSession, agent: Any) -> str:
    """Resolve effective timezone without opening a second DB transaction."""
    if getattr(agent, "timezone", None):
        return normalize_timezone_name(agent.timezone)
    tenant_id = getattr(agent, "tenant_id", None)
    if tenant_id:
        from app.models.tenant import Tenant

        tenant = await db.get(Tenant, tenant_id)
        if tenant and tenant.timezone:
            return normalize_timezone_name(tenant.timezone)
    return "UTC"


def get_agent_timezone_sync(agent, tenant=None) -> str:
    """Synchronous version — when agent and tenant objects are already loaded.

    Priority: agent.timezone → tenant.timezone → 'UTC'
    """
    if agent.timezone:
        return normalize_timezone_name(agent.timezone)
    if tenant and hasattr(tenant, 'timezone') and tenant.timezone:
        return normalize_timezone_name(tenant.timezone)
    return "UTC"


def normalize_timezone_name(tz_name: str | None) -> str:
    """Return a valid IANA timezone name, falling back safely to UTC."""
    candidate = str(tz_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return candidate


def parse_datetime_for_agent(value: Any, tz_name: str) -> datetime | None:
    """Parse a tool timestamp into canonical UTC.

    Offset-bearing inputs keep their absolute instant. Naive inputs, including
    date-only ISO values, are interpreted in the Agent's effective timezone.
    Empty values return ``None``; malformed values raise ``ValueError``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(normalize_timezone_name(tz_name)))
    return parsed.astimezone(timezone.utc)


def canonical_utc_datetime(value: datetime | None) -> datetime | None:
    """Normalize persisted timestamps to UTC without changing their instant."""
    if value is None:
        return None
    if value.tzinfo is None:
        # PostgreSQL timestamps are canonical UTC. This also keeps legacy/test
        # rows deterministic if a driver returns a naive value.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_datetime_for_agent(value: datetime | None, tz_name: str) -> str | None:
    """Render a canonical timestamp as an unambiguous Agent-local value."""
    canonical = canonical_utc_datetime(value)
    if canonical is None:
        return None
    effective_name = normalize_timezone_name(tz_name)
    local = canonical.astimezone(ZoneInfo(effective_name))
    return f"{local.isoformat()} [{effective_name}]"


def add_local_datetime_projections(value: Any, tz_name: str) -> Any:
    """Recursively add ``*_local`` siblings while preserving machine fields."""
    if isinstance(value, list):
        return [add_local_datetime_projections(item, tz_name) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {
        key: add_local_datetime_projections(item, tz_name)
        for key, item in value.items()
    }
    for key, item in value.items():
        if not str(key).endswith("_at") or item in (None, ""):
            continue
        try:
            parsed = (
                item
                if isinstance(item, datetime)
                else datetime.fromisoformat(str(item).replace("Z", "+00:00"))
            )
            local_key = f"{key}_local"
            projected.setdefault(local_key, format_datetime_for_agent(parsed, tz_name))
        except (TypeError, ValueError):
            continue
    return projected


def normalize_datetime_arguments(
    arguments: dict[str, Any],
    tz_name: str,
    *field_names: str,
) -> dict[str, Any]:
    """Canonicalize selected tool inputs, interpreting naive values locally."""
    normalized = dict(arguments)
    for field_name in field_names:
        if field_name not in normalized or normalized[field_name] in (None, ""):
            continue
        parsed = parse_datetime_for_agent(normalized[field_name], tz_name)
        normalized[field_name] = parsed.isoformat() if parsed else None
    return normalized


def now_in_timezone(tz_name: str) -> datetime:
    """Get current datetime in the given timezone."""
    return datetime.now(ZoneInfo(normalize_timezone_name(tz_name)))
