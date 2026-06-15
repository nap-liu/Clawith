"""Unified LLM calling service with failover support for all execution paths.

This module provides a shared entry point for all LLM calls across:
- WebSocket chat
- IM channels (Feishu, Slack, Teams, Discord, WeCom, DingTalk)
- Background services (task executor, scheduler, heartbeat, etc.)

All paths now support:
1. Config-level fallback: if primary missing, use fallback directly
2. Runtime failover: if primary fails with retryable error, try fallback once
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.services.agent_tools import AGENT_TOOLS, execute_tool, get_agent_tools_for_llm
from app.services.token_tracker import (
    TokenUsage,
    record_token_usage,
    extract_token_usage,
    estimate_token_usage_from_chars,
)

from .client import LLMError
from .failover import classify_error, FailoverErrorType
from .json_recovery import canonicalize_tool_arguments
from .tool_output_store import enforce_message_budget, finalize_tool_output
from .finish import FINISH_PROTOCOL_REMINDER, FINISH_TOOL_DEFINITION, find_finish_call
from .utils import LLMMessage, create_llm_client, get_max_tokens, get_model_api_key

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.llm import LLMModel


def _cache_hit_ratio(usage: dict | None) -> float | None:
    """``cached_tokens / prompt_tokens`` for one LLM round, or ``None`` when
    unknown. Surfaces prefix-cache effectiveness: a ratio that stays low while
    a tool loop grows means the tail is being re-prefilled every round."""
    if not usage:
        return None
    prompt = usage.get("prompt_tokens") or 0
    if not prompt:
        return None
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or usage.get("cached_tokens") or 0
    return round(cached / prompt, 3)


TOOLS_REQUIRING_ARGS = frozenset(
    {
        "write_file",
        "read_file",
        "move_file",
        "delete_file",
        "read_document",
        "send_message_to_agent",
        "send_feishu_message",
        "send_email",
    }
)


# ─── P4: max_output_tokens recovery (Claude-Code-aligned) ─────────────────────
# When a provider truncates the response because the output hit its per-call
# token cap (`finish_reason == "length"` for OpenAI-compat / Gemini, or
# `"max_tokens"` for Anthropic), we *do not* surface an error. Instead we push
# the partial text into history as an assistant message, append a terse
# "continue" user prompt (verbatim from Claude Code), and re-stream. Up to
# MAX_OUTPUT_TOKENS_RECOVERY_LIMIT resume attempts per call; after that we
# surface a clear error so the failover layer can decide what to do.
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3

# Claude Code's exact prompt text for the resume nudge. Keep verbatim so we
# inherit its tuned tone — short, no recap, no apology.
RESUME_PROMPT = (
    "Output token limit hit. Resume directly — no apology, no recap of what "
    "you were doing. Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)

# Finish-reason strings that mean "output cap reached mid-response" across
# providers. Gemini's _normalize_finish_reason maps MAX_TOKENS → "length";
# OpenAI-compat natively emits "length"; Anthropic's stream() keeps the raw
# "max_tokens" string (end_turn/tool_use are normalized but max_tokens is
# not). We accept all three spellings to be defensive across provider code
# paths (and to tolerate any future Gemini fallback where the raw label slips
# through unnormalized).
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "MAX_TOKENS"})


def _response_was_truncated_by_length(response) -> bool:
    """True iff the LLM ran out of output tokens mid-response.

    Uses LLMResponse.finish_reason — which each provider client already
    populates. See _TRUNCATED_FINISH_REASONS for the recognised strings.
    """
    reason = getattr(response, "finish_reason", None)
    if not reason:
        return False
    return reason in _TRUNCATED_FINISH_REASONS


# ═══════════════════════════════════════════════════════════════════════════════
# Failover Guard
# ═══════════════════════════════════════════════════════════════════════════════


class FailoverGuard:
    """Guard state for failover decisions."""

    def __init__(self):
        self.tool_executed = False
        self.streaming_started = False
        self.failover_done = False

    def mark_tool_executed(self):
        """Mark that a side-effecting tool has been executed."""
        self.tool_executed = True

    def mark_streaming_started(self):
        """Mark that streaming output has started."""
        self.streaming_started = True

    def mark_failover_done(self):
        """Mark that failover has already happened once."""
        self.failover_done = True

    def can_failover(self) -> bool:
        """Check if failover is allowed based on guard rules."""
        if self.failover_done:
            return False  # Only failover once
        if self.tool_executed:
            return False  # Don't failover after side effects
        if self.streaming_started:
            return False  # Don't failover after streaming started
        return True


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.

    Uses unified classification from failover.py.
    """
    if not (result.startswith("[LLM Error]") or result.startswith("[LLM call error]") or result.startswith("[Error]")):
        return False

    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _get_model_timeout(model: "LLMModel") -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _usage_from_response_or_estimate(response, api_messages: list[LLMMessage]) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    round_chars = sum(len(m.content or "") if isinstance(m.content, str) else 0 for m in api_messages)
    round_chars += len(response.content or "")
    return estimate_token_usage_from_chars(round_chars)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def _get_agent_config(agent_id) -> tuple[int, str | None]:
    """Get agent config: max_tool_rounds and token limit status."""
    if not agent_id:
        return 50, None

    try:
        from app.models.agent import Agent as AgentModel

        async with async_session() as _db:
            _ar = await _db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _ar.scalar_one_or_none()
            if _agent:
                max_rounds = _agent.max_tool_rounds or 50
                if _agent.max_tokens_per_day and _agent.tokens_used_today >= _agent.max_tokens_per_day:
                    return (
                        max_rounds,
                        f"⚠️ Daily token usage has reached the limit ({_agent.tokens_used_today:,}/{_agent.max_tokens_per_day:,}). Please try again tomorrow or ask admin to increase the limit.",
                    )
                if _agent.max_tokens_per_month and _agent.tokens_used_month >= _agent.max_tokens_per_month:
                    return (
                        max_rounds,
                        f"⚠️ Monthly token usage has reached the limit ({_agent.tokens_used_month:,}/{_agent.max_tokens_per_month:,}). Please ask admin to increase the limit.",
                    )
                return max_rounds, None
    except Exception:
        pass
    return 50, None


