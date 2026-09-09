"""Media tool entrypoints: validate and enqueue, without waiting on providers."""

from __future__ import annotations

import json

from jsonschema import FormatChecker, ValidationError, validate
from sqlalchemy import select

from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.llm.failure_outcome import render_message
from app.services.media_ai_contract import GENERATE_MEDIA_SCHEMA, READ_MEDIA_SCHEMA
from app.services.media_ai_io import MediaAIError
from app.services.media_ai_sessions import enqueue_media, find_submitted_task, task_receipt
from app.services.media_model_selection import resolve_media_model
from app.services.turn_tool_settings import current_tool_settings


async def media_tool_enabled(agent_id, name: str) -> bool:
    scope = current_tool_settings(agent_id)
    if scope is not None:
        return name in scope.enabled_names
    async with async_session() as db:
        return bool(await db.scalar(select(AgentTool.id).join(Tool, Tool.id == AgentTool.tool_id).where(
            AgentTool.agent_id == agent_id, AgentTool.enabled.is_(True),
            Tool.name == name, Tool.enabled.is_(True), Tool.source == "builtin",
        )))


def error_result(error: MediaAIError) -> str:
    return json.dumps({
        "status": "failed", "code": error.code,
        "message": render_message(f"mediaAI.{error.code}"),
        **({"provider_code": error.provider_code} if error.provider_code else {}),
    }, ensure_ascii=False)


async def execute_media_tool(state) -> str:
    try:
        if not await media_tool_enabled(state.agent_id, state.tool_name):
            raise MediaAIError("toolDisabled")
        schema = READ_MEDIA_SCHEMA if state.tool_name == "read_media" else GENERATE_MEDIA_SCHEMA
        validate(state.arguments, schema, format_checker=FormatChecker())
        if not state.arguments["prompt"].strip():
            raise MediaAIError("invalidArguments")
        existing = await find_submitted_task(state.session_id, state.tool_call_id)
        if existing is not None:
            return json.dumps(task_receipt(existing), ensure_ascii=False)
        config = await resolve_media_model(state, await _get_tool_config(state.agent_id, state.tool_name) or {})
        return json.dumps(await enqueue_media(state, config), ensure_ascii=False)
    except ValidationError:
        return error_result(MediaAIError("invalidArguments"))
    except MediaAIError as exc:
        return error_result(exc)
