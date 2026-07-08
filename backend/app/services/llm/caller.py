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

import asyncio
import json
import os
import uuid
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.database import async_session

# NOTE: agent_tools imports are deferred to function bodies to avoid circular
# import: agent_tools → llm/__init__ → caller → agent_tools

async def get_agent_tools_for_llm(*args, **kwargs):
    from app.services.agent_tools import get_agent_tools_for_llm as _impl
    return await _impl(*args, **kwargs)

async def execute_tool(*args, **kwargs):
    from app.services.agent_tools import execute_tool as _impl
    return await _impl(*args, **kwargs)
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
from .confirmation_tool import find_request_confirmation_call
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

PROVIDER_THROTTLE_RETRY_DELAYS = (1.0, 2.0)
PROVIDER_THROTTLE_USER_MESSAGE = (
    "⚠️ 模型服务当前繁忙或被限流，已自动重试仍未成功，请稍后再试。"
)

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


class ProviderThrottleExhausted(Exception):
    """Raised after bounded retries for transient provider throttling."""


def _is_provider_throttle_error(error: Exception) -> bool:
    """Return True for transient provider throttling/capacity errors.

    Quota/token-limit errors are also HTTP 429, but they are not transient
    throttling and retrying them just burns time/tokens. Keep them distinct.
    """
    msg = str(error).lower()
    if any(
        kw in msg
        for kw in (
            "insufficient_quota",
            "token-limit",
            "exceeded your current quota",
            "quota",
        )
    ):
        return False

    return any(
        kw in msg
        for kw in (
            "limit_burst_rate",
            "request rate increased too quickly",
            "too many requests",
            "throttled due to system capacity",
            "system capacity limits",
            "serviceunavailable",
            "service unavailable",
            "http 503",
            "<503>",
        )
    )


