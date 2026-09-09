"""Recover generation on its original tool-call row, without a second queue."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.services.agent_runtime_workspace import (
    bind_agent_runtime_workspace, current_agent_runtime_workspace, resolve_agent_runtime_workspace,
)
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.agent_tools_media_delivery_runtime import _send_media_to_session
from app.services.llm.failure_outcome import render_message
from app.services.conversation_turn_lifecycle import lock_conversation_turn_running
from app.services.chat_history import persist_tool_call_row
from app.services.media_ai_io import MAX_RESULT_BYTES, MediaAIError, download_media, media_mime
from app.services.media_ai_provider import connection, download_result, poll_generation, request, result_url
from app.services.storage import agent_storage_key, ensure_local_path, store_agent_bytes
from app.services.turn_tool_settings import restore_turn_tool_settings


async def retry_read(operation, check, started_at):
    """Retry only idempotent reads, keeping the same durable upstream task."""
    delay = 5
    while True:
        await check()
        if (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds() > 23 * 3600:
            raise MediaAIError("taskExpired")
        try:
            return await operation()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {408, 429} and exc.response.status_code < 500:
                raise
        except httpx.TransportError:
            pass
        except MediaAIError as exc:
            if not exc.retryable:
                raise
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def find_generation_row(state) -> ChatMessage:
    if not state.session_id or not state.tool_call_id:
        raise MediaAIError("contextRequired")
    async with async_session() as db:
        try:
            session = await db.get(ChatSession, uuid.UUID(state.session_id))
        except ValueError:
            session = None
        storage_agent_id = state.agent_id
        if session is not None:
            if state.agent_id not in {session.agent_id, session.peer_agent_id}:
                raise MediaAIError("contextRequired")
            storage_agent = await db.get(Agent, session.agent_id)
            actor = await db.get(Agent, state.agent_id)
            if not actor or not storage_agent or actor.tenant_id != storage_agent.tenant_id:
                raise MediaAIError("contextRequired")
            storage_agent_id = session.agent_id
        await lock_conversation_turn_running(
            db, agent_id=storage_agent_id, conversation_id=state.session_id,
            turn_anchor_id=state.turn_anchor_id,
        )
        query = select(ChatMessage).where(
            ChatMessage.agent_id.in_({storage_agent_id, state.agent_id}),
            ChatMessage.conversation_id == state.session_id,
            ChatMessage.role == "tool_call",
        )
        if state.turn_anchor_id:
            query = query.where(ChatMessage.message_meta["turn_anchor_id"].as_string() == str(state.turn_anchor_id))
        rows = (await db.scalars(query.order_by(ChatMessage.created_at.desc()).limit(100))).all()
        for row in rows:
            payload = json.loads(row.content)
            if payload.get("name") == "generate_media" and payload.get("call_id") == state.tool_call_id:
                return row
        row_id = await persist_tool_call_row(
            db, agent_id=storage_agent_id, user_id=state.user_id,
            conversation_id=state.session_id,
            evt={"name": "generate_media", "call_id": state.tool_call_id,
                 "args": state.arguments, "status": "running"},
            turn_anchor_id=state.turn_anchor_id, turn_fence_locked=True,
        )
        await db.commit()
        return await db.get(ChatMessage, row_id)


async def lock_generation_turn(db, row: ChatMessage) -> None:
    anchor = (row.message_meta or {}).get("turn_anchor_id")
    try:
        session = await db.get(ChatSession, uuid.UUID(row.conversation_id))
    except ValueError:
        session = None
    if session and row.agent_id not in {session.agent_id, session.peer_agent_id}:
        raise MediaAIError("contextRequired")
    await lock_conversation_turn_running(
        db, agent_id=session.agent_id if session else row.agent_id,
        conversation_id=row.conversation_id, turn_anchor_id=uuid.UUID(anchor) if anchor else None,
    )


async def checkpoint(row_id, agent_id, job: dict) -> None:
    async with async_session() as db:
        row = await db.get(ChatMessage, row_id)
        if row is None or row.agent_id != agent_id:
            raise MediaAIError("contextRequired")
        await lock_generation_turn(db, row)
        await db.refresh(row, with_for_update=True)
        row.message_meta = {**(row.message_meta or {}), "media_job": dict(job)}
        if job.get("status") == "completed":
            row.content = json.dumps({**json.loads(row.content), "status": "done",
                                      "result": json.dumps(job["result"], ensure_ascii=False)}, ensure_ascii=False)
        await db.commit()


async def create_intent(row: ChatMessage, config: dict, kind: str, execution_agent_id) -> tuple[dict, bool]:
    job = {
        "status": "submitting", "output_type": kind,
        "model": config[f"{kind}_model"], "base_url": config["base_url"],
        "execution_agent_id": str(execution_agent_id),
        "storage_prefix": current_agent_runtime_workspace(execution_agent_id).storage_prefix,
        "path_base": f"workspace/media/{row.id}",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    async with async_session() as db:
        await lock_generation_turn(db, row)
        current = await db.get(ChatMessage, row.id, with_for_update=True)
        existing = (current.message_meta or {}).get("media_job")
        if existing:
            return dict(existing), False
        current.message_meta = {**(current.message_meta or {}), "media_job": job}
        await db.commit()
    return job, True


async def accept_result(row: ChatMessage, job: dict, result: dict) -> None:
    output = result.get("output") or {}
    if result.get("_bytes") is not None:
        await save_generated_bytes(job, result["_bytes"])
    elif output.get("task_id"):
        job.update(status="processing", task_id=output["task_id"])
    else:
        url = result_url(result, job["output_type"])
        job.update(status="saving", result_ref=encrypt_data(url, get_settings().SECRET_KEY))
    job["usage"] = result.get("usage") or {}
    job["request_id"] = result.get("request_id", "")
    await checkpoint(row.id, row.agent_id, job)


async def save_generated_bytes(job: dict, data: bytes) -> None:
    mime = media_mime(data)
    if mime.split("/", 1)[0] != job["output_type"]:
        raise MediaAIError("invalidMedia")
    extension = {"image/jpeg": "jpg", "audio/mpeg": "mp3", "audio/x-wav": "wav"}.get(mime, mime.split("/")[1])
    path = f"{job['path_base']}.{extension}"
    await store_agent_bytes(uuid.UUID(job["execution_agent_id"]), path, data, content_type=mime)
    job.update(status="delivering", file={"path": path, "mime_type": mime,
                                          "kind": job["output_type"], "size_bytes": len(data)})
    job.pop("result_ref", None)


async def finish_generation(row: ChatMessage, job: dict, config: dict, *, guard=None, deliver=True) -> str:
    """Resume only GET/download/delivery; this function never submits a POST."""
    if config["base_url"] != job["base_url"]:
        raise MediaAIError("connectionChanged")
    execution_agent_id = uuid.UUID(job["execution_agent_id"])
    if current_agent_runtime_workspace(execution_agent_id).storage_prefix != job["storage_prefix"]:
        raise MediaAIError("contextRequired")

    async def check():
        if guard is not None and not await guard():
            raise MediaAIError("stopped")
        await checkpoint(row.id, row.agent_id, job)

    await check()
    if job.get("status") == "completed":
        return json.dumps(job["result"], ensure_ascii=False)
    if not job.get("task_id") and not job.get("result_ref") and not job.get("file"):
        raise MediaAIError("submissionUnknown")
    while job.get("task_id") and not job.get("result_ref") and not job.get("file"):
        await check()
        task_id = str(job["task_id"])
        operation = (lambda: poll_generation(config, task_id)) if config.get("model_id") else (
            lambda: request(config, "/api/v1/tasks/" + quote(task_id, safe=""))
        )
        result = await retry_read(operation, check, job["started_at"])
        output = result.get("output") or {}
        status = output.get("task_status")
        if status == "SUCCEEDED":
            url = result_url(result, job["output_type"])
            job.update(status="saving", result_ref=encrypt_data(url, get_settings().SECRET_KEY),
                       usage=result.get("usage") or job.get("usage", {}))
            await check()
            break
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            job.update(status="failed", provider_code=output.get("code", status))
            await check()
            raise MediaAIError("providerFailed", provider_code=str(job["provider_code"]))
        if status not in {"PENDING", "RUNNING"}:
            raise MediaAIError("providerFailed")
        # Task/result references expire upstream; never restart an expired job.
        started = datetime.fromisoformat(job["started_at"])
        if (datetime.now(timezone.utc) - started).total_seconds() > 23 * 3600:
            raise MediaAIError("taskExpired")
        await asyncio.sleep(5)

    if not job.get("file"):
        await check()
        reference = decrypt_data(job["result_ref"], get_settings().SECRET_KEY)
        operation = (lambda: download_result(config, reference)) if reference.startswith("provider:") else (
            lambda: download_media(reference, max_bytes=MAX_RESULT_BYTES)
        )
        data = await retry_read(operation, check, job["started_at"])
        await check()
        await save_generated_bytes(job, data)
        await check()

    file = job["file"]
    result = {"type": "media_generation", "status": "completed", "files": [file],
              "model": job["model"], "usage": job.get("usage", {})}
    if job["output_type"] == "image":
        result["markdown"] = f"![{render_message('mediaAI.image')}]({file['path']})"
    elif deliver:
        await check()
        result["delivery"] = await deliver_generation(row, execution_agent_id, file)
    job.update(status="completed", result=result)
    await check()
    return json.dumps(result, ensure_ascii=False)


async def deliver_generation(row: ChatMessage, execution_agent_id, file: dict) -> dict:
    payload = json.loads(row.content)
    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(row.conversation_id))
        if session and dict(session.im_config or {}).get("executor") == "media":
            run = await db.get(SubagentRun, session.id)
            session = await db.get(ChatSession, run.parent_session_id) if run else None
        if session and session.source_channel in {"agent", "subagent", "project"}:
            # A2A exchanges workspace files; its channel has no media player delivery.
            return {"type": "media_delivery_result", "version": 1,
                    "status": "unsupported", "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "media_kind": file["kind"], "intent_id": payload["call_id"],
                    "session_id": str(session.id), "channel": session.source_channel}
    path = await ensure_local_path(agent_storage_key(execution_agent_id, file["path"]))
    return json.loads(await _send_media_to_session(
        agent_id=execution_agent_id, session_id=str(session.id) if session else row.conversation_id,
        file_path=path, workspace_path=file["path"], media_kind=file["kind"],
        caption="", cover_path=None, intent_id=payload["call_id"],
        origin_session_id=row.conversation_id,
        origin_turn_anchor_id=uuid.UUID(row.message_meta["turn_anchor_id"]) if row.message_meta.get("turn_anchor_id") else None,
        allow_download=True, tool_args=payload.get("args") or {},
    ))


async def recover_generation(row: ChatMessage, *, execution_agent_id, guard) -> str | None:
    if not (row.message_meta or {}).get("media_job"):
        return None
    if str(execution_agent_id) != row.message_meta["media_job"].get("execution_agent_id"):
        raise MediaAIError("contextRequired")
    async with async_session() as db:
        agent = await db.get(Agent, execution_agent_id)
        session = await db.get(ChatSession, uuid.UUID(row.conversation_id))
        if agent is None or session is None:
            raise MediaAIError("contextRequired")
        workspace = resolve_agent_runtime_workspace(
            agent_id=agent.id, agent_scope=agent.scope, agent_project_id=agent.project_id,
            tenant_id=agent.tenant_id, session_project_id=session.project_id,
            session_config=session.im_config,
        )
    anchor_id = (row.message_meta or {}).get("turn_anchor_id")
    async with restore_turn_tool_settings(execution_agent_id, row.conversation_id, anchor_id):
        async with async_session() as db:
            anchor = await db.get(ChatMessage, uuid.UUID(anchor_id)) if anchor_id else None
            frozen = ((anchor.message_meta or {}).get("media_request") or {}).get("connection_ref") if anchor else None
        config = connection(json.loads(decrypt_data(frozen, get_settings().SECRET_KEY))) if frozen else connection(
            await _get_tool_config(execution_agent_id, "generate_media") or {}
        )
        with bind_agent_runtime_workspace(workspace):
            return await finish_generation(row, dict(row.message_meta["media_job"]), config, guard=guard)
