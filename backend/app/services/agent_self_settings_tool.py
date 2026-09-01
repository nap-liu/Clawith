"""Contract and runtime handler for a Digital Employee's own settings tool."""
from __future__ import annotations

import json
import uuid

from pydantic import ValidationError
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.services.agent_settings_update import (
    AgentSettingsPatch,
    apply_agent_settings_patch,
    public_setting_name,
)


UPDATE_SELF_SETTINGS_PARAMETERS = AgentSettingsPatch.model_json_schema()
UPDATE_SELF_SETTINGS_FUNCTION_TOOL = {
    "type": "function",
    "function": {
        "name": "update_self_settings",
        "description": (
            "Update ordinary settings for myself, the current Digital Employee. "
            "Only provided fields change. Use imagination for creative variation. "
            "This cannot target another Digital Employee or modify permissions, "
            "approval policy, credentials, Soul, or Core Memory."
        ),
        "parameters": UPDATE_SELF_SETTINGS_PARAMETERS,
    },
}
UPDATE_SELF_SETTINGS_TOOL_SEED = {
    "name": "update_self_settings",
    "display_name": "Update My Settings",
    "description": UPDATE_SELF_SETTINGS_FUNCTION_TOOL["function"]["description"],
    "category": "general",
    "icon": "⚙️",
    "is_default": True,
    "parameters_schema": UPDATE_SELF_SETTINGS_PARAMETERS,
    "config": {},
    "config_schema": {"fields": []},
}


async def handle_update_self_settings(agent_id: uuid.UUID, arguments: dict) -> str:
    try:
        patch = AgentSettingsPatch.model_validate(arguments)
    except ValidationError as exc:
        return f"❌ 设置参数无效：{exc.errors(include_url=False)}"

    async with async_session() as db:
        agent = (
            await db.execute(
                select(Agent).where(
                    Agent.id == agent_id,
                    Agent.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            return "❌ 当前数字员工不存在或已删除。"
        try:
            outcome = await apply_agent_settings_patch(
                db,
                agent,
                patch.model_dump(exclude_unset=True),
            )
        except (ValueError, ValidationError) as exc:
            await db.rollback()
            return f"❌ 设置未更新：{exc}"
        if not outcome.changes:
            return json.dumps(
                {"status": "unchanged", "message": "未提供变更，或设置值已经相同。"},
                ensure_ascii=False,
            )
        await db.commit()
        return json.dumps(
            {
                "status": "updated",
                "digital_employee_id": str(agent.id),
                "changed_fields": [public_setting_name(field) for field, _, _ in outcome.changes],
                "clamped_fields": outcome.clamps,
                "applies_from": "next_turn",
            },
            ensure_ascii=False,
        )


__all__ = [
    "UPDATE_SELF_SETTINGS_FUNCTION_TOOL",
    "UPDATE_SELF_SETTINGS_PARAMETERS",
    "UPDATE_SELF_SETTINGS_TOOL_SEED",
    "handle_update_self_settings",
]