async def _get_user_name(user_id) -> str | None:
    """Get user's display name for personalized context."""
    if not user_id:
        return None
    try:
        from app.models.user import User as _UserModel

        async with async_session() as _udb:
            _ur = await _udb.execute(select(_UserModel).where(_UserModel.id == user_id))
            _u = _ur.scalar_one_or_none()
            if _u:
                return _u.display_name or _u.username
    except Exception:
        pass
    return None


def _convert_messages_for_vision(api_messages: list, supports_vision: bool) -> list:
    """Convert image markers to vision format if supported, or strip them."""
    import re as _re_v
    import copy

    # Deep copy to avoid modifying the original list in place
    new_messages = copy.deepcopy(api_messages)

    if supports_vision:
        # Vision format: convert image markers in strings to OpenAI Vision API list format
        for i, msg in enumerate(new_messages):
            if msg.role != "user" or not msg.content or not isinstance(msg.content, str):
                continue

            content_str = msg.content
            pattern = r"\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]"
            images = _re_v.findall(pattern, content_str)

            if not images:
                continue

            text = _re_v.sub(pattern, "", content_str).strip()
            parts = [{"type": "image_url", "image_url": {"url": img}} for img in images]
            if text:
                # Per OpenAI spec, text part should come after image parts
                parts.append({"type": "text", "text": text})

            new_messages[i] = type(msg)(
                role=msg.role, content=parts, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
            )
    else:
        # Non-vision format: ensure content is a string for all roles, stripping image data.
        _img_marker_pattern = r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]"
        for i, msg in enumerate(new_messages):
            if isinstance(msg.content, list):
                # It's a list, join all text parts. This handles user messages
                # with vision content and tool messages from vision_inject.
                text_parts = [part.get("text", "") for part in msg.content if part.get("type") == "text"]
                content_str = "\n".join(text_parts).strip()
                new_messages[i] = type(msg)(
                    role=msg.role, content=content_str, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

            elif isinstance(msg.content, str) and "[image_data:" in msg.content:
                # It's a string with image markers, strip them
                _n_imgs = len(_re_v.findall(_img_marker_pattern, msg.content))
                cleaned = _re_v.sub(_img_marker_pattern, "", msg.content).strip()
                if _n_imgs > 0:
                    cleaned += f"\n[用户发送了 {_n_imgs} 张图片，但当前模型不支持视觉，无法查看图片内容]"
                new_messages[i] = type(msg)(
                    role=msg.role, content=cleaned, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

    return new_messages


def _inject_first_hop_context(
    api_messages: list,
    dynamic_prompt: str | None,
    *,
    inject: bool,
) -> list:
    """Return a copy of ``api_messages`` with dynamic context wrapped around
    the last ``user`` message — only when ``inject`` is True.

    Layout after injection:
        last_user.content = f"<context>\\n{dynamic_prompt}\\n</context>\\n\\n{original}"

    Why a wrapper instead of a separate message?
    - Keeps message count stable (Qwen/DashScope prefix cache is byte-level).
    - Keeps append-only history: we never mutate ``api_messages`` in place;
      only the dispatched copy carries the wrapper.

    When there is no ``user`` message in history, or dynamic_prompt is empty,
    or inject is False, we return a shallow list copy unchanged. The caller
    must pass ``inject=True`` only on the first hop of the tool loop — on
    continuation rounds the tail is assistant/tool messages and wrapping would
    have no valid target anyway.

    The returned list contains fresh ``LLMMessage`` instances for any message
    we modify, so the caller's ``api_messages`` stays byte-identical for the
    next round's cache prefix.
    """
    out = list(api_messages)
    if not inject or not dynamic_prompt:
        return out

    # Find the last role="user" index
    last_user_idx = -1
    for i in range(len(out) - 1, -1, -1):
        if out[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return out

    original = out[last_user_idx]
    original_content = original.content
    # If content is a vision list, wrap the trailing text part (vision_convert
    # already places text after images); if there's no text part, append one.
    if isinstance(original_content, list):
        new_parts = [dict(p) for p in original_content]
        text_idx = -1
        for i in range(len(new_parts) - 1, -1, -1):
            if new_parts[i].get("type") == "text":
                text_idx = i
                break
        wrapper_prefix = f"<context>\n{dynamic_prompt}\n</context>\n\n"
        if text_idx >= 0:
            existing_text = new_parts[text_idx].get("text", "")
            new_parts[text_idx] = {
                "type": "text",
                "text": f"{wrapper_prefix}{existing_text}",
            }
        else:
            new_parts.append({"type": "text", "text": wrapper_prefix.rstrip()})
        new_content: str | list = new_parts
    else:
        base = original_content or ""
        new_content = f"<context>\n{dynamic_prompt}\n</context>\n\n{base}"

    out[last_user_idx] = LLMMessage(
        role=original.role,
        content=new_content,
        tool_calls=original.tool_calls,
        tool_call_id=original.tool_call_id,
        reasoning_content=original.reasoning_content,
        reasoning_signature=original.reasoning_signature,
    )
    return out


def _check_tool_requires_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """Check if tool requires arguments and return (should_execute, result_or_error)."""
    if not args and tool_name in TOOLS_REQUIRING_ARGS:
        return (
            False,
            f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments.",
        )
    return True, ""


# ─── Upstream-imported helpers ────────────────────────────────────────────────
# Restored after the take-ours conflict resolution dropped them: the call sites
# (_process_tool_call, the tool execution loops at ~700 and ~1100) were merged
# from upstream and still reference these symbols, so the definitions must
# coexist with the fork's _canonicalize_tc_arguments / _shape_tool_content_for_context.


def _sanitize_tool_calls_for_context(tool_calls: list[dict]) -> tuple[list[dict] | None, str | None]:
    """Return OpenAI-compatible tool calls, or a retry instruction if args are invalid."""
    sanitized: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        tool_name = fn.get("name") or ""
        raw_args = fn.get("arguments", "{}")

        if raw_args is None or raw_args == "":
            args_str = "{}"
        elif isinstance(raw_args, str):
            try:
                json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[LLM] Invalid tool arguments JSON for {}: {} at pos {}",
                    tool_name or "<unknown>",
                    exc.msg,
                    exc.pos,
                )
                return None, (
                    "Your previous tool call arguments were not valid JSON. "
                    f"The affected tool was `{tool_name or 'unknown'}`. "
                    "Retry the tool call now with `function.arguments` as one valid JSON object string. "
                    "Escape all quotes and newlines inside long HTML, CSS, JavaScript, or markdown content. "
                    "Do not explain; only retry with a valid tool call."
                )
            args_str = raw_args
        elif isinstance(raw_args, (dict, list)):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            return None, (
                "Your previous tool call arguments had an unsupported type. "
                f"The affected tool was `{tool_name or 'unknown'}`. "
                "Retry the tool call with `function.arguments` as one valid JSON object string."
            )

        sanitized.append(
            {
                "id": tc.get("id", ""),
                "type": tc.get("type") or "function",
                "function": {
                    "name": tool_name,
                    "arguments": args_str,
                },
            }
        )

    return sanitized, None


def _allowed_tool_names(tools_for_llm: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools_for_llm or []:
        name = ((tool.get("function") or {}).get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _tool_not_enabled_message(tool_name: str) -> str:
    return (
        f"Tool `{tool_name}` is not enabled for this agent. "
        "Do not call it again. Use only the tools currently available to you, "
        "or explain that the required capability is not enabled."
    )


def _canonicalize_tc_arguments(tc: dict, session_id: str) -> dict[str, Any]:
    """Canonicalize ``tc['function']['arguments']`` in place and return the parsed dict.

    The canonical JSON is written back to ``tc['function']['arguments']`` so that
    any subsequent LLM round receiving this ``tc`` in conversation history will
    pass DashScope's ``function.arguments must be in JSON format`` validation.
    Used by both in-flight tool loops (_process_tool_call and _try_model).
    """
    fn = tc["function"]
    tool_name = fn["name"]
    raw_args = fn.get("arguments", "{}")
    args, canonical_args, repair_method = canonicalize_tool_arguments(raw_args)
    fn["arguments"] = canonical_args
    if repair_method != "clean":
        logger.warning(
            f"[LLM] tool_call args repaired: tool={tool_name} method={repair_method} "
            f"orig_len={len(raw_args)} new_len={len(canonical_args)} session={session_id}"
        )
    return args


async def _process_tool_call(
    tc: dict,
    api_messages: list,
    agent_id,
    user_id,
    session_id: str,
    supports_vision: bool,
    on_tool_call,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
) -> str:
    """Process a single tool call and return result."""
    raw_args = tc["function"].get("arguments", "{}")
    logger.info(f"[LLM] Calling tool: {tc['function']['name']}({json.dumps(raw_args, ensure_ascii=False)[:100]})")

    args = _canonicalize_tc_arguments(tc, session_id)
    tool_name = tc["function"]["name"]

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        return error_msg

    if tool_name not in allowed_tool_names:
        result = _tool_not_enabled_message(tool_name)
        logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
        if on_tool_call:
            try:
                await on_tool_call(
                    {
                        "name": tool_name,
                        "call_id": tc.get("id", ""),
                        "args": args,
                        "status": "done",
                        "result": result,
                        "reasoning_content": full_reasoning_content,
                    }
                )
            except Exception:
                pass
        api_messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=tc["id"],
                content=result,
            )
        )
        return ""

    # Notify client about tool call (in-progress)
    if on_tool_call:
        try:
            await on_tool_call(
                {
                    "name": tool_name,
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "running",
                    "reasoning_content": full_reasoning_content,
                }
            )
        except Exception:
            pass

    # Execute tool
    result = await execute_tool(
        tool_name,
        args,
        agent_id=agent_id,
        user_id=user_id or agent_id,
        session_id=session_id,
    )
    logger.debug(f"[LLM] Tool result: {result[:100]}")

    # Materialize oversize output and produce the canonical llm_view string.
    # This is the single shape point — DB, WS live stream, historical replay
    # all consume this string, keeping the messages sequence append-only.
    llm_view = finalize_tool_output(
        result,
        tool_name=tool_name,
        agent_id=agent_id,
        session_id=session_id,
        tool_call_id=tc["id"],
    )

    # Vision injection is a one-shot enhancement for the current turn only.
    # Historical replay sees the string llm_view; only this round gets the
    # richer image payload.
    tool_content: str | list = llm_view
    if supports_vision and agent_id:
        try:
            from app.services.vision_inject import try_inject_screenshot_vision
            from app.config import get_settings

            settings = get_settings()
            ws_path = Path(settings.AGENT_DATA_DIR) / str(agent_id)
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = vision_content
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client (for WS live stream and DB persistence) with the
    # llm_view — never the raw result. Three-way consistency: DB view,
    # LLM replay view, and the value the frontend receives all match.
    if on_tool_call:
        try:
            await on_tool_call(
                {
                    "name": tool_name,
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "done",
                    "result": llm_view,
                    "reasoning_content": full_reasoning_content,
                }
            )
        except Exception:
            pass

    api_messages.append(
        LLMMessage(
            role="tool",
            tool_call_id=tc["id"],
            content=tool_content,
        )
    )
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Core LLM Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_thinking=None,
    supports_vision=False,
    max_tool_rounds_override: int | None = None,
    skip_tools: bool = False,
    is_group: bool = False,
) -> str:
    """Call LLM via unified client with function-calling tool loop."""
    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id)
    if _token_limit_msg:
        return _token_limit_msg
    if max_tool_rounds_override and max_tool_rounds_override < _max_tool_rounds:
        _max_tool_rounds = max_tool_rounds_override

    # Get user's name for personalized context
    _user_name = await _get_user_name(user_id)

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context

    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(
        agent_id, agent_name, role_description, current_user_name=_user_name, is_group=is_group
    )

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS handler
    # on the onboarding greeting turn; keep the runtime-level `finish` tool so
    # every turn still has an explicit stop signal.
    # Sort by function.name so Anthropic's tools[-1] cache_control lands on a
    # stable tool block across calls — DB iteration-order churn would otherwise
    # invalidate the tools prefix cache every request.
    if skip_tools:
        tools_for_llm = [FINISH_TOOL_DEFINITION]
    else:
        tools_for_llm = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
    if tools_for_llm:
        tools_for_llm = sorted(
            tools_for_llm,
            key=lambda t: t.get("function", {}).get("name", ""),
        )
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    # Convert messages to LLMMessage format.
    # IMPORTANT (context-v2 / P1-A): system holds ONLY the static prompt so the
    # system prefix stays byte-identical across calls — Anthropic prompt cache
    # and Qwen/DashScope automatic prefix cache need this to hit. Dynamic bits
    # (current time, memory, focus, round warnings) are injected into the
    # last-user-message on the "first hop" below, so history stays append-only.
    api_messages = [LLMMessage(role="system", content=static_prompt)]
    for msg in messages:
        api_messages.append(
            LLMMessage(
                role=msg.get("role", "user"),
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            )
        )

    # Vision format conversion
    api_messages = _convert_messages_for_vision(api_messages, supports_vision)

    # Create the unified LLM client
    try:
        client = create_llm_client(
            provider=model.provider,
            api_key=get_model_api_key(model),
            model=model.model,
            base_url=model.base_url,
            timeout=_get_model_timeout(model),
        )
    except Exception as e:
        return f"[Error] Failed to create LLM client: {e}"

    max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))
    _accumulated_usage = TokenUsage()

    # P4: per-call resume counter for max_output_tokens truncation.
    # Bounded across the whole call so a runaway agent cannot turn a long
    # tool loop into 50×3 redundant resumes on a misconfigured cap.
    max_output_recoveries = 0

    # Tool-calling loop
    for round_i in range(_max_tool_rounds):
        # Dynamic tool-call limit warning.
        # NB (context-v2): these warnings stay as plain appended user messages.
        # That keeps api_messages append-only — once appended at round N, the
        # warning sits in a fixed historical position for every later send, so
        # the prefix cache before it is not invalidated. Do NOT fold round
        # warnings into the per-round <context> block: that would mutate the
        # last user message mid-loop and break cache hits.
        _warn_threshold_80 = int(_max_tool_rounds * 0.8)
        _warn_threshold_96 = _max_tool_rounds - 2
        if round_i == _warn_threshold_80:
            api_messages.append(
                LLMMessage(
                    role="user",
                    content=(
                        f"⚠️ 你已使用 {round_i}/{_max_tool_rounds} 轮工具调用。"
                        "如果当前任务尚未完成，请尽快使用 upsert_focus_item 保存进度，"
                        "并使用 set_trigger 设置续接触发器，在剩余轮次中做好收尾。"
                    ),
                )
            )
        elif round_i == _warn_threshold_96:
            api_messages.append(
                LLMMessage(
                    role="user",
                    content="🚨 仅剩 2 轮工具调用。请立即使用 upsert_focus_item 保存进度并设置续接触发器。",
                )
            )

        # Build the "dispatch" view of api_messages sent this round.
        # Only on round 0 (the first hop) do we wrap the last user message in a
        # <context>…</context> prefix carrying dynamic_prompt (current time,
        # memory, focus, …). Subsequent rounds are tool-calling continuations
        # whose trailing items are assistant/tool messages — the LLM already
        # has the context from round 0 in its own cached prefix, and we don't
        # want to mutate the original history (breaks append-only + cache).
        dispatch_messages = _inject_first_hop_context(
            api_messages,
            dynamic_prompt,
            inject=(round_i == 0),
        )

        try:
            # Use streaming API for real-time responses
            async def _buffer_chunk(_text: str) -> None:
                # Final user-facing text must come through finish(content=...).
                return None

            response = await client.stream(
                messages=dispatch_messages,
                tools=tools_for_llm if tools_for_llm else None,
                temperature=model.temperature,
                max_tokens=max_tokens,
                on_chunk=_buffer_chunk,
                on_tool_delta=on_tool_delta,
                on_thinking=on_thinking,
            )

            # ── P4: max_output_tokens recovery ────────────────────────────
            # If the response was cut off because we hit the per-call output
            # cap, push the partial text into history as an assistant turn,
            # nudge the model to "continue", and re-stream. Accumulate the
            # partial pieces so the final ``response.content`` returned to
            # the caller is the complete concatenation — downstream DB
            # persistence and tool-calling handling stay unchanged.
            accumulated_partials: list[str] = []
            recovery_count_this_round = 0
            while (
                _response_was_truncated_by_length(response) and max_output_recoveries < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT
            ):
                max_output_recoveries += 1
                recovery_count_this_round += 1
                partial_text = response.content or ""
                accumulated_partials.append(partial_text)
                # Transient scaffolding so the next stream call has a clear
                # "here is what you just said, now continue" context. We pop
                # these back off once the recovery loop succeeds so the final
                # assistant turn lands as a single clean message.
                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=partial_text,
                    )
                )
                api_messages.append(
                    LLMMessage(
                        role="user",
                        content=RESUME_PROMPT,
                    )
                )
                logger.info(
                    f"[LLM] max_output_tokens hit; resume attempt "
                    f"{max_output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERY_LIMIT} "
                    f"(round {round_i + 1}, this-round {recovery_count_this_round})"
                )

                # Re-build dispatch view. Dynamic <context> injection is a
                # FIRST-hop-only nudge — do not re-inject on resumes.
                dispatch_messages = _inject_first_hop_context(
                    api_messages,
                    dynamic_prompt,
                    inject=False,
                )
                response = await client.stream(
                    messages=dispatch_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    temperature=model.temperature,
                    max_tokens=max_tokens,
                    on_chunk=on_chunk,
                    on_thinking=on_thinking,
                )

            # Still truncated after exhausting the resume budget — surface
            # a clear error. The failover layer will see a [LLM Error] and
            # can decide whether to try the fallback model (which may have
            # a larger output cap).
            if _response_was_truncated_by_length(response):
                logger.error(
                    f"[LLM] Output token limit not recoverable after {MAX_OUTPUT_TOKENS_RECOVERY_LIMIT} resume attempts"
                )
                if agent_id and _accumulated_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _accumulated_usage)
                await client.close()
                return "[LLM Error] Output token limit exceeded after 3 resume attempts"

            # Recovery succeeded (or never happened). If we accumulated any
            # partials, stitch them onto the final content and pop the
            # transient (partial_assistant, resume_user) pairs so history
            # keeps its "one logical turn = one message" shape.
            if recovery_count_this_round:
                full_content = "".join(accumulated_partials) + (response.content or "")
                # Each recovery appended exactly 2 messages.
                del api_messages[-(2 * recovery_count_this_round) :]
                response.content = full_content
        except LLMError as e:
            logger.error(
                f"[LLM] LLMError: provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}"
            )
            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            await client.close()
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            await client.close()
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        # Track tokens for this round
        _accumulated_usage.add(_usage_from_response_or_estimate(response, api_messages))

        # Observability: prefix-cache effectiveness for this round. A ratio that
        # stays low while the tool loop grows means the tail isn't being cached
        # (re-prefilled every round → "responses get slower as the chat grows").
        _usage = getattr(response, "usage", None)
        _ratio = _cache_hit_ratio(_usage)
        if _ratio is not None and isinstance(_usage, dict):
            _prompt = _usage.get("prompt_tokens")
            _line = f"[LLM] Round {round_i + 1} cache-hit-ratio={_ratio} prompt={_prompt}"
            if _ratio < float(os.environ.get("CLAWITH_CACHE_HIT_WARN_RATIO", "0.7")):
                logger.warning(_line + " (LOW — prefix cache may be missing the tool-loop tail)")
            else:
                logger.info(_line)

        # Plain assistant text is not a stop condition. The model must finish
        # explicitly via finish(content=...).
        if not response.tool_calls:
            if response.content:
                api_messages.append(LLMMessage(role="assistant", content=response.content))
            api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
            continue

        # Execute tool calls
        logger.info(f"[LLM] Round {round_i + 1}: {len(response.tool_calls)} tool call(s)")
        sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
        if retry_instruction:
            api_messages.append(LLMMessage(role="user", content=retry_instruction))
            continue

        finish_call = find_finish_call(sanitized_tool_calls)
        if finish_call:
            if finish_call.valid:
                if agent_id and _accumulated_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _accumulated_usage)
                await client.close()
                return finish_call.content

            api_messages.append(LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            ))
            api_messages.append(LLMMessage(
                role="tool",
                content=finish_call.error or "`finish` was invalid.",
                tool_call_id=finish_call.call_id,
            ))
            continue

        # Remember where this round's appended entries begin. The message-level
        # budget enforcer operates only on items at or beyond this index —
        # historical messages (already sent as prefix bytes in prior rounds) must
        # stay byte-identical so Anthropic / Qwen / DashScope prefix caches keep
        # hitting.
        fresh_start = len(api_messages)

        # Add assistant message with tool calls
        # NB: tc["function"] is shared by reference with _canonicalize_tc_arguments's
        # in-place canonicalization — must stay as a reference (no deepcopy), or
        # history entries will carry the pre-repair malformed arguments.
        api_messages.append(
            LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )

        full_reasoning_content = response.reasoning_content or ""

        for tc in sanitized_tool_calls or []:
            tool_error = await _process_tool_call(
                tc=tc,
                api_messages=api_messages,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                supports_vision=supports_vision,
                on_tool_call=on_tool_call,
                full_reasoning_content=full_reasoning_content,
                allowed_tool_names=allowed_tool_names,
            )
            if tool_error:
                api_messages.append(
                    LLMMessage(
                        role="tool",
                        content=tool_error,
                        tool_call_id=tc.get("id", ""),
                    )
                )

        # P2: A single round can produce many in-budget tool results whose
        # sum blows past the message-level cap (e.g. 3 × 30 KB RAGFlow
        # queries). Enforce the ceiling now, after every tool message for
        # this round is appended but before the next client.stream call.
        enforce_message_budget(
            api_messages,
            fresh_start_idx=fresh_start,
            agent_id=agent_id,
            session_id=session_id,
        )

        # Auto-compaction hook (P5).
        # When this round's prompt_tokens crosses the per-model
        # `compact_trigger_ratio` (default 0.85 of context_window), kick
        # off a structured-summary compaction so the NEXT call_llm
        # invocation loads a slimmer history. We deliberately do NOT
        # mutate `api_messages` in place — the in-memory list still
        # carries the long prefix for the remainder of this round, and
        # the channel's next history load through chat_history's
        # compaction-aware path picks up the new shape transparently.
        # 15% safety margin in the current request is enough to cover
        # the ~2 additional tool rounds that may follow before the
        # tool loop exits.
        if agent_id and session_id:
            last_prompt_tokens = None
            if response and response.usage:
                last_prompt_tokens = response.usage.get("prompt_tokens")
            if last_prompt_tokens:
                try:
                    from app.services.llm.compactor import maybe_compact

                    compaction_result = await maybe_compact(
                        agent_id=agent_id,
                        conversation_id=session_id,
                        model=model,
                        last_prompt_tokens=last_prompt_tokens,
                    )
                    if compaction_result.progress_notice and on_chunk:
                        await on_chunk(f"\n\n{compaction_result.progress_notice}\n\n")
                except Exception as compact_exc:
                    # Compaction is opportunistic — never let its
                    # failure abort the live conversation. The session
                    # falls back to ctx_size truncation on the next
                    # call_llm if context keeps growing.
                    logger.warning(
                        f"[LLM] auto-compaction hook failed (non-fatal): {type(compact_exc).__name__}: {compact_exc}"
                    )

    # Record tokens even on "too many rounds" exit
    if agent_id and _accumulated_usage.total_tokens > 0:
        await record_token_usage(agent_id, _accumulated_usage)
    await client.close()
    return "[Error] Too many tool call rounds"


