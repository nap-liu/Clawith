"""Internal, capability-authenticated bridge to the existing ToolCall path."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import PlainTextResponse, Response

from app.services.agent_tools import execute_tool
from app.services.turn_tool_settings import restore_turn_tool_settings
from app.services.toolscall.capability import (
    ToolscallUnavailable,
    verify_toolscall_context,
)
from app.services.toolscall.runtime import (
    get_toolscall_signing_seed,
    toolscall_scope_allows,
)

router = APIRouter(
    prefix="/internal/toolscall/v1",
    tags=["internal-toolscall"],
    include_in_schema=False,
)


def _protocol_error(message: str, status_code: int) -> PlainTextResponse:
    return PlainTextResponse(message, status_code=status_code)


@router.post("/call/{tool_name}")
async def call_tool(
    tool_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    if not authorization or not authorization.startswith("Bearer "):
        return _protocol_error("valid toolscall execution context required", 401)
    try:
        signing_seed = await get_toolscall_signing_seed()
        context = verify_toolscall_context(
            authorization[7:].strip(),
            signing_seed=signing_seed,
        )
    except ToolscallUnavailable as exc:
        return _protocol_error(str(exc), 503)
    except ValueError as exc:
        return _protocol_error(str(exc), 401)

    try:
        allowed = await toolscall_scope_allows(
            scope_id=context["scope"],
            tool_name=tool_name,
        )
    except ToolscallUnavailable as exc:
        return _protocol_error(str(exc), 503)
    if not allowed:
        return _protocol_error(
            f"tool is not available in this execution context: {tool_name}", 403
        )

    try:
        arguments = json.loads(await request.body() or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _protocol_error("request body must be one JSON object", 400)
    if not isinstance(arguments, dict):
        return _protocol_error("request body must be one JSON object", 400)

    try:
        agent_id = uuid.UUID(str(context["agent"]))
        user_id = uuid.UUID(str(context["user"]))
        turn_anchor_id = (
            uuid.UUID(str(context["turn"])) if context.get("turn") else None
        )
    except (KeyError, TypeError, ValueError):
        return _protocol_error("invalid identity in execution context", 401)

    nested_call_id = f"toolscall_{uuid.uuid4().hex}"
    session_id = str(context.get("session") or "")
    async with restore_turn_tool_settings(agent_id, session_id, turn_anchor_id):
        result: str = await execute_tool(
            tool_name,
            arguments,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            tool_call_id=nested_call_id,
            turn_anchor_id=turn_anchor_id,
        )
    if not isinstance(result, str):
        return _protocol_error("tool executor returned a non-text result", 500)
    return Response(content=result.encode("utf-8"), media_type="text/plain")
