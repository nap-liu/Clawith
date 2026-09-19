from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.okr_feature import is_retired_okr_tool
from app.database import async_session
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.agent_tools import (
    _FORCED_AUTONOMY_LEVELS,
    _TOOL_AUTONOMY_MAP,
    _agent_workspace_root,
    _find_outbound_tool_receipt,
    _get_agent_tenant_id,
)
from app.services.im_delivery import DELIVERY_LEASE, IMDeliveryResult, register_delivery
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.turn_tool_settings import current_tool_settings
from app.services.tool_enablement import tool_is_required
from app.services.llm.failure_outcome import render_message
from app.services.user_project_tools import (
    USER_PROJECT_TOOL_NAMES,
    execute_user_project_tool,
    record_user_project_tool_activity,
    user_project_tool_error,
)

_ROOT_TOOL_SYMBOLS = (
    "_FORCED_AUTONOMY_LEVELS",
    "_TOOL_AUTONOMY_MAP",
    "_agent_workspace_root",
    "_find_outbound_tool_receipt",
    "_get_agent_tenant_id",
)


def _sync_root_tool_symbols() -> None:
    from app.services import agent_tools as _agent_tools_root

    for _name in _ROOT_TOOL_SYMBOLS:
        globals()[_name] = getattr(_agent_tools_root, _name)


@dataclass
class ExecuteToolDispatchContext:
    tool_name: str
    arguments: dict
    agent_id: uuid.UUID
    user_id: uuid.UUID | None
    session_id: str
    tool_call_id: str
    turn_anchor_id: uuid.UUID | None
    on_output: Any
    skip_autonomy: bool
    tools_for_llm: list[dict] | None
    approved_by_human: bool
    project_workspace: str | None
    project_sandbox_scope: Any
    agent_tenant_id: str | None
    ws: Path