async def call_llm_with_failover(
    primary_model,
    fallback_model,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_tool_call=None,
    on_tool_delta=None,
    supports_vision=False,
    on_failover=None,
    skip_tools: bool = False,
    is_group: bool = False,
) -> str:
    """Call LLM with automatic failover support."""
    guard = FailoverGuard()

    # Config-level fallback: if no primary, use fallback directly
    if primary_model is None and fallback_model is not None:
        logger.info("[Failover] Primary model not configured, using fallback directly")
        primary_model = fallback_model
        fallback_model = None

    if primary_model is None:
        return "⚠️ 未配置 LLM 模型"

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_tool_call(data: dict):
        if data.get("status") == "done":
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=supports_vision,
        skip_tools=skip_tools,
        is_group=is_group,
    )

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        logger.warning(f"[Failover] Canceled: Primary model returned a non-retryable error: {primary_result[:150]}")
        return primary_result

    # Check guard conditions
    if not guard.can_failover():
        if guard.tool_executed:
            logger.warning("[Failover] Blocked: side-effecting tool already executed")
        elif guard.streaming_started:
            logger.warning("[Failover] Blocked: streaming already started")
        elif guard.failover_done:
            logger.warning("[Failover] Blocked: failover already done once")
        return primary_result

    # No fallback available
    if fallback_model is None:
        logger.warning("[Failover] No fallback model available")
        return primary_result

    # Runtime failover: retry with fallback model
    logger.info(f"[Failover] Retrying with fallback model: {fallback_model.provider}/{fallback_model.model}")

    if on_failover:
        try:
            await on_failover(f"Switched to fallback model: {fallback_model.model}")
        except Exception:
            pass

    guard.mark_failover_done()

    # Call fallback with fresh callbacks
    fallback_guard = FailoverGuard()
    fallback_guard.mark_failover_done()

    async def _fallback_on_chunk(text: str):
        fallback_guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _fallback_on_tool_call(data: dict):
        if data.get("status") == "done":
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=getattr(fallback_model, "supports_vision", False),
        skip_tools=skip_tools,
        is_group=is_group,
    )

    # Combine error messages if fallback also failed
    if is_retryable_error(fallback_result) or fallback_result.startswith("⚠️") or fallback_result.startswith("[Error]"):
        return f"⚠️ 调用模型出错: Primary: {primary_result[:80]} | Fallback: {fallback_result[:80]}"

    return fallback_result


