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
import inspect
import json
import os
import uuid
from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any

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

from .client import LLMClientCloseGuard, LLMError
from .confirmation_tool import find_request_confirmation_call
from .failover import FailoverErrorType, classify_error
from .json_recovery import canonicalize_tool_arguments
from .tool_output_store import ToolOutputRewrite, enforce_message_budget, finalize_tool_output
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


def _join_visible_response_segments(*segments: str | None) -> str:
    """Join model text emitted across the tool rounds of one logical turn."""
    return "\n\n".join(segment for segment in segments if segment and segment.strip())


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

PROVIDER_THROTTLE_RETRY_DELAYS = (1.0, 2.0)
# DashScope compatible endpoints can silently queue requests when several
# project Agents wake at once.  Bound provider I/O separately from the
# Subagent worker pool so waiting for a slot does not consume the model's
# request timeout.  Operators with a higher provider quota can raise this
# without changing the durable project queue semantics.
PROVIDER_MAX_IN_FLIGHT_ENV = "CLAWITH_LLM_PROVIDER_MAX_IN_FLIGHT"
PROVIDER_MAX_IN_FLIGHT_DEFAULT = 2
PROVIDER_THROTTLE_USER_MESSAGE = "⚠️ 模型服务当前繁忙或被限流，已自动重试仍未成功，请稍后再试。"

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


async def _close_cancelled_provider_client(client) -> None:
    """Best-effort close without allowing cleanup to replace cancellation."""
    close_task = asyncio.create_task(client.close())

    def _consume_close_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException as exc:  # cleanup must never mask cancellation
            logger.warning(f"[LLM] cancelled provider client close failed (ignored): {exc}")

    close_task.add_done_callback(_consume_close_result)
    try:
        await asyncio.shield(close_task)
    except BaseException:
        # A repeated cancel must still propagate the original cancellation;
        # the shielded close task continues and its callback consumes errors.
        pass


_provider_slots: dict[tuple[int, str, str, int, int], asyncio.Semaphore] = {}


def _provider_slot(model) -> asyncio.Semaphore:
    """Return one event-loop-local slot pool for a provider endpoint."""
    try:
        limit = int(os.environ.get(PROVIDER_MAX_IN_FLIGHT_ENV, PROVIDER_MAX_IN_FLIGHT_DEFAULT))
    except ValueError:
        limit = PROVIDER_MAX_IN_FLIGHT_DEFAULT
    limit = max(1, limit)
    loop_id = id(asyncio.get_running_loop())
    key = (
        loop_id,
        str(getattr(model, "provider", "") or "").lower(),
        str(getattr(model, "base_url", "") or ""),
        # Separate provider accounts without retaining or logging credential
        # material in the slot key. Cloned models sharing one stored credential
        # intentionally share capacity.
        hash(str(getattr(model, "api_key_encrypted", "") or "")),
        limit,
    )
    return _provider_slots.setdefault(key, asyncio.Semaphore(limit))


async def _stream_with_throttle_retry(client, *, model, round_i: int, **stream_kwargs):
    throttle_attempt_idx = 0
    provider_slot = _provider_slot(model)

    while True:
        first_progress_at: list[float] = []
        _t0 = perf_counter()
        dispatch_started_at = _t0
        attempt_kwargs = dict(stream_kwargs)
        try:
            # Queueing is admission control only. It does not impose a
            # response lifetime on an accepted production turn.
            async with provider_slot:
                dispatch_started_at = perf_counter()

                def _wrap_progress(cb, *, progress_marks=first_progress_at):
                    async def _marked(*args, **kwargs):
                        if not progress_marks:
                            progress_marks.append(perf_counter())
                        if cb is not None:
                            return await cb(*args, **kwargs)

                    return _marked

                # Always observe meaningful model deltas, even when the
                # transport adapter has no UI callback. This gates retries and
                # prevents duplicate text/tool state after streaming starts.
                for callback_key in ("on_chunk", "on_thinking", "on_tool_delta"):
                    attempt_kwargs[callback_key] = _wrap_progress(attempt_kwargs.get(callback_key))
                response = await client.stream(**attempt_kwargs)
            _elapsed = perf_counter() - _t0
            _ttft = f"{first_progress_at[0] - dispatch_started_at:.2f}s" if first_progress_at else "n/a"
            _usage = getattr(response, "usage", None)
            _out_tokens = _usage.get("completion_tokens") if isinstance(_usage, dict) else None
            _rate = f" ({_out_tokens / _elapsed:.0f} tok/s)" if _out_tokens and _elapsed > 0 else ""
            logger.info(
                f"[LLM Timing] round={round_i} model={getattr(model, 'model', '?')} "
                f"llm_call={_elapsed:.2f}s ttft={_ttft} output_tokens={_out_tokens}{_rate}"
            )
            return response
        except asyncio.CancelledError:
            await _close_cancelled_provider_client(client)
            raise
        except LLMError as e:
            if first_progress_at or not _is_provider_throttle_error(e):
                raise
            if throttle_attempt_idx >= len(PROVIDER_THROTTLE_RETRY_DELAYS):
                raise ProviderThrottleExhausted(str(e)) from e

            delay = PROVIDER_THROTTLE_RETRY_DELAYS[throttle_attempt_idx]
            throttle_attempt_idx += 1
            logger.warning(
                f"[LLM] Provider throttle; retrying after {delay:.1f}s "
                f"(attempt {throttle_attempt_idx + 1}/"
                f"{len(PROVIDER_THROTTLE_RETRY_DELAYS) + 1}, round {round_i}, "
                f"provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')}): {e}"
            )
            await _sleep_before_throttle_retry(delay)


async def _complete_with_throttle_retry(client, *, model, round_i: int, **complete_kwargs):
    """Run a non-streaming provider request through the shared capacity gate.

    Background, scheduled, and project turns use ``complete`` while Web Chat
    uses ``stream``. Both paths must share the same provider-account limit;
    otherwise a project burst can bypass admission and starve interactive
    conversations. Waiting for a provider slot does not hold a database
    transaction or impose a response lifetime.
    """

    throttle_attempt_idx = 0
    provider_slot = _provider_slot(model)

    while True:
        queued_at = perf_counter()
        dispatch_started_at = queued_at
        try:
            async with provider_slot:
                dispatch_started_at = perf_counter()
                response = await client.complete(**complete_kwargs)
            elapsed = perf_counter() - dispatch_started_at
            logger.info(
                f"[LLM Timing] round={round_i} model={getattr(model, 'model', '?')} "
                f"queue={dispatch_started_at - queued_at:.2f}s llm_call={elapsed:.2f}s (complete)"
            )
            return response
        except asyncio.CancelledError:
            await _close_cancelled_provider_client(client)
            raise
        except LLMError as exc:
            if not _is_provider_throttle_error(exc):
                raise
            if throttle_attempt_idx >= len(PROVIDER_THROTTLE_RETRY_DELAYS):
                raise ProviderThrottleExhausted(str(exc)) from exc

            delay = PROVIDER_THROTTLE_RETRY_DELAYS[throttle_attempt_idx]
            throttle_attempt_idx += 1
            logger.warning(
                f"[LLM] Provider complete throttled; retrying after {delay:.1f}s "
                f"(attempt {throttle_attempt_idx + 1}/"
                f"{len(PROVIDER_THROTTLE_RETRY_DELAYS) + 1}, round {round_i}, "
                f"provider={getattr(model, 'provider', '?')} "
                f"model={getattr(model, 'model', '?')}): {exc}"
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
