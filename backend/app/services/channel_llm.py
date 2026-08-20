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

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_agent_expired
from app.database import async_session

# LLM-layer error sentinels surfaced verbatim to the IM user, followed by a
# recovery hint that guides them to reset the session with /new.
_LLM_ERROR_PREFIXES = ("[LLM Error]", "[LLM call error]", "[Error]")
_IM_LLM_RECOVERY_HINT = (
    "\n\n———\n如果反复出现此问题，请在当前聊天中单独发送一条消息：/new。"
    "请不要在同一条消息中添加其他文字或附件；重置成功后再重新发送需求和附件。"
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


async def _broadcast_to_web_session(agent_id, session_id, payload: dict) -> None:
    """Best-effort mirror of one event to every web client viewing this session.

    The web ConnectionManager is a single-process in-memory registry shared by
    the whole backend (uvicorn runs one process), so this reaches the WS chat
    connections registered under the same session_id. Lazy import avoids a
    services->api import cycle; any failure must never affect channel delivery.
    """
    if not session_id:
        return
    try:
        from app.api.websocket import manager as _ws_manager

        await _ws_manager.send_to_session(str(agent_id), str(session_id), payload)
    except Exception:
        pass


async def broadcast_channel_user_message(
    agent_id,
    session_id,
    *,
    message,
    sender_name: str | None = None,
    user_id=None,
) -> None:
    """Mirror an inbound IM (channel) user message to web clients viewing the
    SAME session in real time. Without this, a person watching a DingTalk/Feishu
    conversation in the web UI sees the agent's reply stream (see
    ``_call_agent_llm``) but the channel user's OWN message would not appear
    until reload. Serialize the persisted message through the same contract as
    history so the live bubble matches what a reload would render."""
    from app.services.chat_message_serializer import serialize_chat_message_for_client

    payload = serialize_chat_message_for_client(
        message,
        sender_name=sender_name,
        sender_user_id=user_id,
    )
    payload["type"] = "channel_user_message"
    # One-release compatibility for older Web clients.
    payload["user_id"] = str(user_id) if user_id is not None else None
    await _broadcast_to_web_session(agent_id, session_id, payload)


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
    is_group: bool = False,
    recovery_hint: str | None = _IM_LLM_RECOVERY_HINT,
    continue_turn: bool = False,
    recovery_mode: bool = False,
    turn_anchor_id: uuid.UUID | None = None,
    storage_agent_id: uuid.UUID | None = None,
    model_name: str | None = None,
    prepared_tools: list[dict] | None = None,
    before_round=None,
    before_tool_execution=None,
    broadcast_web: bool = True,
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
        resolve_runtime_models,
    )
    from app.services.llm import call_llm_with_failover
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
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    if model_name:
        from app.services.chat_model_selection import (
            MODEL_STATUS_OK,
            resolve_tenant_model_by_name,
        )
        from app.services.llm.runtime_model import RuntimeLLMModel

        resolved_named = await resolve_tenant_model_by_name(
            db,
            tenant_id=agent.tenant_id,
            model_name=model_name,
        )
        if resolved_named.status != MODEL_STATUS_OK or resolved_named.model is None:
            return f"⚠️ Subagent 指定模型 {model_name} 已不可用"
        model = RuntimeLLMModel.from_orm(resolved_named.model)
        fallback_model = None
    else:
        turn_model_id = await load_turn_model_id(
            db,
            agent_id=agent_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        if turn_model_id:
            resolved_models = await resolve_runtime_models(
                db,
                agent=agent,
                override_model_id=turn_model_id,
            )
        else:
            # Compatibility for project child inputs created before per-turn
            # model snapshots were introduced.  Keep the fallback inside the
            # unified channel path so retries and compaction use the same model.
            from app.models.chat_session import ChatSession
            from app.models.project import Project
            from app.services.chat_model_selection import resolve_project_runtime_models

            try:
                runtime_session = await db.get(ChatSession, uuid.UUID(str(session_id)))
            except (TypeError, ValueError):
                runtime_session = None
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
                )
            else:
                resolved_models = await resolve_runtime_models(db, agent=agent)
        if (
            turn_model_id
            and resolved_models.override_status not in {MODEL_OVERRIDE_NONE, MODEL_OVERRIDE_OK}
        ):
            return (
                "⚠️ 当前会话选择的模型已不可用，请发送 /model list 重新选择，"
                "或 /model default 恢复默认模型。"
            )
        model = resolved_models.primary_model
        fallback_model = resolved_models.fallback_model

    if not model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

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
            if (
                anchor is not None
                and anchor.agent_id == history_agent_id
                and anchor.conversation_id == str(session_id)
            ):
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

        frozen_current_suffix = [dict(messages[-1])]
        protected_keep_recent_turns = effective_keep_recent_turns(model, fallback_model)

        async def _recover_context(_recovery_model, dispatch_budget):
            from app.services.chat_history import load_history_prefix_before_anchor
            from app.services.llm.compactor import maybe_compact

            compacted = await maybe_compact(
                agent_id=history_agent_id,
                conversation_id=session_id,
                model=_recovery_model,
                pre_flight_estimate=dispatch_budget.estimated_tokens,
                current_anchor_id=turn_anchor_id,
                force_required=dispatch_budget.char_overflow,
                keep_recent_turns_override=protected_keep_recent_turns,
            )
            if not compacted.triggered:
                logger.warning(
                    "[Channel] context recovery could not compact session="
                    f"{session_id}: {compacted.skipped_reason}"
                )
                return None
            async with async_session() as recovery_db:
                prefix = await load_history_prefix_before_anchor(
                    recovery_db,
                    agent_id=history_agent_id,
                    conversation_id=session_id,
                    turn_anchor_id=turn_anchor_id,
                    ctx_size=ctx_size,
                    is_group=is_group,
                )
            if prefix is None:
                logger.warning(
                    f"[Channel] context recovery lost latest-anchor race session={session_id}"
                )
                return None
            return _normalize_history_messages(prefix) + frozen_current_suffix

        context_recovery = _recover_context

    # Use actual user_id so the system prompt knows who it's chatting with
    effective_user_id = user_id or agent_id

    # Centralized tool-call persistence: wrap the channel callback so EVERY IM
    # channel stores completed tool calls with one canonical schema (shared
    # persist_tool_call) — making them visible in the web UI and replayable in
    # cross-turn LLM history, exactly like the WebSocket path. The channel's own
    # callback (if any) is kept purely for live side effects (e.g. Feishu
    # progress nudges) and must never persist.
    from app.database import async_session as _persist_session_factory
    from app.services.chat_history import persist_tool_call as _persist_tool_call

    # Mirror this turn to any web client viewing the SAME session in real time.
    # IM apps render only the final reply, but a person watching the conversation
    # in the web UI expects the live stream — so broadcast every event to the
    # session's live web connections (the "connection = subscriber" model, same
    # as the WebSocket chat path). Lazy import avoids a services->api import
    # cycle; best-effort so IM delivery is never affected by a web-side hiccup.
    async def _web_broadcast(payload: dict):
        if broadcast_web:
            await _broadcast_to_web_session(history_agent_id, session_id, payload)

    async def _on_chunk_bridged(text: str):
        await _web_broadcast({"type": "chunk", "content": text})
        if on_chunk is not None:
            await on_chunk(text)

    async def _on_thinking_bridged(text: str):
        await _web_broadcast({"type": "thinking", "content": text})
        if on_thinking is not None:
            await on_thinking(text)

    async def _on_tool_call_persisted(evt: dict):
        public_evt = {k: v for k, v in evt.items() if not k.startswith("_")}
        # Persist only when we have a real session + user (FK-safe). IM channels
        # always pass both; guard keeps stray callers from writing orphan rows.
        if session_id and user_id is not None and not evt.get("_durable_persisted"):
            await _persist_tool_call(
                _persist_session_factory,
                agent_id=history_agent_id,
                user_id=user_id,
                conversation_id=session_id,
                evt=public_evt,
                turn_anchor_id=turn_anchor_id,
            )
        # Mirror to web viewers, masking secrets at the output boundary exactly
        # like the WebSocket path (raw args stay in the persisted row for replay).
        from app.utils.sanitize import sanitize_tool_args

        _evt = (
            {**public_evt, "args": sanitize_tool_args(public_evt.get("args"))}
            if "args" in public_evt
            else public_evt
        )
        await _web_broadcast({"type": "tool_call", **_evt})
        if on_tool_call is not None:
            await on_tool_call(public_evt)

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

    try:
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
            on_usage=_collect_usage,
            on_tool_call=_on_tool_call_persisted,
            is_group=is_group,
            channel_context=scene_channel_context,
            turn_anchor_id=turn_anchor_id,
            context_recovery=context_recovery,
            prepared_tools=prepared_tools,
            before_round=before_round,
            before_tool_execution=before_tool_execution,
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
    reply = _context_reply(reply)

    # Finalize the streamed bubble for any web client watching this session, so
    # an IM-driven conversation updates live in the web UI (not only on reload).
    await _web_broadcast({"type": "done", "role": "assistant", "content": reply})

    # IM channels render this reply directly to the end user. Keep the original
    # error sentinel visible (it carries the concrete failure reason) and append
    # a short recovery hint that guides the user to reset the session with /new.
    if reply and any(reply.startswith(p) for p in _LLM_ERROR_PREFIXES):
        logger.error(
            f"[Channel] LLM error surfaced on IM channel "
            f"(agent_id={agent_id}, model={getattr(model, 'model', 'unknown')}): {reply[:200]}"
        )
    return _apply_recovery_hint(reply, recovery_hint)
