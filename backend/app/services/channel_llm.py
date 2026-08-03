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
    agent_id, session_id, *, content: str, sender_name: str | None = None, user_id=None
) -> None:
    """Mirror an inbound IM (channel) user message to web clients viewing the
    SAME session in real time. Without this, a person watching a DingTalk/Feishu
    conversation in the web UI sees the agent's reply stream (see
    ``_call_agent_llm``) but the channel user's OWN message would not appear
    until reload. Pass the clean persisted content + sender so the live bubble
    matches what a reload would render."""
    await _broadcast_to_web_session(
        agent_id,
        session_id,
        {
            "type": "channel_user_message",
            "content": content or "",
            "sender_name": sender_name,
            "user_id": str(user_id) if user_id is not None else None,
        },
    )


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
    from app.models.llm import LLMModel
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

    # Load primary model (skip if disabled by admin)
    model = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        model = model_result.scalar_one_or_none()
        if model and not model.enabled:
            logger.info(f"[Channel] Primary model {model.model} is disabled, skipping")
            model = None

    # Load fallback model (skip if disabled by admin)
    fallback_model = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()
        if fallback_model and not fallback_model.enabled:
            logger.info(f"[Channel] Fallback model {fallback_model.model} is disabled, skipping")
            fallback_model = None

    # Config-level fallback: primary missing -> use fallback
    if not model and fallback_model:
        model = fallback_model
        fallback_model = None
        logger.warning(f"[Channel] Primary model unavailable, using fallback: {model.model}")

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
        messages.append({"role": "user", "content": user_text})

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
                    rehydrate_images_max=3,
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
        await _broadcast_to_web_session(agent_id, session_id, payload)

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
        on_tool_call=_on_tool_call_persisted,
        supports_vision=getattr(model, "supports_vision", False),
        is_group=is_group,
        channel_context=scene_channel_context,
        turn_anchor_id=turn_anchor_id,
        context_recovery=context_recovery,
    )
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
