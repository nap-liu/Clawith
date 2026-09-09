"""Deterministic media execution inside the shared leased child worker."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.core.security import decrypt_data
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.services.agent_runtime_workspace import bind_agent_runtime_workspace, resolve_agent_runtime_workspace
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.chat_attachments import attachment_from_workspace_path
from app.services.media_ai_context import prepare_media_context
from app.services.media_ai_io import MediaAIError, load_media, load_understanding_media
from app.services.media_ai_jobs import accept_result, create_intent, find_generation_row, finish_generation
from app.services.media_ai_provider import connection, generation_payload, request, understand, understand_response
from app.services.media_ai_tools import error_result
from app.services.read_media_compat import media_input_workspace
from app.services.llm.failure_outcome import render_message
from app.services.media_url_source import MediaUrlError
from app.services.turn_tool_settings import restore_turn_tool_settings


async def is_media_session(session_id) -> bool:
    async with async_session() as db:
        child = await db.get(ChatSession, session_id)
        return child is not None and dict(child.im_config or {}).get("executor") == "media"


async def _checkpoint_input(run_id, anchor_id, updates: dict):
    from app.services.subagent_runtime import _owns_subagent_lease

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if not _owns_subagent_lease(run):
            raise asyncio.CancelledError
        anchor = await db.get(ChatMessage, anchor_id, with_for_update=True)
        anchor.message_meta = {**dict(anchor.message_meta or {}), **updates}
        await db.commit()


async def _generate_turn(agent, child, run, anchor, media_request, config):
    from app.services.subagent_runtime import _assert_subagent_running

    async def guard():
        await _assert_subagent_running(run.id)
        return True

    async with async_session() as db:
        row = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == str(child.id), ChatMessage.role == "tool_call",
            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
        ).order_by(ChatMessage.created_at.desc()).limit(1))
    job = dict((row.message_meta or {}).get("media_job") or {}) if row else {}
    if job:
        # Upstream has already accepted this request. Input URLs may have expired;
        # recovery needs only the durable provider task/result and saved parameters.
        args = json.loads(row.content)["args"]
    else:
        args, _history = await prepare_media_context(agent, child, anchor, media_request)
        media = await load_media(agent.id, args.get("files", []))
        state = SimpleNamespace(
            agent_id=agent.id, user_id=run.execution_user_id, session_id=str(child.id),
            tool_call_id=str(anchor.id), turn_anchor_id=anchor.id, arguments=args,
        )
        row = await find_generation_row(state)
        path, payload = generation_payload(config, args, media)
        job, claimed = await create_intent(row, config, args["output_type"], agent.id)
        if claimed:
            await guard()
            try:
                response = await request(config, path, payload, asynchronous=args["output_type"] == "video")
            except httpx.HTTPError as exc:
                raise MediaAIError("submissionUnknown") from exc
            await guard()
            await accept_result(row, job, response)
    return json.loads(await finish_generation(row, job, config, guard=guard)), args


async def execute_media_turn(run_id, anchor, *, recovering=False) -> bool:
    from app.services.subagent_runtime import _assert_subagent_running, _finish_subagent_turn, _validate_execution_identity

    meta = dict(anchor.message_meta or {})
    media_request = meta.get("media_request") or {}
    result = meta.get("media_result")
    context = meta.get("media_context") or {}
    responses_snapshot = meta.get("media_responses_snapshot")
    attachments = []
    try:
        await _assert_subagent_running(run_id)
        async with async_session() as db:
            run = await db.get(SubagentRun, run_id)
            child = await db.get(ChatSession, run_id)
            agent = await _validate_execution_identity(db, run, child)
            session_config = {**dict(child.im_config or {}), "agent_runtime_workspace": media_request.get("workspace")}
            workspace = resolve_agent_runtime_workspace(
                agent_id=agent.id, agent_scope=agent.scope, agent_project_id=agent.project_id,
                tenant_id=agent.tenant_id, session_project_id=child.project_id, session_config=session_config,
            )
        if not media_request:
            raise MediaAIError("invalidArguments")
        async with restore_turn_tool_settings(agent.id, str(child.id), anchor.id):
            if media_request.get("connection_ref"):
                config = connection(json.loads(decrypt_data(media_request["connection_ref"], get_settings().SECRET_KEY)))
            else:
                live = await _get_tool_config(agent.id, media_request["tool"]) or {}
                config = connection({**live, **media_request["config"]})
            with bind_agent_runtime_workspace(workspace):
                if result is None:
                    if recovering and meta.get("media_read_started"):
                        raise MediaAIError("analysisInterrupted")
                    if media_request["tool"] == "read_media":
                        args, history = await prepare_media_context(agent, child, anchor, media_request)
                        with media_input_workspace(agent.id, media_request):
                            media, input_errors = await load_understanding_media(
                                agent.id, args.get("files", []), loader=load_media,
                            )
                        await _assert_subagent_running(run_id)
                        await _checkpoint_input(run_id, anchor.id, {"media_read_started": True})
                        if config.get("model_id"):
                            response = await understand_response(config, args["prompt"], media, history=history)
                            text, usage = response.content, response.usage or {}
                            actual_model = response.model or config["understanding_model"]
                            responses_snapshot = response.responses_snapshot
                        else:
                            text, usage = await understand(config, args["prompt"], media, history=history)
                            actual_model = config["understanding_model"]
                        if input_errors:
                            text += "\n\n" + render_message("mediaAI.partialInputs") + "\n" + "\n".join(
                                f"{item['index']}: {item['code']}" for item in input_errors
                            )
                        result = {"status": "completed", "text": text, "usage": usage,
                                  "input_errors": input_errors,
                                  "model": actual_model}
                    else:
                        result, args = await _generate_turn(agent, child, run, anchor, media_request, config)
                    context = {
                        "sources": args.get("_context_sources", args.get("files", [])), "files": result.get("files", []),
                        "parameters": {key: args[key] for key in ("ratio", "size", "resolution", "voice", "duration") if key in args},
                    }
                    await _checkpoint_input(run_id, anchor.id, {
                        "media_result": result, "media_context": context,
                        "media_responses_snapshot": responses_snapshot,
                    })
        attachments = [attachment_from_workspace_path(
            item["path"], mime_type=item["mime_type"], size_bytes=item["size_bytes"],
        ) for item in result.get("files", [])]
    except MediaAIError as exc:
        result = json.loads(error_result(exc))
    except MediaUrlError:
        result = json.loads(error_result(MediaAIError("unsafeUrl")))
    except (httpx.HTTPError, OSError):
        result = json.loads(error_result(MediaAIError("connectionFailed")))
    await _assert_subagent_running(run_id)
    result = {**result, "task_id": str(anchor.id), "session_id": str(run_id)}
    failed = result.get("status") != "completed"
    reply = result.get("text") or result.get("message") or render_message("mediaAI.generationCompleted")
    return await _finish_subagent_turn(
        run_id=run_id, anchor_id=anchor.id, reply=reply, failed=failed,
        failure_code=result.get("code"), attachments=attachments,
        result_meta={"media_result": result, "media_context": context,
                     **({"responses_snapshot": responses_snapshot} if responses_snapshot else {})},
    )
