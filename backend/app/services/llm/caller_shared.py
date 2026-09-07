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

import inspect
import json
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.services.conversation_execution_lock import serialize_conversation_execution

# NOTE: agent_tools imports are deferred to function bodies to avoid circular
# import: agent_tools → llm/__init__ → caller → agent_tools


async def get_agent_tools_for_llm(*args, **kwargs):
    from app.services.agent_tools import get_agent_tools_for_llm as _impl

    return await _impl(*args, **kwargs)


async def execute_tool(*args, **kwargs):
    from app.services.agent_tools import execute_tool as _impl

    return await _impl(*args, **kwargs)


from app.services.active_turns import ensure_active_turn
from app.services.token_tracker import (
    TokenUsage,
    extract_token_usage,
    record_token_usage,
)

from .client import LLMClientCloseGuard, LLMError, ModelResponseIdleTimeout
from .confirmation_tool import find_request_confirmation_call
from .failover import FailoverErrorType, classify_error
from .json_recovery import canonicalize_tool_arguments
from .tool_output_store import ToolOutputRewrite, enforce_message_budget, finalize_tool_output
from .utils import LLMMessage, create_llm_client, get_max_tokens, get_model_api_key
from .failure_outcome import LLMFailure, make_llm_failure, model_response_idle_timeout_failure
from .provider_retry import *  # noqa: F401,F403

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


def _latest_visible_response_segment(*segments: str | None) -> str:
    """Return the latest non-blank model text without merging tool rounds."""
    return next(
        (segment for segment in reversed(segments) if segment and segment.strip()),
        "",
    )