async def execute_tool_preflight(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str = "",
    tool_call_id: str = "",
    turn_anchor_id: uuid.UUID | None = None,
    on_output=None,
    skip_autonomy: bool = False,
    tools_for_llm: list[dict] | None = None,
    approved_by_human: bool = False,
    on_progress=None,
) -> str | ExecuteToolDispatchContext:
    """Execute a tool call and return the result as a string.

    Args:
        session_id: The ChatSession ID, used to isolate AgentBay instances
                    per conversation. Passed through to agentbay_* tools.
        skip_autonomy: Skip the autonomy boundary check. Used when a human has
                    already approved this exact action (e.g. a confirmation card
                    the user confirmed) — the card IS the approval, so re-gating
                    on autonomy would be redundant.
        approved_by_human: Internal approval-resume marker. Forced-L3 market
                    actions ignore ordinary ``skip_autonomy`` callers and honor
                    this marker only after a durable approval resolution.
    """
    _sync_root_tool_symbols()
    if not isinstance(tool_name, str):
        tool_name = str(tool_name or "")
    tool_name = (
        tool_name.replace("`", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .strip()
    )
    from app.services.read_media_compat import normalize_read_image_call

    tool_name, arguments = normalize_read_image_call(tool_name, arguments)
    scope = current_tool_settings(agent_id)
    from app.services.project_runtime_tool_catalog import PROJECT_RUNTIME_TOOL_NAMES

    # Project protocol tools have their own validated member/role scope below.
    if scope is not None and tool_name not in scope.enabled_names and not tool_is_required(tool_name) and tool_name not in PROJECT_RUNTIME_TOOL_NAMES:
        from app.services.mcp_access import resolve_mcp_execution

        async with async_session() as access_db:
            candidates = await resolve_mcp_execution(access_db, agent_id, tool_name)
        if len(candidates) > 1:
            return render_message("mcpAccess.ambiguous")
        if not candidates or candidates[0][0].name not in scope.enabled_names:
            return render_message("sceneRuntime.toolDisabled")
        tool_name = candidates[0][0].name
    # Normalize only the legacy Agent-UUID sentinel at the shared tool boundary.
    # A genuine ``None`` remains anonymous/autonomous; durable background entry
    # points resolve a missing resource execution user to its creator earlier.
    try:
        canonical_agent_id = uuid.UUID(str(agent_id))
        canonical_user_id = uuid.UUID(str(user_id)) if user_id is not None else None
    except (TypeError, ValueError, AttributeError):
        canonical_agent_id = None
        canonical_user_id = None
    if canonical_agent_id is not None and canonical_user_id == canonical_agent_id:
        from app.models.agent import Agent as AgentModel

        async with async_session() as identity_db:
            creator_id = await identity_db.scalar(
                select(AgentModel.creator_id).where(AgentModel.id == canonical_agent_id)
            )
        if creator_id is not None:
            user_id = creator_id

    from app.core.plaza_feature import PLAZA_TOOL_NAMES

    if tool_name in PLAZA_TOOL_NAMES:
        logger.warning(
            "[Tools] Blocked globally disabled Plaza tool {} for agent {}",
            tool_name,
            agent_id,
        )
        return "This capability is unavailable."

    if is_retired_okr_tool(tool_name):
        return "This tool is unavailable."
    # Defensive guard: request_confirmation must be intercepted by the caller
    # loop before reaching execute_tool. If it somehow lands here, return a
    # clear signal instead of falling through to unknown-tool handling.
    if tool_name == REQUEST_CONFIRMATION_TOOL_NAME:
        return "⚠️ request_confirmation 由确认流程处理,不应到达工具执行层"

    if tool_name in {
        "run_subagent",
        "get_subagent_status",
        "send_message_to_subagent",
        "stop_subagent",
        "send_message_to_parent",
    }:
        from app.services.subagent_runtime import (
            SubagentError,
            append_subagent_message,
            create_subagent,
            get_subagent_status,
            run_subagent_sync,
            send_subagent_message_to_parent,
            stop_subagent,
        )

        try:
            if tool_name == "run_subagent":
                run, _created = await create_subagent(
                    agent_id=agent_id,
                    execution_user_id=user_id,
                    parent_session_id=session_id,
                    origin_tool_call_id=tool_call_id,
                    name=arguments.get("name"),
                    task=arguments.get("task"),
                    mode=arguments.get("mode", "sync"),
                    model=arguments.get("model"),
                    temperature=arguments.get("temperature"),
                    reasoning_effort=arguments.get("reasoning_effort"),
                    fork=bool(arguments.get("fork", False)),
                    soul=arguments.get("soul", True) is not False,
                    memory=arguments.get("memory", True) is not False,
                    turn_anchor_id=turn_anchor_id,
                )
                if on_progress is not None:
                    await on_progress({
                        "session_id": str(run.id),
                        "execution_agent_id": str(agent_id),
                    })
                if run.mode == "async":
                    return json.dumps(
                        {
                            "subagent_id": str(run.id),
                            "session_id": str(run.id),
                            "execution_agent_id": str(agent_id),
                            "status": run.status,
                            "mode": run.mode,
                            "model": run.model,
                            "model_id": str(run.model_id) if run.model_id else None,
                            "temperature": run.temperature,
                            "reasoning_effort": run.reasoning_effort,
                            "soul": run.soul,
                            "memory": run.memory,
                        },
                        ensure_ascii=False,
                    )
                status, reply, parent_messages = await run_subagent_sync(run.id)
                return json.dumps(
                    {
                        "subagent_id": str(run.id),
                        "session_id": str(run.id),
                        "execution_agent_id": str(agent_id),
                        "status": status,
                        "mode": run.mode,
                        "model": run.model,
                        "model_id": str(run.model_id) if run.model_id else None,
                        "temperature": run.temperature,
                        "reasoning_effort": run.reasoning_effort,
                        "soul": run.soul,
                        "memory": run.memory,
                        "result": reply,
                        "messages_to_parent": parent_messages,
                    },
                    ensure_ascii=False,
                )
            if tool_name == "send_message_to_subagent":
                status = await append_subagent_message(
                    agent_id=agent_id,
                    parent_session_id=session_id,
                    subagent_id=arguments.get("subagent_id"),
                    message=arguments.get("message"),
                    execution_user_id=user_id,
                    origin_tool_call_id=tool_call_id,
                )
                return json.dumps({"status": status}, ensure_ascii=False)
            if tool_name == "get_subagent_status":
                status = await get_subagent_status(
                    agent_id=agent_id,
                    parent_session_id=session_id,
                    subagent_id=arguments.get("subagent_id"),
                    execution_user_id=user_id,
                )
                return json.dumps(status, ensure_ascii=False)
            if tool_name == "stop_subagent":
                status = await stop_subagent(
                    agent_id=agent_id,
                    parent_session_id=session_id,
                    subagent_id=arguments.get("subagent_id"),
                    execution_user_id=user_id,
                    task_id=arguments.get("task_id"),
                )
                return json.dumps({"status": status}, ensure_ascii=False)
            await send_subagent_message_to_parent(
                agent_id=agent_id,
                execution_user_id=user_id,
                origin_tool_call_id=tool_call_id,
                subagent_session_id=session_id,
                message=arguments.get("message"),
            )
            return json.dumps({"status": "sent"}, ensure_ascii=False)
        except SubagentError as exc:
            return f"❌ {exc}"

    if tool_name in USER_PROJECT_TOOL_NAMES:
        try:
            return await execute_user_project_tool(
                tool_name,
                arguments,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                turn_anchor_id=turn_anchor_id,
            )
        except (ValueError, HTTPException) as exc:
            await record_user_project_tool_activity(
                agent_id=agent_id,
                tool_name=tool_name,
                outcome="rejected",
                user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
                tool_call_id=tool_call_id,
                project_id=arguments.get("project_id"),
            )
            return f"❌ {user_project_tool_error(exc)}"
        except (OSError, RuntimeError, SQLAlchemyError, TypeError):
            logger.exception("[UserProjectTool] {} failed", tool_name)
            await record_user_project_tool_activity(
                agent_id=agent_id,
                tool_name=tool_name,
                outcome="failed",
                user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
                tool_call_id=tool_call_id,
                project_id=arguments.get("project_id"),
            )
            return "❌ Project query failed."

    from app.services.project_runtime_tools import (
        PROJECT_SANDBOX_TOOL_NAMES,
        PROJECT_STANDARD_FILE_TOOL_NAMES,
        PROJECT_STRUCTURED_READ_TOOL_NAMES,
        PROJECT_RUNTIME_TOOL_NAMES,
        execute_project_workspace_tool,
        execute_project_runtime_tool,
        resolve_project_sandbox_scope,
    )

    project_sandbox_scope = None
    project_workspace = None
    if current_agent_runtime_workspace(agent_id).is_project and tool_name in (
        PROJECT_STANDARD_FILE_TOOL_NAMES
        | PROJECT_STRUCTURED_READ_TOOL_NAMES
        | PROJECT_SANDBOX_TOOL_NAMES
    ):
        project_workspace = str(arguments.get("workspace") or "").strip().casefold()
        if project_workspace not in {"agent", "project"}:
            return "❌ Project file tools require workspace='agent' or workspace='project'."

    if tool_name in PROJECT_RUNTIME_TOOL_NAMES:
        try:
            return await execute_project_runtime_tool(
                tool_name,
                arguments,
                agent_id=agent_id,
                execution_user_id=user_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                turn_anchor_id=turn_anchor_id,
            )
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return f"❌ {detail}"
        except Exception:
            logger.exception("[ProjectTool] {} failed", tool_name)
            return "❌ 项目操作未完成，请稍后重试。"

    _agent_tenant_id = await _get_agent_tenant_id(agent_id)

    ws = _agent_workspace_root(agent_id)

    # ── Autonomy boundary check (skipped when a human already approved, e.g. a
    #    confirmation card the user confirmed) ──
    action_type = _TOOL_AUTONOMY_MAP.get(tool_name)
    if action_type and (
        not skip_autonomy
        or (_FORCED_AUTONOMY_LEVELS.get(tool_name) == "L3" and not approved_by_human)
    ):
        try:
            from app.services.autonomy_service import autonomy_service
            from app.models.agent import Agent as AgentModel

            async with async_session() as _adb:
                _ar = await _adb.execute(select(AgentModel).where(AgentModel.id == agent_id))
                _agent = _ar.scalar_one_or_none()
                if _agent:
                    from app.utils.sanitize import sanitize_tool_args as _sanitize_tool_args

                    _sanitized_args = _sanitize_tool_args(arguments) or {}
                    approval_key = None
                    if _FORCED_AUTONOMY_LEVELS.get(tool_name) == "L3" and tool_call_id:
                        approval_key = ":".join(
                            [
                                "market-tool",
                                str(agent_id),
                                action_type,
                                str(session_id or "no-session"),
                                str(turn_anchor_id or "no-turn"),
                                str(tool_call_id),
                            ]
                        )
                    result_check = await autonomy_service.check_and_enforce(
                        _adb,
                        _agent,
                        action_type,
                        {
                            "tool": tool_name,
                            "args": _sanitized_args,
                            "requested_by": str(user_id),
                            "session_id": str(session_id or ""),
                            "turn_anchor_id": str(turn_anchor_id or ""),
                            "tool_call_id": str(tool_call_id or ""),
                        },
                        forced_level=_FORCED_AUTONOMY_LEVELS.get(tool_name),
                        idempotency_key=approval_key,
                    )
                    pending_im_notifications = result_check.pop("_pending_im_notifications", [])
                    await _adb.commit()
                    if pending_im_notifications:
                        try:
                            from app.services.autonomy_service import (
                                deliver_prepared_creator_notifications,
                            )

                            await deliver_prepared_creator_notifications(pending_im_notifications)
                        except Exception:
                            logger.exception("[Autonomy] committed creator notification delivery failed")
                    if not result_check.get("allowed"):
                        level = result_check.get("level", "L3")
                        logger.info(f"[Autonomy] Tool {tool_name} denied, level: {level}")
                        if level == "L3":
                            approval_status = result_check.get("approval_status")
                            if approval_status == "approved":
                                return "✅ This approved market action has already been executed."
                            if approval_status == "rejected":
                                return "❌ This market action was rejected and will not be executed."
                            return f"⏳ This action requires approval. An approval request has been sent. Please wait for approval before retrying. (Approval ID: {result_check.get('approval_id', 'N/A')})"
                        return f"❌ Action denied: {result_check.get('message', 'unknown reason')}"
        except Exception as e:
            logger.exception(f"[Autonomy] Check failed: {e}")
            return f"⚠️ Autonomy check failed ({e}). Operation blocked for safety. Please retry or contact admin."

    if project_workspace == "project" and tool_name in (
        PROJECT_STANDARD_FILE_TOOL_NAMES | PROJECT_STRUCTURED_READ_TOOL_NAMES
    ):
        if user_id is None:
            return "❌ Project file tools require an authorized execution user."
        try:
            return await execute_project_workspace_tool(
                tool_name,
                arguments,
                agent_id=agent_id,
                execution_user_id=user_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                turn_anchor_id=turn_anchor_id,
            )
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return f"❌ {detail}"
        except Exception:
            logger.exception("[ProjectWorkspaceTool] {} failed", tool_name)
            return "❌ 项目文件操作未完成，请稍后重试。"

    if project_workspace == "project" and tool_name in PROJECT_SANDBOX_TOOL_NAMES:
        if user_id is None:
            return "❌ Project sandbox requires an authorized execution user."
        try:
            project_sandbox_scope = await resolve_project_sandbox_scope(
                agent_id=agent_id,
                execution_user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
            )
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return f"❌ {detail}"
        except Exception:
            logger.exception("[ProjectSandbox] scope resolution failed")
            return "❌ 项目沙箱未能启动，请稍后重试。"

    # Tool-loop recovery can replay a completed tool call after a process crash.
    # Messaging providers do not all expose an idempotency header, so the durable
    # outbound receipt is the platform-owned replay boundary.  This stays entirely
    # internal: the tool schema and model-visible arguments remain unchanged.
    if tool_name in {
        "send_channel_message",
        "send_group_session_message",
        "send_session_message",
        "send_feishu_message",
        "send_platform_message",
        "send_message_to_agent",
        "send_channel_file",
    }:
        cached_outbound = await _find_outbound_tool_receipt(
            agent_id=agent_id,
            origin_session_id=session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
        if cached_outbound is not None:
            cached_meta = cached_outbound.message_meta if isinstance(cached_outbound.message_meta, dict) else {}
            # OpenClaw task delivery has two durable phases: the outbound row is
            # first recorded and its exact callback is armed, then the gateway
            # row and `queued` status commit together. A crash between those
            # phases must resume instead of pretending the target saw it.
            if not (tool_name == "send_message_to_agent" and cached_meta.get("delivery_status") == "recorded"):
                target = str(cached_meta.get("target_name") or "the recipient")
                channel = str(cached_meta.get("source_channel") or "the selected channel")
                delivery = cached_meta.get("delivery") if isinstance(cached_meta.get("delivery"), dict) else {}
                delivery_status = str(delivery.get("status") or cached_meta.get("delivery_status") or "sent")
                if delivery_status == "pending":
                    try:
                        updated_at = datetime.fromisoformat(str(delivery.get("updated_at") or ""))
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        updated_at = None
                    if updated_at and datetime.now(timezone.utc) - updated_at >= DELIVERY_LEASE:
                        await register_delivery(
                            cached_outbound.id,
                            IMDeliveryResult.unknown(channel, "delivery_lease_expired"),
                        )
                        return (
                            f"❌ The previous delivery to {target} via {channel} is unknown "
                            f"(message_id: {cached_outbound.id}). It was not retried to avoid duplication."
                        )
                    return (
                        f"⏳ A previous delivery to {target} via {channel} is still pending "
                        f"(message_id: {cached_outbound.id}). It will not be sent twice."
                    )
                if delivery_status in {"failed", "unknown"}:
                    return (
                        f"❌ The previous delivery to {target} via {channel} is {delivery_status} "
                        f"(message_id: {cached_outbound.id}). It was not retried to avoid duplication."
                    )
                if delivery_status == "partial":
                    return (
                        f"⚠️ The previous delivery to {target} via {channel} was only partially sent "
                        f"(message_id: {cached_outbound.id}). It was not retried to avoid duplication; "
                        "known delivered parts can still be recalled."
                    )
                return f"✅ Message already sent to {target} via {channel} (idempotent replay)."

    # Pre-inject session_id into arguments for AgentBay tools so each
    # _agentbay_* handler can pass it to get_agentbay_client_for_agent()
    # for per-ChatSession isolation of cloud instances.
    if tool_name.startswith("agentbay_"):
        arguments["_session_id"] = session_id

        # Take Control lock: block automatic tool execution while a human
        # is manually controlling the browser/desktop session. This prevents
        # input collisions between human clicks and agent-initiated actions.
        from app.api.agentbay_control import is_session_locked

        if is_session_locked(str(agent_id), session_id):
            return (
                "⏸️ A human operator is currently controlling this browser session "
                "(Take Control mode). Please wait for them to finish before retrying "
                "browser/computer operations."
            )

    return ExecuteToolDispatchContext(
        tool_name=tool_name,
        arguments=arguments,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        turn_anchor_id=turn_anchor_id,
        on_output=on_output,
        skip_autonomy=skip_autonomy,
        tools_for_llm=tools_for_llm,
        approved_by_human=approved_by_human,
        project_workspace=project_workspace,
        project_sandbox_scope=project_sandbox_scope,
        agent_tenant_id=_agent_tenant_id,
        ws=ws,
    )