async def _sleep_before_throttle_retry(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


async def _stream_with_throttle_retry(client, *, model, round_i: int, **stream_kwargs):
    attempts = 1 + len(PROVIDER_THROTTLE_RETRY_DELAYS)

    # Per-round latency observability: wall time + time-to-first-token (first
    # content/thinking/tool-args delta from the provider). Callbacks are wrapped
    # to timestamp the first delta; None callbacks get a marker-only no-op
    # (safe — clients simply invoke whatever callback is present).
    _first_token_at: list[float] = []

    def _wrap_first_token(cb):
        async def _marked(*a, **k):
            if not _first_token_at:
                _first_token_at.append(perf_counter())
            if cb is not None:
                return await cb(*a, **k)
        return _marked

    for _cb_key in ("on_chunk", "on_thinking"):
        stream_kwargs[_cb_key] = _wrap_first_token(stream_kwargs.get(_cb_key))
    if stream_kwargs.get("on_tool_delta") is not None:
        stream_kwargs["on_tool_delta"] = _wrap_first_token(stream_kwargs["on_tool_delta"])

    for attempt_idx in range(attempts):
        _first_token_at.clear()
        _t0 = perf_counter()
        try:
            response = await client.stream(**stream_kwargs)
            _elapsed = perf_counter() - _t0
            _ttft = f"{_first_token_at[0] - _t0:.2f}s" if _first_token_at else "n/a"
            _usage = getattr(response, "usage", None)
            _out_tokens = _usage.get("completion_tokens") if isinstance(_usage, dict) else None
            _rate = f" ({_out_tokens / _elapsed:.0f} tok/s)" if _out_tokens and _elapsed > 0 else ""
            logger.info(
                f"[LLM Timing] round={round_i} model={getattr(model, 'model', '?')} "
                f"llm_call={_elapsed:.2f}s ttft={_ttft} output_tokens={_out_tokens}{_rate}"
            )
            return response
        except LLMError as e:
            if not _is_provider_throttle_error(e):
                raise
            if attempt_idx >= len(PROVIDER_THROTTLE_RETRY_DELAYS):
                raise ProviderThrottleExhausted(str(e)) from e

            delay = PROVIDER_THROTTLE_RETRY_DELAYS[attempt_idx]
            logger.warning(
                f"[LLM] Provider throttle; retrying after {delay:.1f}s "
                f"(attempt {attempt_idx + 2}/{attempts}, round {round_i}, "
                f"provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')}): {e}"
            )
            await _sleep_before_throttle_retry(delay)


# ── Repeated tool-call guard ─────────────────────────────────────────────────
# DashScope / qwen returns a hard 400 ("Repetitive tool calls detected ... the
# same tool call with identical name and arguments has been repeated across
# multiple consecutive rounds") once the conversation history carries several
# back-to-back identical assistant tool_calls. A model stuck re-issuing one tool
# that keeps failing the same way (a 500ing API, an unreachable sandbox) would
# otherwise crash the WHOLE turn — the user sees the raw error + a "/new" prompt
# and loses all progress. We track per-signature consecutive-round streaks and
# step in BEFORE the history grows enough to trip that 400:
#   • at REPEAT_TOOL_CALL_NUDGE identical rounds → inject one corrective nudge,
#     giving the model a chance to change approach or answer honestly;
#   • at REPEAT_TOOL_CALL_BREAK identical rounds → stop the loop gracefully
#     (before appending/re-sending this Nth call), so the provider never sees
#     enough repetition to reject. Capping history at BREAK-1 identical calls
#     keeps us safely under the provider's threshold.
REPEAT_TOOL_CALL_NUDGE = 2
REPEAT_TOOL_CALL_BREAK = 3

REPEAT_TOOL_CALL_NUDGE_PROMPT = (
    "⚠️ 你刚刚用完全相同的参数重复调用了同一个工具，结果不会改变。"
    "请不要再用相同参数重复调用：换一种方法或参数；如果确实无法完成，"
    "请直接如实向用户说明情况和已经掌握的信息。"
)

REPEAT_TOOL_CALL_BREAK_MESSAGE = (
    "抱歉，我在用相同的方式反复调用同一个工具，但始终没有得到新的结果，"
    "为避免无效循环我先停在这里。这通常意味着对应的数据或接口当前不可用。"
    "你可以换个问法、缩小范围，或稍后再试。"
)


def _tool_call_signature(tc: dict) -> tuple[str, str]:
    """Stable ``(name, canonical-args)`` identity for a tool call.

    Arguments are JSON-normalised (sorted keys) so semantically identical calls
    compare equal regardless of key order / whitespace — at least as strict as
    the provider's own "identical arguments" check. Falls back to the trimmed
    raw string when the arguments are not valid JSON.
    """
    fn = (tc or {}).get("function") or {}
    name = fn.get("name") or ""
    raw = fn.get("arguments")
    if raw is None or raw == "":
        return (name, "")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        args_key = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args_key = raw.strip() if isinstance(raw, str) else str(raw)
    return (name, args_key)


def _update_repeat_streaks(
    prev_streaks: dict[tuple[str, str], int],
    round_signatures: list[tuple[str, str]],
) -> dict[tuple[str, str], int]:
    """Consecutive-round streak counts after one round of tool calls.

    A signature seen this round extends its prior streak (+1); any signature NOT
    seen this round drops out (streak resets to 0). Pure — does not mutate
    ``prev_streaks``. Identical calls within the *same* round count once (the
    provider's 400 is about repetition *across* rounds).
    """
    return {sig: prev_streaks.get(sig, 0) + 1 for sig in round_signatures}


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


def is_error_result(result: str) -> bool:
    """True when *result* is an error sentinel string, not a normal model reply.

    The LLM/tool layer signals failures by returning a string prefixed with one
    of these markers instead of raising, so a successful reply never matches.
    """
    return result.startswith(("[LLM Error]", "[LLM call error]", "[Error]"))


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.

    Uses unified classification from failover.py.
    """
    if not is_error_result(result):
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


def _coerce_uuid(value) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _persist_tool_call_events_strict(
    events: list[dict],
    *,
    agent_id,
    user_id,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> bool:
    """Durably append tool-call markers before the loop relies on them.

    Returns False for non-persistable test/background calls without UUID ids.
    For real chat turns, DB errors propagate and stop execution before side
    effects can happen without a recovery marker.
    """
    if not events or not session_id:
        return False
    agent_uuid = _coerce_uuid(agent_id)
    user_uuid = _coerce_uuid(user_id or agent_id)
    if agent_uuid is None or user_uuid is None:
        return False

    from app.services.chat_history import persist_tool_call_row

    async with async_session() as db:
        for evt in events:
            await persist_tool_call_row(
                db,
                agent_id=agent_uuid,
                user_id=user_uuid,
                conversation_id=session_id,
                evt=evt,
                turn_anchor_id=turn_anchor_id,
            )
        await db.commit()
    return True


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
        from app.models.agent import Agent as _AgentModel

        async with async_session() as _udb:
            _ur = await _udb.execute(select(_UserModel).where(_UserModel.id == user_id))
            _u = _ur.scalar_one_or_none()
            if _u:
                return _u.display_name or _u.username
            # Check Agent name fallback (A2A: the "user" may be a peer agent)
            _ar = await _udb.execute(select(_AgentModel).where(_AgentModel.id == user_id))
            _a = _ar.scalar_one_or_none()
            if _a:
                return _a.name
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

        new_tc = {
            "id": tc.get("id", ""),
            "type": tc.get("type") or "function",
            "function": {
                "name": tool_name,
                "arguments": args_str,
            },
        }
        if "_gemini_extra" in tc:
            new_tc["_gemini_extra"] = tc["_gemini_extra"]
        sanitized.append(new_tc)

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
    on_code_output=None,
    emit_running: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
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

    # Notify client about tool call (in-progress). The normal multi-tool loop
    # pre-emits running markers for the whole round before executing any tool;
    # direct helper callers keep the historical behavior through emit_running=True.
    if emit_running:
        running_evt = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "running",
            "reasoning_content": full_reasoning_content,
        }
        if await _persist_tool_call_events_strict(
            [running_evt],
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        ):
            running_evt["_durable_persisted"] = True
    else:
        running_evt = None

    if running_evt is not None and on_tool_call:
        try:
            await on_tool_call(running_evt)
        except Exception:
            pass

    # Execute tool — pass on_output for execute_code streaming
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    _tool_t0 = perf_counter()
    result = await execute_tool(
        tool_name,
        args,
        agent_id=agent_id,
        user_id=user_id or agent_id,
        session_id=session_id,
        on_output=_on_output,
    )
    logger.info(f"[LLM Timing] tool={tool_name} exec={perf_counter() - _tool_t0:.2f}s agent={agent_id}")
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
            settings = get_settings()
            ws_path = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR) / str(agent_id)
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = vision_content
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client (for WS live stream and DB persistence) with the
    # llm_view — never the raw result. Three-way consistency: DB view,
    # LLM replay view, and the value the frontend receives all match.
    done_evt = {
        "name": tool_name,
        "call_id": tc.get("id", ""),
        "args": args,
        "status": "done",
        "result": llm_view,
        "reasoning_content": full_reasoning_content,
    }
    if await _persist_tool_call_events_strict(
        [done_evt],
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        turn_anchor_id=turn_anchor_id,
    ):
        done_evt["_durable_persisted"] = True

    if on_tool_call:
        try:
            await on_tool_call(done_evt)
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
    on_code_output=None,
    current_user_name_override: str | None = None,
    turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Call LLM via unified client with function-calling tool loop."""
    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id)
    if _token_limit_msg:
        return _token_limit_msg
    if max_tool_rounds_override and max_tool_rounds_override < _max_tool_rounds:
        _max_tool_rounds = max_tool_rounds_override

    # Get user's name for personalized context
    if current_user_name_override:
        _user_name = current_user_name_override
    else:
        _user_name = await _get_user_name(user_id)

    # Auto-assign fallback tool call logger if none provided but conversation context exists
    if on_tool_call is None and session_id:
        from app.services.chat_history import persist_tool_call
        async def _default_on_tool_call(data: dict):
            if data.get("status") in {"running", "done"} and agent_id:
                await persist_tool_call(
                    async_session,
                    agent_id=agent_id,
                    user_id=user_id or agent_id,
                    conversation_id=session_id,
                    evt=data,
                    turn_anchor_id=turn_anchor_id,
                )
        on_tool_call = _default_on_tool_call

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context

    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(
        agent_id, agent_name, role_description, current_user_name=_user_name, is_group=is_group
    )

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn — the bootstrap response is a
    # structured templated greeting that never needs to call tools, so we
    # save ~3-5k tokens of prompt and cut TTFT by passing an empty list.
    # Sort by function.name so Anthropic's tools[-1] cache_control lands on a
    # stable tool block across calls — DB iteration-order churn would otherwise
    # invalidate the tools prefix cache every request.
    if skip_tools:
        tools_for_llm = []
    else:
        from app.services.agent_tools import AGENT_TOOLS
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
    _unsaved_usage = TokenUsage()

    # Turn-level latency accounting, logged once at every loop exit so slow
    # turns can be attributed (how many rounds, how long) straight from logs.
    _turn_t0 = perf_counter()

    def _log_turn_timing(outcome: str, rounds: int) -> None:
        logger.info(
            f"[LLM Timing] turn outcome={outcome} rounds={rounds} "
            f"total={perf_counter() - _turn_t0:.2f}s agent={agent_id} session={session_id}"
        )

    # P4: per-call resume counter for max_output_tokens truncation.
    # Bounded across the whole call so a runaway agent cannot turn a long
    # tool loop into 50×3 redundant resumes on a misconfigured cap.
    max_output_recoveries = 0

    # Repeated tool-call guard state: per-signature consecutive-round streaks.
    _repeat_streaks: dict[tuple[str, str], int] = {}

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

        # Check token usage limit mid-loop (every 3 rounds)
        if round_i > 0 and round_i % 3 == 0:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
                _unsaved_usage = TokenUsage()
                _, _token_limit_msg = await _get_agent_config(agent_id)
                if _token_limit_msg:
                    logger.warning(f"[LLM] Token limit exceeded mid-loop: {_token_limit_msg}")
                    await client.close()
                    _log_turn_timing("token_limit", round_i + 1)
                    return _token_limit_msg

        try:
            # Use streaming API for real-time responses
            response = await _stream_with_throttle_retry(
                client,
                model=model,
                round_i=round_i + 1,
                messages=dispatch_messages,
                tools=tools_for_llm if tools_for_llm else None,
                temperature=model.temperature,
                max_tokens=max_tokens,
                on_chunk=on_chunk,
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
                response = await _stream_with_throttle_retry(
                    client,
                    model=model,
                    round_i=round_i + 1,
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
                _log_turn_timing("output_limit", round_i + 1)
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
        except ProviderThrottleExhausted as e:
            logger.error(
                f"[LLM] Provider throttle exhausted: "
                f"provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}"
            )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            _log_turn_timing("throttle_exhausted", round_i + 1)
            return PROVIDER_THROTTLE_USER_MESSAGE
        except LLMError as e:
            logger.error(f"[LLM] LLMError: provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            _log_turn_timing("llm_error", round_i + 1)
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            _log_turn_timing("call_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        # Track tokens for this round
        _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
        _accumulated_usage.add(_usage_this_round)
        _unsaved_usage.add(_usage_this_round)

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

        # Plain assistant text (no tool calls) ends the turn — it IS the reply.
        if not response.tool_calls:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            _log_turn_timing("reply", round_i + 1)
            return response.content or "[LLM returned empty content]"

        # Execute tool calls
        logger.info(f"[LLM] Round {round_i + 1}: {len(response.tool_calls)} tool call(s)")
        sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
        if retry_instruction:
            api_messages.append(LLMMessage(role="user", content=retry_instruction))
            continue

        # request_confirmation handling: valid → SUSPEND the turn on this tool_call (persist
        # a pending request_confirmation tool_call row + deliver the card; the user's click
        # later fills the result and resumes the loop). Platform executes nothing. invalid /
        # tool not enabled → surface error back to model and loop.
        conf_call = find_request_confirmation_call(sanitized_tool_calls)
        if conf_call is not None:
            _conf_action_tool = (conf_call.action or {}).get("tool") if conf_call.action else None
            if conf_call.valid and (_conf_action_tool is None or _conf_action_tool in allowed_tool_names):
                from app.services import confirmation_service  # lazy import — avoid circular
                await confirmation_service.suspend_for_confirmation(
                    agent_id=agent_id,
                    conversation_id=session_id,
                    chat_session_id=None,
                    source_channel="web",
                    user_id=user_id,
                    intro_text=response.content,
                    title=conf_call.title,
                    summary=conf_call.summary,
                    action=conf_call.action,
                    risk_level=conf_call.risk_level,
                    buttons=conf_call.buttons,
                    turn_anchor_id=turn_anchor_id,
                )
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client.close()
                # Turn suspended: the intro text + card are already persisted/delivered.
                # Return "" so the channel handler doesn't re-persist a duplicate reply.
                _log_turn_timing("confirmation_suspended", round_i + 1)
                return ""
            else:
                if not conf_call.valid:
                    _conf_reason = conf_call.error or "request_confirmation 参数无效"
                else:
                    _conf_reason = f"工具 {_conf_action_tool} 未对该 agent 启用,无法挟带"
                # A role="tool" reply must be preceded by the assistant message
                # carrying its tool_calls, or strict providers reject the
                # orphaned tool message next round.
                api_messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=sanitized_tool_calls,
                    reasoning_content=response.reasoning_content,
                ))
                api_messages.append(LLMMessage(
                    role="tool",
                    content=f"❌ {_conf_reason}",
                    tool_call_id=conf_call.call_id,
                ))
                continue

        # Repeated tool-call guard. If the model has now emitted the identical
        # call (name + args) for REPEAT_TOOL_CALL_BREAK consecutive rounds, stop
        # BEFORE appending/executing it — sending a history with that much
        # repetition makes DashScope/qwen 400 ("Repetitive tool calls detected")
        # and crash the whole turn. Breaking here caps history at BREAK-1
        # identical calls, safely under the provider's threshold.
        _round_sigs = [_tool_call_signature(tc) for tc in (sanitized_tool_calls or [])]
        _repeat_streaks = _update_repeat_streaks(_repeat_streaks, _round_sigs)
        _max_repeat = max(_repeat_streaks.values(), default=0)
        if _max_repeat >= REPEAT_TOOL_CALL_BREAK:
            logger.warning(
                f"[LLM] Repeated tool-call guard tripped (streak={_max_repeat}, "
                f"round {round_i + 1}, agent={agent_id}); stopping loop gracefully."
            )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            _log_turn_timing("repeat_guard", round_i + 1)
            return response.content or REPEAT_TOOL_CALL_BREAK_MESSAGE

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

        running_events: list[dict] = []
        for tc in sanitized_tool_calls or []:
            args = _canonicalize_tc_arguments(tc, session_id)
            tool_name = tc["function"]["name"]
            should_execute, _error_msg = _check_tool_requires_args(tool_name, args)
            if not should_execute or tool_name not in allowed_tool_names:
                continue
            running_events.append(
                {
                    "name": tool_name,
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "running",
                    "reasoning_content": full_reasoning_content,
                }
            )

        try:
            running_persisted = await _persist_tool_call_events_strict(
                running_events,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
            )
        except Exception as e:
            logger.exception(f"[LLM] Failed to persist running tool markers before execution: {e}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            _log_turn_timing("tool_marker_persist_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        if running_persisted:
            for evt in running_events:
                evt["_durable_persisted"] = True

        for evt in running_events:
            if on_tool_call:
                try:
                    await on_tool_call(evt)
                except Exception:
                    pass

        for tc in sanitized_tool_calls or []:
            try:
                tool_error = await _process_tool_call(
                    tc=tc,
                    api_messages=api_messages,
                    agent_id=agent_id,
                    user_id=user_id,
                    session_id=session_id,
                    supports_vision=supports_vision,
                    on_tool_call=on_tool_call,
                    on_code_output=on_code_output,
                    full_reasoning_content=full_reasoning_content,
                    allowed_tool_names=allowed_tool_names,
                    emit_running=False,
                    turn_anchor_id=turn_anchor_id,
                )
            except Exception as e:
                logger.exception(f"[LLM] Tool execution or durable result persistence failed: {e}")
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client.close()
                _log_turn_timing("tool_result_persist_error", round_i + 1)
                return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
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

        # Repeated tool-call nudge. On the 2nd identical-call round, append one
        # corrective message (append-only → prefix cache stays intact) so the
        # model gets a chance to change approach or answer before the guard
        # above hard-stops it on the 3rd.
        if _max_repeat == REPEAT_TOOL_CALL_NUDGE:
            api_messages.append(LLMMessage(role="user", content=REPEAT_TOOL_CALL_NUDGE_PROMPT))

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
    if agent_id and _unsaved_usage.total_tokens > 0:
        await record_token_usage(agent_id, _unsaved_usage)
    await client.close()
    _log_turn_timing("round_limit", _max_tool_rounds)
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
    on_code_output=None,
    current_user_name_override: str | None = None,
    turn_anchor_id: uuid.UUID | None = None,
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
        if data.get("status") in {"running", "done"}:
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
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        turn_anchor_id=turn_anchor_id,
    )

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        # A non-error result is just a normal reply — no failover needed, and
        # nothing to warn about. Only a genuine error string that classifies as
        # non-retryable is worth surfacing.
        if is_error_result(primary_result):
            logger.warning(f"[Failover] Skipped: primary model returned a non-retryable error: {primary_result[:150]}")
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
        if data.get("status") in {"running", "done"}:
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
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        turn_anchor_id=turn_anchor_id,
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
            turn_anchor_id=None,
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

    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
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
            # Repeated tool-call guard state: per-signature consecutive-round streaks.
            _repeat_streaks: dict[tuple[str, str], int] = {}
            for round_i in range(max_rounds):
                # Check token usage limit mid-loop (every 3 rounds)
                if round_i > 0 and round_i % 3 == 0:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                        _unsaved_usage = TokenUsage()
                        _, _token_limit_msg = await _get_agent_config(agent_id)
                        if _token_limit_msg:
                            logger.warning(f"[call_agent_llm_with_tools] Token limit exceeded mid-loop: {_token_limit_msg}")
                            await client.close()
                            return _token_limit_msg, False, tool_executed

                try:
                    _round_t0 = perf_counter()
                    response = await client.complete(
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                    )
                    logger.info(
                        f"[LLM Timing] round={round_i + 1} model={getattr(model, 'model', '?')} "
                        f"llm_call={perf_counter() - _round_t0:.2f}s (complete) agent={agent_id}"
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client.close()
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    raise

                # Track tokens for this round
                _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
                _accumulated_usage.add(_usage_this_round)
                _unsaved_usage.add(_usage_this_round)

                # Plain assistant text (no tool calls) ends the turn — it IS the reply.
                if not response.tool_calls:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client.close()
                    return response.content or "[Empty response]", True, tool_executed

                # Execute tool calls
                # Sanitize first — invalid tool args become a retry user message
                # (mirrors the streaming path's behavior in the main _try_model).
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue

                # request_confirmation handling (twin of the main call_llm loop):
                # valid → SUSPEND the turn on this tool_call (pending row + card; resumed on
                # the user's click); invalid / tool not enabled → surface error and loop.
                conf_call = find_request_confirmation_call(sanitized_tool_calls)
                if conf_call is not None:
                    _conf_action_tool = (conf_call.action or {}).get("tool") if conf_call.action else None
                    if conf_call.valid and (_conf_action_tool is None or _conf_action_tool in allowed_tool_names):
                        from app.services import confirmation_service  # lazy import — avoid circular
                        await confirmation_service.suspend_for_confirmation(
                            agent_id=agent_id,
                            conversation_id=session_id,
                            chat_session_id=None,
                            source_channel="web",
                            user_id=None,
                            intro_text=response.content,
                            title=conf_call.title,
                            summary=conf_call.summary,
                            action=conf_call.action,
                            risk_level=conf_call.risk_level,
                            buttons=conf_call.buttons,
                            turn_anchor_id=None,
                        )
                        if agent_id and _unsaved_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _unsaved_usage)
                        await client.close()
                        # Suspended: intro text + card already persisted/delivered. Return ""
                        # so the channel handler doesn't re-persist a duplicate reply.
                        return "", True, True
                    else:
                        if not conf_call.valid:
                            _conf_reason = conf_call.error or "request_confirmation 参数无效"
                        else:
                            _conf_reason = f"工具 {_conf_action_tool} 未对该 agent 启用,无法挟带"
                        # A role="tool" reply must be preceded by the assistant
                        # message carrying its tool_calls, or strict providers
                        # reject the orphaned tool message.
                        api_messages.append(LLMMessage(
                            role="assistant",
                            content=response.content or None,
                            tool_calls=sanitized_tool_calls,
                            reasoning_content=response.reasoning_content,
                        ))
                        api_messages.append(LLMMessage(
                            role="tool",
                            content=f"❌ {_conf_reason}",
                            tool_call_id=conf_call.call_id,
                        ))
                        continue

                # Repeated tool-call guard (twin of the streaming call_llm loop).
                # Stop before re-issuing the identical call for the Nth
                # consecutive round so the provider never 400s on "Repetitive
                # tool calls detected" and crashes this turn.
                _round_sigs = [_tool_call_signature(tc) for tc in (sanitized_tool_calls or [])]
                _repeat_streaks = _update_repeat_streaks(_repeat_streaks, _round_sigs)
                _max_repeat = max(_repeat_streaks.values(), default=0)
                if _max_repeat >= REPEAT_TOOL_CALL_BREAK:
                    logger.warning(
                        f"[call_agent_llm_with_tools] Repeated tool-call guard tripped "
                        f"(streak={_max_repeat}, round {round_i + 1}, agent={agent_id})."
                    )
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client.close()
                    return response.content or REPEAT_TOOL_CALL_BREAK_MESSAGE, True, tool_executed

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
                        _tool_t0 = perf_counter()
                        result = await execute_tool(
                            tool_name,
                            args,
                            agent_id=agent_id,
                            user_id=agent.creator_id,
                            session_id=session_id,
                        )
                        logger.info(
                            f"[LLM Timing] tool={tool_name} exec={perf_counter() - _tool_t0:.2f}s agent={agent_id}"
                        )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tc["id"],
                            content=str(result),
                        )
                    )

                # Repeated tool-call nudge (2nd identical round): one corrective
                # message so the model can change approach or answer before the
                # guard above hard-stops it on the 3rd.
                if _max_repeat == REPEAT_TOOL_CALL_NUDGE:
                    api_messages.append(LLMMessage(role="user", content=REPEAT_TOOL_CALL_NUDGE_PROMPT))

            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return "[Error] Too many tool call rounds", False, tool_executed

        except Exception as e:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
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