async def _invoke_before_round(
    before_round,
    round_i: int,
    *,
    before_injection=None,
) -> list[dict]:
    """Invoke a round hook with an optional durable pre-claim callback.

    Production IM/Subagent drains accept ``before_injection`` and call it only
    after they find work but before committing the injected state. Legacy test
    and extension hooks keep their one-argument contract; for those, persist as
    soon as a non-empty result is observed, still before provider continuation.
    """

    if before_round is None:
        return []
    supports_callback = False
    if before_injection is not None:
        try:
            parameters = inspect.signature(before_round).parameters.values()
            supports_callback = any(
                parameter.name == "before_injection"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_callback = False
    if supports_callback:
        return list(
            await before_round(
                round_i,
                before_injection=before_injection,
            )
            or []
        )
    injected = list(await before_round(round_i) or [])
    if injected and before_injection is not None:
        await before_injection()
    return injected


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


def _as_model_response_idle_timeout(error: BaseException) -> ModelResponseIdleTimeout | None:
    """Normalize a nested HTTP read timeout without classifying other failures."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ModelResponseIdleTimeout):
            return current
        if isinstance(current, httpx.ReadTimeout):
            return ModelResponseIdleTimeout()
        current = current.__cause__ or current.__context__
    return None


def _llm_failure_details(
    *,
    model,
    round_number: int,
    error: BaseException | None = None,
    messages: list | None = None,
    had_tool_side_effect: bool = False,
) -> dict[str, Any]:
    """Build a bounded diagnostic projection without request content or URLs."""
    details: dict[str, Any] = {
        "provider": str(getattr(model, "provider", "") or "")[:64],
        "model": str(getattr(model, "model", "") or "")[:128],
        "model_record_id": str(getattr(model, "id", "") or "")[:64],
        "round": round_number,
        "had_tool_side_effect": had_tool_side_effect,
    }
    if error is not None:
        for output_key, attribute in (
            ("http_status", "status_code"),
            ("provider_error_code", "error_code"),
            ("provider_error_type", "error_type"),
            ("provider_request_id", "request_id"),
        ):
            value = getattr(error, attribute, None)
            if value is not None and value != "":
                details[output_key] = value
        details["had_provider_progress"] = bool(getattr(error, "had_progress", False))

    attachment_count = 0
    attachment_mimes: set[str] = set()
    attachment_bytes = 0
    for message in (messages or [])[:256]:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for part in content[:64]:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            attachment_count += 1
            raw_url = (part.get("image_url") or {}).get("url", "")
            if not isinstance(raw_url, str) or not raw_url.startswith("data:"):
                continue
            header, separator, encoded = raw_url.partition(",")
            mime = header[5:].split(";", 1)[0]
            if mime:
                attachment_mimes.add(mime[:64])
            if separator:
                attachment_bytes += (len(encoded) * 3) // 4
    details.update(
        {
            "attachment_count": attachment_count,
            "attachment_mimes": ",".join(sorted(attachment_mimes))[:256],
            "attachment_bytes": attachment_bytes,
        }
    )
    return details


def _provider_failure_outcome(
    *,
    model,
    round_number: int,
    error: LLMError,
    messages: list | None,
    had_tool_side_effect: bool,
) -> str:
    if isinstance(error, ProviderThrottleExhausted):
        details = _llm_failure_details(
            model=model,
            round_number=round_number,
            error=error,
            messages=messages,
            had_tool_side_effect=had_tool_side_effect,
        )
        details.update(
            {
                "retry_count": error.retry_count,
                "total_requests": error.retry_count + 1,
                "recovery_action": "continue",
            }
        )
        return make_llm_failure(
            code="provider_rate_limit_exhausted",
            message_key="errors.providerRateLimitExhausted",
            retryable=False,
            allow_failover=False,
            details=details,
        )
    exhausted = isinstance(error, ProviderRecoveryExhausted)
    permanently_blocked = error.status_code in {401, 403} or _is_hard_quota_error(error)
    retryable = (
        classify_error(error) == FailoverErrorType.RETRYABLE
        and not exhausted
        and not permanently_blocked
    )
    return make_llm_failure(
        code="provider_request_failed",
        message_key="errors.providerRequestFailed",
        retryable=retryable,
        allow_failover=retryable,
        details=_llm_failure_details(
            model=model,
            round_number=round_number,
            error=error,
            messages=messages,
            had_tool_side_effect=had_tool_side_effect,
        ),
    )


def _combined_model_failure(primary: str, fallback: str) -> str:
    if not isinstance(fallback, LLMFailure):
        return fallback
    details = dict(fallback.details)
    details["primary_error_code"] = getattr(primary, "code", "unknown")
    if isinstance(primary, LLMFailure):
        for key, value in primary.details.items():
            details[f"primary_{key}"] = value
    return make_llm_failure(
        code="provider_failover_failed",
        message_key="errors.providerFailoverFailed",
        details=details,
    )


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

PROVIDER_CONTEXT_BLOCKED_MESSAGE = "上下文过长，请新开会话。"


@dataclass(frozen=True)
class DispatchBudget:
    physical_context_window: int
    context_usage_ratio: float
    effective_context_window: int
    input_capacity: int
    safe_input_limit: int
    token_overflow: bool
    authoritative_prompt_tokens: int | None = None
    count_source: str = "unavailable"
    provider_overflow: bool = False
    keep_recent_turns_override: int | None = None

    @property
    def fits(self) -> bool:
        return not self.token_overflow

    @property
    def physical_input_capacity(self) -> int:
        """Compatibility alias for older logs and callers."""
        return self.input_capacity

    @property
    def reason(self) -> str:
        if self.provider_overflow:
            return "provider_context_rejection"
        if self.token_overflow:
            return "token_limit"
        return "fits"


def measure_dispatch(
    *,
    model,
    messages: list,
    tools: list[dict] | None,
    max_output_tokens: int,
    authoritative_prompt_tokens: int | None = None,
    count_source: str = "unavailable",
) -> DispatchBudget:
    """Measure request shape without pretending text size is model tokens.

    ``authoritative_prompt_tokens`` may only come from an official counter or
    from the provider usage for the same/previous completed request.  The local
    estimate is observability/internal-summary data and never a token hard gate.
    """
    from app.services.llm.context_budget import resolve_context_budget

    context = resolve_context_budget(model, max_output_tokens=max_output_tokens)
    safe_input_limit = context.hard_input_limit
    token_overflow = context.configured and (
        authoritative_prompt_tokens is not None
        and (safe_input_limit <= 0 or authoritative_prompt_tokens > safe_input_limit)
    )
    return DispatchBudget(
        physical_context_window=context.physical_context_window,
        context_usage_ratio=context.context_usage_ratio,
        effective_context_window=context.effective_context_window,
        input_capacity=context.input_capacity,
        safe_input_limit=safe_input_limit,
        token_overflow=token_overflow,
        authoritative_prompt_tokens=authoritative_prompt_tokens,
        count_source=count_source,
    )


async def _guard_provider_dispatch(
    *,
    model,
    messages: list,
    tools: list[dict] | None,
    max_output_tokens: int,
    session_id: str,
) -> str | None:
    """Block oversized provider I/O without mutating persistent session state.

    This final guard is deliberately stateless. It covers system prompts, tool
    schemas, provider-specific character limits, and estimation variance while
    leaving a safe fallback model eligible when no output or tool side effect
    has occurred.
    """
    budget = measure_dispatch(
        model=model,
        messages=messages,
        tools=tools,
        max_output_tokens=max_output_tokens,
    )
    if budget.fits:
        return None

    reason = (
        f"provider_dispatch_oversized:physical_context_window={budget.physical_context_window},"
        f"context_usage_ratio={budget.context_usage_ratio},"
        f"effective_context_window={budget.effective_context_window},"
        f"input_capacity={budget.input_capacity},"
        f"safe_input_limit={budget.safe_input_limit},reason={budget.reason},"
        f"provider={getattr(model, 'provider', '?')},"
        f"model={getattr(model, 'model', '?')}"
    )
    logger.error(f"[context_guard] blocked provider dispatch session={session_id} {reason}")
    return PROVIDER_CONTEXT_BLOCKED_MESSAGE


__all__ = [name for name in globals() if name != "__all__" and not name.startswith("__")]