# ═══════════════════════════════════════════════════════════════════════════════
# High-level Agent Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    supports_vision: bool = False,
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.core.permissions import is_agent_expired

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    # Load primary model
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        primary_model = model_result.scalar_one_or_none()

    # Load fallback model
    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()

    # Config-level fallback: primary missing -> use fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None
        logger.warning(f"[call_agent_llm] Primary model unavailable, using fallback: {primary_model.model}")

    if not primary_model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

    # Build conversation messages
    messages: list[dict] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    # Use unified call_llm_with_failover
    try:
        reply = await call_llm_with_failover(
            primary_model=primary_model,
            fallback_model=fallback_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            supports_vision=supports_vision or getattr(primary_model, "supports_vision", False),
        )
        return reply
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[call_agent_llm] Unexpected error: {error_msg}")
        return f"⚠️ 调用模型出错: {error_msg[:150]}"


async def call_agent_llm_with_tools(
    db: AsyncSession,
    agent_id: uuid.UUID,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int = 50,
    session_id: str = "",
) -> str:
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # Load agent and models
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ Agent not found"

    # Load models
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        primary_model = model_result.scalar_one_or_none()

    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()

    # Config-level fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None

    if not primary_model:
        return f"⚠️ {agent.name} has no LLM model configured"

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    # Load tools
    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        tool_executed = False
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=get_model_api_key(model),
                model=model.model,
                base_url=model.base_url,
                timeout=_get_model_timeout(model),
            )

            max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))

            # Tool-calling loop
            api_messages = list(messages)
            for round_i in range(max_rounds):
                try:
                    response = await client.complete(
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client.close()
                    if agent_id and _accumulated_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _accumulated_usage)
                    raise

                # Track tokens for this round
                _accumulated_usage.add(_usage_from_response_or_estimate(response, api_messages))

                if not response.tool_calls:
                    # Plain assistant text is not a stop condition — the model must
                    # finish() explicitly. Nudge with the protocol reminder and loop.
                    if response.content:
                        api_messages.append(LLMMessage(role="assistant", content=response.content))
                    api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                    continue

                # Execute tool calls
                # Sanitize first — invalid tool args become a retry user message
                # (mirrors the streaming path's behavior in the main _try_model).
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue

                # finish() handling: valid → return content; invalid → surface the
                # error back to the model and loop.
                finish_call = find_finish_call(sanitized_tool_calls)
                if finish_call:
                    if finish_call.valid:
                        if agent_id and _accumulated_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _accumulated_usage)
                        await client.close()
                        return finish_call.content, True, tool_executed
                    api_messages.append(LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    ))
                    api_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=finish_call.call_id,
                        content=finish_call.error or "`finish` was invalid.",
                    ))
                    continue

                # Add assistant message with tool calls.
                # NB: tc["function"] is shared by reference with _canonicalize_tc_arguments's
                # in-place canonicalization — must stay a reference (no deepcopy), or
                # history entries will carry the pre-repair malformed arguments.
                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )

                for tc in sanitized_tool_calls or []:
                    args = _canonicalize_tc_arguments(tc, session_id)
                    tool_name = tc["function"]["name"]

                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(
                            f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={agent_id}"
                        )
                        result = _tool_not_enabled_message(tool_name)
                    else:
                        result = await execute_tool(
                            tool_name,
                            args,
                            agent_id=agent_id,
                            user_id=agent.creator_id,
                            session_id=session_id,
                        )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tc["id"],
                            content=str(result),
                        )
                    )

            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            await client.close()
            return "[Error] Too many tool call rounds", False, tool_executed

        except Exception as e:
            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            return f"[Error] {e}", False, tool_executed

    # Try primary model
    reply, success, primary_tool_executed = await _try_model(primary_model)
    if success:
        return reply

    # Primary failed - check if retryable
    error_type = classify_error(Exception(reply))
    if error_type == FailoverErrorType.NON_RETRYABLE or not fallback_model:
        return reply

    if primary_tool_executed:
        logger.warning("[call_agent_llm_with_tools] Blocked fallback: side-effecting tool already executed")
        return reply

    # Try fallback model
    logger.info(f"[call_agent_llm_with_tools] Retrying with fallback: {fallback_model.model}")
    reply2, success2, _fallback_tool_executed = await _try_model(fallback_model)
    if success2:
        return reply2

    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"


__all__ = [
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "FailoverGuard",
    "is_retryable_error",
]
