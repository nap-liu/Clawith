"""OKR tool-row and trigger-setting helpers for Agent seeding."""

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.okr import OKRSettings
from app.models.tool import Tool
from app.models.trigger import AgentTrigger


async def _ensure_okr_tool_rows_exist(required_tool_names: list[str]) -> dict[str, Tool]:
    """Ensure all required OKR tool definitions exist in the tools table.

    In older deployments, startup sometimes reached OKR Agent seeding/patching
    before the newly added builtin tool rows were visible in the target
    database. When that happened, the OKR Agent could keep a prompt that
    mentioned `upsert_member_daily_report` but still not receive the actual tool
    in its LLM tool list, which later surfaced as `Unknown tool`.

    To make the startup path self-healing, we defensively re-run builtin tool
    seeding if any required OKR tool row is missing, then re-query the rows.
    """
    tool_rows: dict[str, Tool] = {}
    async with async_session() as db:
        result = await db.execute(select(Tool).where(Tool.name.in_(required_tool_names)))
        tool_rows = {tool.name: tool for tool in result.scalars().all()}

    missing = [name for name in required_tool_names if name not in tool_rows]
    if missing:
        logger.warning(
            f"[AgentSeeder] Missing OKR tool rows {missing}; re-running builtin tool seeder"
        )
        from app.services.tool_seeder import seed_builtin_tools
        await seed_builtin_tools()
        async with async_session() as db:
            result = await db.execute(select(Tool).where(Tool.name.in_(required_tool_names)))
            tool_rows = {tool.name: tool for tool in result.scalars().all()}

    return tool_rows


async def _sync_okr_triggers_with_settings(db, agent_id: uuid.UUID, settings: OKRSettings | None) -> bool:
    """Align existing OKR system triggers with tenant report settings."""
    if not settings:
        return False

    changed = False
    daily_hour, daily_minute = 18, 0
    try:
        hour_str, minute_str = settings.daily_report_time.split(":", 1)
        daily_hour = max(0, min(23, int(hour_str)))
        daily_minute = max(0, min(59, int(minute_str)))
    except Exception:
        logger.warning(f"[AgentSeeder] Invalid OKR daily_report_time {settings.daily_report_time}; using 18:00")

    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id == agent_id,
            AgentTrigger.name.in_([
                "daily_okr_collection",
                "daily_okr_report",
                "weekly_okr_report",
                "biweekly_okr_checkin",
                "monthly_okr_report",
            ]),
        )
    )
    triggers = {t.name: t for t in result.scalars().all()}

    desired = {
        "daily_okr_collection": {
            "config": {"expr": f"{daily_minute} {daily_hour} * * *"},
            "is_enabled": bool(settings.enabled and settings.daily_report_enabled),
        },
        "daily_okr_report": {
            "config": {"expr": "0 9 * * *"},
            "is_enabled": bool(settings.enabled),
        },
        "weekly_okr_report": {
            "config": {"expr": "0 9 * * 1"},
            "is_enabled": bool(settings.enabled),
        },
        "biweekly_okr_checkin": {
            "is_enabled": bool(settings.enabled),
            "reason": (
                "System trigger: fires on the 1st and 15th of every month at 10:00 "
                "to perform the mandatory bi-weekly OKR check-in."
            ),
        },
        "monthly_okr_report": {
            "config": {"expr": "0 9 1 * *"},
            "is_enabled": bool(settings.enabled),
            "reason": (
                "System trigger: fires at 09:00 on the 1st of every month to generate "
                "the previous month's company OKR report."
            ),
        },
    }

    for name, values in desired.items():
        trigger = triggers.get(name)
        if not trigger:
            continue
        if "config" in values and trigger.config != values["config"]:
            trigger.config = values["config"]
            changed = True
        if trigger.is_enabled != values["is_enabled"]:
            trigger.is_enabled = values["is_enabled"]
            changed = True
        if "reason" in values and trigger.reason != values["reason"]:
            trigger.reason = values["reason"]
            changed = True

    if changed:
        logger.info("[AgentSeeder] Synced OKR system triggers with settings")
    return changed

