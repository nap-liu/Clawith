"""Shared, channel-agnostic LLM entry point for the external IM channels.

``_call_agent_llm`` is the single LLM call path used by every external IM
channel (Feishu / DingTalk / WeCom / Slack / Discord / Teams / WhatsApp /
WeChat) — the SAME failover-aware caller the WebSocket chat endpoint uses, so
every provider behaves identically on both surfaces. It centralizes tool-call
persistence and pre-flight compaction so all channels share one canonical
schema.

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

# LLM-layer error sentinels surfaced verbatim to the IM user, followed by a
# recovery hint that guides them to reset the session with /new.
_LLM_ERROR_PREFIXES = ("[LLM Error]", "[LLM call error]", "[Error]")
_IM_LLM_RECOVERY_HINT = "\n\n———\n如果反复出现此问题，请发送 /new 开启新对话后重试。"


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
) -> str:
    """Call the agent's configured LLM model with conversation history.

    Reuses the same call_llm function as the WebSocket chat endpoint so that
    all providers (OpenRouter, Qwen, etc.) work identically on both channels.
    """
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.services.llm import call_llm_with_failover

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

        messages.extend(strip_leading_orphan_tool_messages(_normalize_history_messages(history)[-ctx_size:]))
    messages.append({"role": "user", "content": user_text})

    # Pre-flight compaction: if the about-to-be-sent prompt is near the model
    # window, compact NOW and reload so THIS request doesn't overflow (the
    # post-round hook only slims the NEXT turn). The reload already includes the
    # just-saved current user message — channels save it before calling — so we
    # rebuild from the fresh history WITHOUT re-appending user_text. Best-effort:
    # any failure leaves the original messages untouched.
    if session_id:
        from app.services.chat_history import load_history_for_llm, strip_leading_orphan_tool_messages
        from app.services.llm.compactor import maybe_precompact_prompt

        try:
            if await maybe_precompact_prompt(
                agent_id=agent_id, conversation_id=session_id, model=model, prompt_messages=messages
            ):
                fresh = await load_history_for_llm(
                    db,
                    agent_id=agent_id,
                    conversation_id=session_id,
                    ctx_size=ctx_size,
                    is_group=is_group,
                    rehydrate_images_max=3,
                )
                rebuilt = strip_leading_orphan_tool_messages(_normalize_history_messages(fresh)[-ctx_size:])
                # The reload ends with the current user message as stored in DB
                # (raw text). Restore the per-turn-augmented user_text that was on
                # the original prompt — sender wrap and the file-upload hint live
                # only in user_text, not in the persisted row.
                if rebuilt and rebuilt[-1].get("role") == "user":
                    rebuilt[-1] = {"role": "user", "content": user_text}
                else:
                    rebuilt.append({"role": "user", "content": user_text})
                messages = rebuilt
        except Exception as _pf_exc:
            logger.warning(f"[Channel] pre-flight compaction skipped (non-fatal): {_pf_exc}")

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

    async def _on_tool_call_persisted(evt: dict):
        # Persist only when we have a real session + user (FK-safe). IM channels
        # always pass both; guard keeps stray callers from writing orphan rows.
        if session_id and user_id is not None:
            await _persist_tool_call(
                _persist_session_factory,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=session_id,
                evt=evt,
            )
        if on_tool_call is not None:
            await on_tool_call(evt)

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
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_tool_call=_on_tool_call_persisted,
        supports_vision=getattr(model, "supports_vision", False),
        is_group=is_group,
    )

    # IM channels render this reply directly to the end user. Keep the original
    # error sentinel visible (it carries the concrete failure reason) and append
    # a short recovery hint that guides the user to reset the session with /new.
    if reply and any(reply.startswith(p) for p in _LLM_ERROR_PREFIXES):
        logger.error(
            f"[Channel] LLM error surfaced on IM channel "
            f"(agent_id={agent_id}, model={getattr(model, 'model', 'unknown')}): {reply[:200]}"
        )
        return reply + _IM_LLM_RECOVERY_HINT
    return reply
