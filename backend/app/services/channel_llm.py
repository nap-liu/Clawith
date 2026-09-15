"""Shared, channel-agnostic LLM entry point for the external IM channels.

``_call_agent_llm`` is the single LLM call path used by every external IM
channel (Feishu / DingTalk / WeCom / Slack / Discord / Teams / WhatsApp /
WeChat) — the SAME failover-aware caller the WebSocket chat endpoint uses, so
every provider behaves identically on both surfaces. It centralizes tool-call
persistence and first-dispatch context recovery so all channels share one
canonical schema.

It lives here in ``services/`` (rather than in any one channel module) because
it is channel-agnostic: channels import it from here instead of reaching across
into ``app.api.feishu``. ``app.api.feishu`` re-exports it for backwards
compatibility, but new code should import from this module.
"""

import uuid
from contextlib import nullcontext
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_agent_expired
from app.database import async_session
from app.services.channel_dispatch import run_channel_reaction_hook
from app.services.channel_llm_broadcast import (
    _broadcast_to_web_session,
    _resolve_web_sender_profile as _resolve_web_sender_profile,
    broadcast_channel_user_message as broadcast_channel_user_message,
)

if TYPE_CHECKING:
    from app.models.chat_session import ChatSession
    from app.services.agent_runtime_workspace import AgentRuntimeWorkspace

# LLM-layer error sentinels surfaced verbatim to the IM user, followed by a
# recovery hint that guides them to reset the session with /new.
_LLM_ERROR_PREFIXES = ("[LLM Error]", "[LLM call error]", "[Error]")
_IM_LLM_RECOVERY_HINT = (
    "\n\n———\n如果反复出现此问题，请在当前聊天中单独发送一条消息：/new。"
    "请不要在同一条消息中添加其他文字或附件；重置成功后再重新发送需求和附件。"
)
_INDEPENDENT_PROGRESS_CHANNELS = frozenset(
    {
        "dingtalk",
        "slack",
        "wecom",
        "teams",
        "microsoft_teams",
        "whatsapp",
        "wechat",
        "discord",
    }
)


def _apply_recovery_hint(reply: str, hint: str | None) -> str:
    """Append a recovery hint to an LLM-error reply, if a hint is given.

    IM channels pass the default ``_IM_LLM_RECOVERY_HINT`` (guides the user to
    ``/new``). Non-IM callers (e.g. the MCP channel, which has no ``/new``) pass
    ``hint=None`` to opt out. Non-error replies are never touched.
    """
    if hint and reply and any(reply.startswith(p) for p in _LLM_ERROR_PREFIXES):
        return reply + hint
    return reply


def _normalize_history_messages(history: list[dict] | None) -> list[dict]:
    """Drop UI-only message roles before replaying history into the LLM."""
    if not history:
        return []
    allowed_roles = {"system", "assistant", "user", "tool", "function"}
    normalized: list[dict] = []
    for msg in history:
        role = msg.get("role")
        if role not in allowed_roles:
            continue
        normalized.append(msg)
    return normalized


