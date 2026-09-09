"""Admission of media turns into the existing durable child-session runtime."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.media_ai_io import MediaAIError, normalize_sources
from app.services.llm.failure_outcome import render_message


def task_receipt(row: ChatMessage) -> dict:
    meta = row.message_meta or {}
    status = meta.get("turn_status")
    if status not in {"completed", "failed", "cancelled"}:
        status = "running" if meta.get("subagent_input_state") == "processing" else "queued"
    return {"type": "media_task", "task_id": str(row.id), "session_id": row.conversation_id, "status": status}


async def find_submitted_task(parent_id, call_id) -> ChatMessage | None:
    async with async_session() as db:
        return await db.scalar(select(ChatMessage).where(
            ChatMessage.message_meta["media_request"]["origin_session_id"].as_string() == str(parent_id),
            ChatMessage.message_meta["media_request"]["origin_call_id"].as_string() == call_id,
            ChatMessage.role == "user",
        ))


async def enqueue_media(state, config: dict, *, notify_parent: bool = True) -> dict:
    from app.services.subagent_runtime import SubagentError, append_subagent_message, create_subagent

    if not state.session_id or not state.tool_call_id or not state.turn_anchor_id:
        raise MediaAIError("contextRequired")
    args = dict(state.arguments)
    session_id = args.pop("session_id", None)
    if "files" in args:
        args["files"] = normalize_sources(args["files"])
    if state.tool_name == "read_media" and not session_id and not args.get("files"):
        raise MediaAIError("inputCombination")
    existing = await find_submitted_task(state.session_id, state.tool_call_id)
    if existing is not None:
        return task_receipt(existing)
    request = {
        "tool": state.tool_name, "arguments": args,
        "config": {key: value for key, value in config.items() if key not in {"api_key", "runtime_model"}},
        "origin_session_id": state.session_id, "origin_call_id": state.tool_call_id,
        "workspace": current_agent_runtime_workspace(state.agent_id).as_session_config(),
        "connection_ref": encrypt_data(json.dumps(config), get_settings().SECRET_KEY),
    }
    async with async_session() as db:
        parent_anchor = await db.get(ChatMessage, state.turn_anchor_id)
        parent_meta = dict(parent_anchor.message_meta or {}) if parent_anchor else {}
    metadata = {key: parent_meta[key] for key in ("scene_key", "scene_revision", "activation_source") if key in parent_meta}
    metadata["media_request"] = request
    try:
        if session_id:
            async with async_session() as db:
                child = await db.get(ChatSession, uuid.UUID(session_id))
                run = await db.get(SubagentRun, uuid.UUID(session_id))
                if (child is None or run is None or child.agent_id != state.agent_id
                        or str(run.parent_session_id) != str(state.session_id)
                        or run.execution_user_id != state.user_id
                        or dict(child.im_config or {}).get("executor") != "media"):
                    raise MediaAIError("contextRequired")
            await append_subagent_message(
                agent_id=state.agent_id, execution_user_id=state.user_id,
                parent_session_id=state.session_id, subagent_id=session_id,
                origin_tool_call_id=state.tool_call_id, message=args["prompt"],
                input_metadata=metadata, executor="media",
            )
        else:
            await create_subagent(
                agent_id=state.agent_id, execution_user_id=state.user_id,
                parent_session_id=state.session_id, origin_tool_call_id=state.tool_call_id,
                name=args["prompt"][:80], task=args["prompt"], mode="async" if notify_parent else "sync",
                executor="media", soul=False, memory=False, fork=False,
                turn_anchor_id=state.turn_anchor_id, input_metadata=metadata,
            )
    except (SubagentError, ValueError) as exc:
        if isinstance(exc, MediaAIError):
            raise
        raise MediaAIError("contextRequired") from exc
    row = await find_submitted_task(state.session_id, state.tool_call_id)
    if row is None:
        raise MediaAIError("contextRequired")
    return task_receipt(row)


async def recover_media_submission(row: ChatMessage) -> str | None:
    payload = json.loads(row.content)
    task = await find_submitted_task(row.conversation_id, payload.get("call_id", ""))
    return json.dumps(task_receipt(task), ensure_ascii=False) if task else None


async def cancel_pending_media_task(db, child, task_id: str) -> str | None:
    """Cancel a queued media input using the existing Run -> input lock order."""
    from app.services.subagent_runtime import SubagentError

    try:
        row = await db.get(ChatMessage, uuid.UUID(task_id), with_for_update=True)
    except ValueError as exc:
        raise SubagentError(render_message("mediaAI.invalidArguments")) from exc
    if row is None or row.conversation_id != str(child.id) or not (row.message_meta or {}).get("media_request"):
        raise SubagentError(render_message("mediaAI.contextRequired"))
    meta = dict(row.message_meta or {})
    state = meta.get("subagent_input_state")
    if state == "processing":
        return None
    if state == "pending":
        row.message_meta = {**meta, "subagent_input_state": "cancelled", "turn_status": "cancelled"}
        from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session, transition_conversation_turn

        if conversation_turn_snapshot_for_session(child).anchor_id == row.id:
            await transition_conversation_turn(db, agent_id=child.agent_id, conversation_id=str(child.id),
                                              turn_anchor_id=row.id, status="cancelled")
        await db.commit()
        return "cancelled"
    return task_receipt(row)["status"]
