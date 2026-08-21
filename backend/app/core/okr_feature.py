"""Central retirement gate for the legacy OKR product surface.

The data model is intentionally retained.  This module provides the small set
of invariants needed to keep API, Agent, tool, and trigger entry points closed
while the feature flag is disabled (the default).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Iterable

from sqlalchemy import or_, select

from app.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.agent import Agent


OKR_TOOL_NAMES = frozenset(
    {
        "get_okr",
        "get_my_okr",
        "update_kr_progress",
        "update_kr_content",
        "collect_okr_progress",
        "generate_okr_report",
        "get_okr_settings",
        "create_objective",
        "create_key_result",
        "update_objective",
        "update_any_kr_progress",
        "generate_monthly_okr_report",
        "upsert_member_daily_report",
    }
)

OKR_SYSTEM_TRIGGER_NAMES = frozenset(
    {
        "daily_okr_collection",
        "daily_okr_report",
        "weekly_okr_report",
        "monthly_okr_report",
        "biweekly_okr_checkin",
    }
)

_retired_okr_agent_ids: frozenset[uuid.UUID] = frozenset()


def okr_feature_enabled() -> bool:
    """Return the process-wide feature state; false is the safe default."""
    return bool(get_settings().OKR_FEATURE_ENABLED)


def configure_retired_okr_agent_ids(agent_ids: Iterable[uuid.UUID]) -> None:
    """Install the process-wide immutable identity set used by hot paths."""
    global _retired_okr_agent_ids
    _retired_okr_agent_ids = frozenset(agent_ids)


async def refresh_retired_okr_agent_ids(db: "AsyncSession") -> frozenset[uuid.UUID]:
    """Load canonical and legacy Agent identities once during process startup."""
    if okr_feature_enabled():
        configure_retired_okr_agent_ids(())
        return _retired_okr_agent_ids

    from app.models.agent import Agent
    from app.models.okr import OKRSettings

    settings_result = await db.execute(
        select(OKRSettings.okr_agent_id).where(
            OKRSettings.okr_agent_id.is_not(None)
        )
    )
    legacy_result = await db.execute(
        select(Agent.id).where(
            Agent.is_system.is_(True),
            Agent.name == "OKR Agent",
        )
    )
    # ``scalars`` is always present on a real SQLAlchemy Result. The fallback
    # keeps narrow lifespan test doubles compatible without weakening runtime
    # behavior; database execution errors still fail startup closed.
    settings_ids = set(settings_result.scalars().all()) if hasattr(settings_result, "scalars") else set()
    legacy_ids = set(legacy_result.scalars().all()) if hasattr(legacy_result, "scalars") else set()
    configure_retired_okr_agent_ids(settings_ids | legacy_ids)
    return _retired_okr_agent_ids


def hidden_okr_agent_clause(agent_model):
    """SQL predicate used by hot list queries without a cross-table join.

    Direct access and execution use :func:`is_retired_okr_agent` to confirm the
    canonical settings link.  List queries use the immutable system identity so
    they remain available in partial-schema tools and do not tax every Agent
    listing with a correlated subquery.
    """
    legacy_identity = agent_model.is_system.is_(True) & (agent_model.name == "OKR Agent")
    if not _retired_okr_agent_ids:
        return legacy_identity
    return or_(legacy_identity, agent_model.id.in_(_retired_okr_agent_ids))


async def is_retired_okr_agent(db: "AsyncSession", agent: "Agent | None") -> bool:
    """Resolve the canonical Agent identity, with a legacy fallback."""
    if okr_feature_enabled() or agent is None:
        return False

    if getattr(agent, "id", None) in _retired_okr_agent_ids:
        return True
    if (
        getattr(agent, "is_system", False) is True
        and getattr(agent, "name", None) == "OKR Agent"
    ):
        return True

    # The startup identity snapshot covers ordinary requests.  A system Agent
    # may still be linked by an operator-side data repair after startup, so keep
    # a canonical fallback without adding queries to ordinary Agent hot paths.
    if getattr(agent, "is_system", False) is not True:
        return False

    from app.models.okr import OKRSettings

    linked = await db.scalar(
        select(OKRSettings.tenant_id)
        .where(OKRSettings.okr_agent_id == agent.id)
        .limit(1)
    )
    if linked is not None:
        return True
    return False


def is_retired_okr_tool(tool_name: str | None) -> bool:
    return not okr_feature_enabled() and str(tool_name or "") in OKR_TOOL_NAMES


def is_retired_okr_trigger(
    trigger_name: str | None,
    agent_id: uuid.UUID | None = None,
) -> bool:
    return (
        not okr_feature_enabled()
        and str(trigger_name or "") in OKR_SYSTEM_TRIGGER_NAMES
        and agent_id in _retired_okr_agent_ids
    )


def partition_retired_okr_triggers(triggers):
    """Split a runtime batch without suppressing unrelated triggers."""
    retired = [
        trigger
        for trigger in triggers
        if is_retired_okr_trigger(trigger.name, trigger.agent_id)
    ]
    active = [
        trigger
        for trigger in triggers
        if not is_retired_okr_trigger(trigger.name, trigger.agent_id)
    ]
    return retired, active