async def _call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    *,
    session_id: str,
    user_id,
    history: list[dict] | None = None,
    on_chunk=None,
    on_thinking=None,
    on_tool_call=None,
    on_status=None,
    is_group: bool = False,
    recovery_hint: str | None = _IM_LLM_RECOVERY_HINT,
    continue_turn: bool = False,
    recovery_mode: bool = False,
    turn_anchor_id: uuid.UUID | None = None,
    turn_type: str | None = None,
    storage_agent_id: uuid.UUID | None = None,
    model_name: str | None = None,
    prepared_tools: list[dict] | None = None,
    before_round=None,
    before_tool_execution=None,
    broadcast_web: bool = True,
    web_broadcast_targets: list[tuple[uuid.UUID | str, str, dict]] | None = None,
    include_soul: bool = True,
    include_memory: bool = True,
    runtime_session: "ChatSession | None" = None,
    runtime_workspace: "AgentRuntimeWorkspace | None" = None,
    max_tool_rounds_override: int | None = None,
    model_override_id: str | uuid.UUID | None = None,
    temperature_override: float | None = None,
    reasoning_effort_override: str | None = None,
    prepared_turn_context: tuple[str, str] | None = None,
) -> str:
    """Call the agent's configured LLM model with conversation history.

    Reuses the same call_llm function as the WebSocket chat endpoint so that
    all providers (OpenRouter, Qwen, etc.) work identically on both channels.

    ``continue_turn``: resume the loop from existing history WITHOUT appending a new
    user message — used after a confirmation card is resolved, where ``history`` already
    ends with the request_confirmation assistant(tool_call)+tool(result) pair, so the
    model continues straight from the user's click (no synthetic user turn). ``user_text``
    is ignored in this mode.
    """
    from app.models.agent import Agent
    from app.services.chat_model_selection import (
        MODEL_OVERRIDE_NONE,
        MODEL_OVERRIDE_OK,
        load_turn_model_id,
        load_turn_reasoning_effort,
        resolve_runtime_models,
    )
    from app.services.llm import call_llm_with_failover
    from app.services.llm.failure_outcome import make_llm_failure
    from app.services.llm.session_context_guard import (
        CONTEXT_REQUEST_TOO_LARGE_MESSAGE,
        IM_SESSION_CONTEXT_TERMINATED_MESSAGE,
        SESSION_CONTEXT_TERMINATED_MESSAGE,
    )

    def _context_reply(reply: str) -> str:
        if recovery_hint and reply in {
            SESSION_CONTEXT_TERMINATED_MESSAGE,
            CONTEXT_REQUEST_TOO_LARGE_MESSAGE,
        }:
            return IM_SESSION_CONTEXT_TERMINATED_MESSAGE
        return reply

    # A2A sessions store their shared history under the session owner (the
    # stable min-id side), while the model and tools execute as the agent being
    # awakened.  Ordinary channels use the same id for both roles.
    history_agent_id = storage_agent_id or agent_id

    from app.services.scene_service import load_turn_scene_context

    scene_channel_context = await load_turn_scene_context(
        db,
        agent_id=agent_id,
        session_id=session_id,
        turn_anchor_id=turn_anchor_id,
    )

    # Load agent and model
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return make_llm_failure(code="agent_unavailable", message_key="errors.agentUnavailable")
    from app.core.okr_feature import is_retired_okr_agent

    if await is_retired_okr_agent(db, agent):
        return make_llm_failure(code="agent_unavailable", message_key="errors.agentUnavailable")

    if is_agent_expired(agent):
        return make_llm_failure(code="agent_expired", message_key="errors.agentExpired")

    if str(getattr(agent, "scope", "") or "").strip().lower() == "project":
        if runtime_workspace is None:
            raise RuntimeError("Project Agent runtime workspace was not bound before dispatch")
        if runtime_workspace.agent_id != agent.id or runtime_workspace.project_id != agent.project_id:
            raise RuntimeError("Project Agent runtime workspace does not match execution identity")

    if model_name and not model_override_id:
        from app.services.chat_model_selection import (
            MODEL_STATUS_OK,
            resolve_tenant_model_reference,
        )

        resolved_named = await resolve_tenant_model_reference(
            db,
            tenant_id=agent.tenant_id,
            reference=model_name,
        )
        if resolved_named.status != MODEL_STATUS_OK or resolved_named.model is None:
            return make_llm_failure(code="model_unavailable", message_key="errors.modelUnavailable")
        model_override_id = resolved_named.model.id

    turn_model_id = await load_turn_model_id(
        db,
        agent_id=agent_id,
        session_id=session_id,
        turn_anchor_id=turn_anchor_id,
    )
    turn_reasoning_effort = await load_turn_reasoning_effort(
        db,
        agent_id=agent_id,
        session_id=session_id,
        turn_anchor_id=turn_anchor_id,
    )
    from app.models.chat_session import ChatSession

    if runtime_session is None:
        try:
            runtime_session = await db.get(ChatSession, uuid.UUID(str(session_id)))
        except (TypeError, ValueError):
            runtime_session = None
    runtime_config = dict(runtime_session.im_config or {}) if runtime_session is not None else {}
    effective_override_id = model_override_id or turn_model_id
    effective_reasoning_effort = (
        reasoning_effort_override
        if reasoning_effort_override is not None
        else turn_reasoning_effort
    )
    if effective_override_id:
        resolved_models = await resolve_runtime_models(
            db,
            agent=agent,
            override_model_id=effective_override_id,
            override_temperature=temperature_override,
            override_reasoning_effort=effective_reasoning_effort,
        )
    elif (
        runtime_session is not None
        and runtime_session.source_channel == "subagent"
        and runtime_session.project_id is not None
        and isinstance(runtime_config.get("member_config_snapshot"), dict)
    ):
        from app.models.project import Project
        from app.services.chat_model_selection import resolve_project_member_runtime_models

        project = await db.get(Project, runtime_session.project_id)
        resolved_models = await resolve_project_member_runtime_models(
            db,
            agent=agent,
            member_config=runtime_config.get("member_config_snapshot"),
            project_settings=project.settings if project is not None else {},
            override_temperature=temperature_override,
            override_reasoning_effort=effective_reasoning_effort,
        )
    else:
        # Compatibility for project child inputs created before per-turn model
        # snapshots were introduced. Keep retries in the unified channel path.
        from app.models.project import Project
        from app.services.chat_model_selection import resolve_project_runtime_models

        if (
            runtime_session is not None
            and runtime_session.source_channel == "subagent"
            and runtime_session.project_id is not None
        ):
            project = await db.get(Project, runtime_session.project_id)
            resolved_models = await resolve_project_runtime_models(
                db,
                agent=agent,
                project_settings=project.settings if project is not None else {},
                override_temperature=temperature_override,
                override_reasoning_effort=effective_reasoning_effort,
            )
        else:
            resolved_models = await resolve_runtime_models(
                db,
                agent=agent,
                override_temperature=temperature_override,
                override_reasoning_effort=effective_reasoning_effort,
            )
    if effective_override_id and resolved_models.override_status not in {MODEL_OVERRIDE_NONE, MODEL_OVERRIDE_OK}:
        if model_override_id:
            return make_llm_failure(code="model_unavailable", message_key="errors.modelUnavailable")
        return make_llm_failure(code="model_unavailable", message_key="errors.sessionModelUnavailable")
    model = resolved_models.primary_model
    fallback_model = resolved_models.fallback_model

    if not model:
        return make_llm_failure(code="model_unavailable", message_key="errors.modelNotConfigured")

    if runtime_session is None and session_id:
        from app.models.chat_session import ChatSession

        try:
            runtime_session = await db.get(ChatSession, uuid.UUID(str(session_id)))
        except (AttributeError, TypeError, ValueError):
            runtime_session = None

    turn_snapshot = None
    if turn_anchor_id is not None and runtime_session is not None:
        from app.services.conversation_turn_lifecycle import (
            publish_conversation_turn_event,
            transition_conversation_turn,
        )

        async with async_session() as lifecycle_db:
            turn_snapshot = await transition_conversation_turn(
                lifecycle_db,
                agent_id=history_agent_id,
                conversation_id=str(session_id),
                turn_anchor_id=turn_anchor_id,
                status="running",
            )
            await lifecycle_db.commit()
        await publish_conversation_turn_event(
            agent_id=history_agent_id,
            conversation_id=str(session_id),
            payload={"type": "turn_state"},
            snapshot=turn_snapshot,
            event_kind="turn_lifecycle",
        )

    # Build conversation messages (without system prompt — call_llm adds it)
    messages: list[dict] = []
    from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE

    ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
    if history:
        # Expanded tool_call rows are assistant(tool_calls)+tool(result) pairs;
        # the ctx_size slice can cut a pair, so drop any leading orphan tool
        # message (same guard the WebSocket path applies after its slice).
        from app.services.chat_history import strip_leading_orphan_tool_messages

        normalized_history = _normalize_history_messages(history)
        messages.extend(strip_leading_orphan_tool_messages(normalized_history))
    if not continue_turn:
        current_message: dict = {"role": "user", "content": user_text}
        get_row = getattr(db, "get", None)
        if turn_anchor_id is not None and callable(get_row):
            from app.models.audit import ChatMessage
            from app.services.chat_attachments import (
                normalize_attachment_metadata,
                normalize_chat_message_attachments,
                strip_image_data_markers,
            )

            anchor = await get_row(ChatMessage, turn_anchor_id)
            if anchor is not None and anchor.agent_id == history_agent_id and anchor.conversation_id == str(session_id):
                meta = anchor.message_meta if isinstance(anchor.message_meta, dict) else {}
                if "attachments" in meta:
                    # Keep sender attribution and extracted document text from
                    # the live turn; the persisted display text is UI-only.
                    content = strip_image_data_markers(user_text)
                    attachments = normalize_attachment_metadata(meta.get("attachments"))
                else:
                    content, attachments = normalize_chat_message_attachments(
                        user_text,
                        meta,
                        meta.get("source_channel"),
                    )
                current_message["content"] = content
                if attachments:
                    current_message["attachments"] = attachments
        messages.append(current_message)

    # Exact first-dispatch recovery. Only a fresh, durably anchored user turn
    # is eligible; confirmation/startup continuations retain their in-memory
    # transcript and fail safely rather than risking a replay.
    context_recovery = None
    if session_id and turn_anchor_id is not None and not continue_turn and not recovery_mode:
        from app.services.llm.turn_partition import effective_keep_recent_turns

        protected_keep_recent_turns = effective_keep_recent_turns(model, fallback_model)

        async def _recover_context(_recovery_model, dispatch_budget):
            from app.services.chat_history import load_recoverable_history_for_turn
            from app.services.llm.compactor import (
                COMPACTION_NOT_APPLICABLE_REASONS,
                ContextRecoveryMessages,
                maybe_compact,
            )

            compacted = await maybe_compact(
                agent_id=history_agent_id,
                conversation_id=session_id,
                model=_recovery_model,
                last_prompt_tokens=getattr(dispatch_budget, "authoritative_prompt_tokens", None),
                current_anchor_id=turn_anchor_id,
                force_required=getattr(dispatch_budget, "provider_overflow", False),
                keep_recent_turns_override=(
                    getattr(dispatch_budget, "keep_recent_turns_override", None)
                    if getattr(dispatch_budget, "provider_overflow", False)
                    else protected_keep_recent_turns
                ),
            )
            preflight_not_applicable = (
                not compacted.triggered
                and compacted.skipped_reason in COMPACTION_NOT_APPLICABLE_REASONS
                and dispatch_budget.fits
            )
            if not compacted.triggered and not preflight_not_applicable:
                logger.warning(
                    f"[Channel] context recovery could not compact session={session_id}: {compacted.skipped_reason}"
                )
                return None
            async with async_session() as recovery_db:
                recovered = await load_recoverable_history_for_turn(
                    recovery_db,
                    agent_id=history_agent_id,
                    conversation_id=session_id,
                    turn_anchor_id=turn_anchor_id,
                    ctx_size=ctx_size,
                    is_group=is_group,
                )
            if not recovered:
                logger.warning(f"[Channel] context recovery lost latest-anchor race session={session_id}")
                return None
            return ContextRecoveryMessages(
                _normalize_history_messages(recovered),
                preflight_not_applicable=preflight_not_applicable,
            )

        context_recovery = _recover_context

    # Use actual user_id so the system prompt knows who it's chatting with
    effective_user_id = user_id

    # Ordinary parent Sessions share the same durable Subagent-event inbox as
    # Web Chat. A child Session keeps its existing, child-specific before_round
    # hook; project Sessions retain their dedicated group/A2A batching policy.
    if runtime_session is None and session_id:
        from app.models.chat_session import ChatSession

        try:
            runtime_session = await db.get(ChatSession, uuid.UUID(str(session_id)))
        except (TypeError, ValueError):
            runtime_session = None
    try:
        parent_event_execution_user_id = (
            uuid.UUID(str(effective_user_id)) if effective_user_id is not None else None
        )
    except (TypeError, ValueError):
        parent_event_execution_user_id = None
    if (
        turn_anchor_id is not None
        and runtime_session is not None
        and runtime_session.source_channel != "subagent"
        and runtime_session.project_id is None
        and parent_event_execution_user_id is not None
    ):
        from app.services.subagent_runtime import build_parent_subagent_before_round
        from app.services.turn_inbox import is_turn_inbox_channel

        before_round = build_parent_subagent_before_round(
            parent_session_id=session_id,
            active_turn_anchor_id=turn_anchor_id,
            execution_agent_id=agent_id,
            execution_user_id=parent_event_execution_user_id,
            turn_anchor_agent_id=history_agent_id,
            include_turn_inbox=is_turn_inbox_channel(
                runtime_session.source_channel
            ),
            upstream=before_round,
        )

    # Centralized tool-call persistence: wrap the channel callback so EVERY IM
    # channel stores completed tool calls with one canonical schema (shared
    # persist_tool_call) — making them visible in the web UI and replayable in
    # cross-turn LLM history, exactly like the WebSocket path. The channel's own
    # callback (if any) is kept purely for live side effects (e.g. Feishu
    # progress nudges) and must never persist.
    from app.database import async_session as _persist_session_factory
    from app.services.chat_history import persist_tool_call as _persist_tool_call

    delivered_progress_texts: set[str] = set()
    delivered_progress_rounds: set[str] = set()

    async def _deliver_tool_round_progress(evt: dict, message_id: str | None) -> None:
        if runtime_session is None or evt.get("status") != "running":
            return
        from app.services.im_thinking_output import resolve_im_progress_enabled

        if not resolve_im_progress_enabled(agent, runtime_session):
            return
        channel = str(runtime_session.source_channel or "").lower()
        if channel not in _INDEPENDENT_PROGRESS_CHANNELS:
            return
        from app.services.user_output import sanitize_user_visible_text

        progress = sanitize_user_visible_text(str(evt.get("assistant_content") or "")).strip()
        round_id = str(evt.get("round_id") or "")
        if (
            not message_id
            or not progress
            or progress in delivered_progress_texts
            or (round_id and round_id in delivered_progress_rounds)
        ):
            return
        # Mark before the provider wait. A timeout or uncertain receipt must not
        # cause the same model round to fan out multiple visible messages.
        delivered_progress_texts.add(progress)
        if round_id:
            delivered_progress_rounds.add(round_id)
        try:
            from app.services.im_delivery import (
                IMDeliveryResult,
                deliver_persisted_message,
                register_delivery,
            )
            from app.services.turn_runtime import TurnRuntime

            if not await register_delivery(message_id, IMDeliveryResult.pending(channel)):
                raise RuntimeError("tool_round_progress_anchor_not_found")
            await deliver_persisted_message(
                message_id=message_id,
                agent_id=history_agent_id,
                runtime=TurnRuntime(
                    session_found=True,
                    source_channel=channel,
                    conversation_id=str(session_id),
                    external_conv_id=runtime_session.external_conv_id,
                    is_group=bool(runtime_session.is_group),
                ),
                message=progress,
                receipt_recallable=False,
            )
        except Exception as exc:  # noqa: BLE001 - progress must not fail the tool turn
            logger.warning(
                "[Channel] independent assistant progress delivery failed: "
                f"channel={channel} error={type(exc).__name__}"
            )

    # Mirror this turn to any web client viewing the SAME session in real time.
    # IM apps render only the final reply, but a person watching the conversation
    # in the web UI expects the live stream — so broadcast every event to the
    # session's live web connections (the "connection = subscriber" model, same
    # as the WebSocket chat path). Lazy import avoids a services->api import
    # cycle; best-effort so IM delivery is never affected by a web-side hiccup.
    async def _web_broadcast(payload: dict):
        if not broadcast_web:
            return
        from app.services.conversation_turn_lifecycle import with_turn_envelope

        await _broadcast_to_web_session(
            history_agent_id,
            session_id,
            with_turn_envelope(
                payload,
                turn_snapshot,
                event_kind=("turn_tool" if payload.get("type") == "tool_call" else "turn_stream"),
            ),
        )
        for target_agent_id, target_session_id, target_context in web_broadcast_targets or []:
            event_kind = (
                "turn_tool"
                if payload.get("type") == "tool_call"
                else "turn_stream"
            )
            await _broadcast_to_web_session(
                target_agent_id,
                target_session_id,
                {**payload, **target_context, "event_kind": event_kind},
            )

    from app.services.user_output import (
        UserOutputStreamSanitizer,
        sanitize_user_visible_text,
    )

    chunk_guard = UserOutputStreamSanitizer()
    thinking_guard = UserOutputStreamSanitizer()

    async def _emit_chunk(text: str) -> None:
        if not text:
            return
        await _web_broadcast({"type": "chunk", "content": text})
        await run_channel_reaction_hook(
            on_chunk,
            text,
            hook_name="on_chunk",
        )

    async def _emit_thinking(text: str) -> None:
        if not text:
            return
        await _web_broadcast({"type": "thinking", "content": text})
        await run_channel_reaction_hook(
            on_thinking,
            text,
            hook_name="on_thinking",
        )

    async def _on_chunk_bridged(text: str):
        await _emit_chunk(chunk_guard.feed(text))

    async def _on_thinking_bridged(text: str):
        await _emit_thinking(thinking_guard.feed(text))

    async def _on_status_bridged(status: dict):
        from app.services.llm.failure_outcome import render_message
        message_key = str(status.get("message_key") or "")
        content = render_message(message_key, "zh").format(**status) if message_key else ""
        if content:
            await _web_broadcast({"type": "info", "content": content})
        await run_channel_reaction_hook(
            on_status,
            {**status, "content": content},
            hook_name="on_status",
        )

    async def _on_tool_call_persisted(evt: dict):
        durable_message_id = str(evt.get("_durable_message_id") or "") or None
        public_evt = {k: v for k, v in evt.items() if not k.startswith("_")}
        # Persist only when we have a real session + user (FK-safe). IM channels
        # always pass both; guard keeps stray callers from writing orphan rows.
        if session_id and user_id is not None and not evt.get("_durable_persisted"):
            persisted_id = await _persist_tool_call(
                _persist_session_factory,
                agent_id=history_agent_id,
                user_id=user_id,
                conversation_id=session_id,
                evt=public_evt,
                turn_anchor_id=turn_anchor_id,
            )
            if persisted_id is not None:
                durable_message_id = str(persisted_id)
        await _deliver_tool_round_progress(public_evt, durable_message_id)
        # Mirror to web viewers, masking secrets at the output boundary exactly
        # like the WebSocket path (raw args stay in the persisted row for replay).
        from app.utils.sanitize import sanitize_tool_args
        from app.services.tool_result_display import tool_event_for_display

        _evt = (
            {**public_evt, "args": sanitize_tool_args(public_evt.get("args"))} if "args" in public_evt else public_evt
        )
        await _web_broadcast({"type": "tool_call", **tool_event_for_display({
            **_evt, "_durable_message_id": durable_message_id,
        })})
        await run_channel_reaction_hook(
            on_tool_call,
            public_evt,
            hook_name="on_tool_call",
        )

    # Reuse the unified, failover-aware caller — the SAME path as the WebSocket
    # chat endpoint, so every provider behaves identically on both surfaces.
    #
    # IMPORTANT: do NOT wrap this in an outer ``asyncio.wait_for``. That would cap
    # the ENTIRE multi-round tool-calling loop with one budget and kill long-but-
    # healthy conversations — the root cause of the channel-wide
    # "Model response timed out (>180s)" errors. Per-request timeouts already live
    # inside call_llm (the httpx client timeout), and the loop is bounded by the
    # agent's ``max_tool_rounds``.
    from app.services.session_token_usage import persist_turn_token_usage
    from app.services.token_tracker import TokenUsage

    turn_usage = TokenUsage()

    async def _collect_usage(usage: TokenUsage) -> None:
        turn_usage.add(usage)

    # Everything needed by the provider/tool loop is now materialized. End the
    # ingress/read transaction before remote model I/O so every caller shares
    # the same short transaction boundary and no channel can accidentally pin
    # locks or a pool connection for the full tool loop. ``expire_on_commit`` is
    # disabled by the shared session factory, so loaded runtime objects remain
    # usable and callers may reuse this session for a later short transaction.
    if db.in_transaction():
        await db.commit()

    try:
        runtime_binding = nullcontext()
        if runtime_workspace is not None:
            from app.services.agent_runtime_workspace import (
                bind_agent_runtime_workspace,
            )

            runtime_binding = bind_agent_runtime_workspace(runtime_workspace)
        with runtime_binding:
            reply = await call_llm_with_failover(
                primary_model=model,
                fallback_model=fallback_model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=agent_id,
                user_id=effective_user_id,
                session_id=session_id,
                on_chunk=_on_chunk_bridged,
                on_thinking=_on_thinking_bridged,
                on_status=_on_status_bridged,
                on_usage=_collect_usage,
                on_tool_call=_on_tool_call_persisted,
                is_group=is_group,
                channel_context=scene_channel_context,
                turn_anchor_id=turn_anchor_id,
                turn_anchor_agent_id=history_agent_id,
                turn_type=turn_type,
                context_recovery=context_recovery,
                prepared_tools=prepared_tools,
                prepared_turn_context=prepared_turn_context,
                before_round=before_round,
                before_tool_execution=before_tool_execution,
                include_soul=include_soul,
                include_memory=include_memory,
                max_tool_rounds_override=max_tool_rounds_override,
            )
    finally:
        try:
            await persist_turn_token_usage(
                agent_id=history_agent_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
                usage=turn_usage,
            )
        except Exception as exc:
            logger.warning(f"[Channel] session token usage persistence failed: {exc}")
    await _emit_chunk(chunk_guard.flush())
    await _emit_thinking(thinking_guard.flush())
    from app.services.llm.failure_outcome import LLMFailure, localize_llm_failure

    contextual_reply = _context_reply(reply)
    if isinstance(contextual_reply, LLMFailure):
        reply = localize_llm_failure(contextual_reply, "zh")
    else:
        reply = sanitize_user_visible_text(contextual_reply)

    # IM channels render this reply directly to the end user. Keep the original
    # error sentinel visible (it carries the concrete failure reason) and append
    # a short recovery hint that guides the user to reset the session with /new.
    if isinstance(reply, LLMFailure):
        logger.error(
            f"[Channel] LLM failure surfaced on channel "
            f"(agent_id={agent_id}, code={reply.code}, "
            f"model={getattr(model, 'model', 'unknown')})"
        )
        return reply
    if reply and any(reply.startswith(p) for p in _LLM_ERROR_PREFIXES):
        logger.error(
            f"[Channel] LLM error surfaced on IM channel "
            f"(agent_id={agent_id}, model={getattr(model, 'model', 'unknown')}): {reply[:200]}"
        )
    return sanitize_user_visible_text(_apply_recovery_hint(reply, recovery_hint))
