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
    return result == PROVIDER_CONTEXT_BLOCKED_MESSAGE or result.startswith(
        ("[LLM Error]", "[LLM call error]", "[Error]")
    )


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.

    Uses unified classification from failover.py.
    """
    if not is_error_result(result):
        return False

    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _same_model_record(primary_model, fallback_model) -> bool:
    """True only for the same configured DB model record.

    Two records with the same provider/model name may intentionally use a
    different endpoint or credential and remain a valid fallback.
    """
    if primary_model is None or fallback_model is None:
        return False
    primary_id = getattr(primary_model, "id", None)
    fallback_id = getattr(fallback_model, "id", None)
    return primary_id is not None and fallback_id is not None and primary_id == fallback_id


def _get_model_timeout(model: "LLMModel") -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _usage_from_response(response) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    # Missing provider usage is unknown. Never synthesize input tokens from
    # text length, bytes, image placeholders, or a local tokenizer.
    return TokenUsage()


def _authoritative_usage_details(raw_usage: dict | None) -> dict:
    """Return provider modality/cache detail fields without inventing values."""
    if not isinstance(raw_usage, dict):
        return {}
    details: dict = {}
    for key in (
        "prompt_tokens_details",
        "input_tokens_details",
        "completion_tokens_details",
        "output_tokens_details",
        "usageMetadata",
    ):
        value = raw_usage.get(key)
        if isinstance(value, dict):
            details[key] = value
    return details


def _is_provider_context_overflow(error: Exception) -> bool:
    """Recognize explicit provider context-limit rejections only.

    This intentionally excludes generic HTTP 400 responses.  Retrying an
    authentication/schema/safety error after compaction would be both wasteful
    and capable of masking the real failure.
    """
    structured = " ".join(
        str(value or "").lower()
        for value in (
            getattr(error, "error_code", None),
            getattr(error, "error_type", None),
        )
    )
    structured_markers = (
        "context_length_exceeded",
        "context_window_exceeded",
        "max_tokens_exceeded",
        "request_too_large",
    )
    if any(marker in structured for marker in structured_markers):
        return True
    if getattr(error, "status_code", None) == 413:
        return True

    value = str(error).lower()
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "max context length",
        "context window",
        "input length exceeds",
        "input is too long",
        "too many tokens",
        "reduce the length of the messages",
        "prompt is too long",
    )
    return any(marker in value for marker in markers)


def _coerce_uuid(value) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


_SENSITIVE_RESULT_TOOLS = {"list_installed_mcp_servers"}


def _observable_tool_args(tool_name: str, args: dict[str, Any]) -> str:
    """Return useful tool-call metadata without logging private media URLs."""
    if tool_name != "send_media":
        return json.dumps(args, ensure_ascii=False, default=str)
    summary = {
        "keys": sorted(str(key) for key in args),
        "media_type": args.get("media_type"),
        "source": "url" if args.get("url") else "file_path" if args.get("file_path") else None,
        "url_mode": args.get("url_mode"),
        "has_session_id": bool(args.get("session_id")),
        "has_user_id": bool(args.get("user_id")),
        "channel": args.get("channel"),
        "has_message": bool(args.get("message")),
        "has_cover_image_path": bool(args.get("cover_image_path")),
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


def _observable_tool_result(tool_name: str, result: str) -> str:
    """Mask credential values while preserving the complete diagnostic shape."""
    if tool_name == "send_media":
        try:
            payload = json.loads(result)
            if not isinstance(payload, dict):
                return '{"type": "media_delivery_result", "status": "unparseable"}'
            safe_keys = (
                "type",
                "version",
                "status",
                "code",
                "media_kind",
                "source_mode",
                "channel",
                "http_status",
                "actual_kind",
                "size",
                "mime_type",
                "caption_sent",
            )
            summary = {key: payload[key] for key in safe_keys if key in payload}
            summary["has_url"] = bool(payload.get("url"))
            summary["has_path"] = bool(payload.get("path") or payload.get("managed_path"))
            return json.dumps(summary, ensure_ascii=False, default=str)
        except Exception:
            return '{"type": "media_delivery_result", "status": "unparseable"}'
    if tool_name not in _SENSITIVE_RESULT_TOOLS:
        return result
    try:
        from app.utils.sanitize import sanitize_sensitive_values

        payload = json.loads(result)
        return json.dumps(
            sanitize_sensitive_values(payload),
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        return result


def _send_media_result_is_durable_in_current_session(tool_name: str, result: str, session_id: str) -> bool:
    """True when send_media already updated the current Session's tool row."""
    if tool_name != "send_media":
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("type")
        in {
            "platform_media_delivery",
            "media_delivery_result",
        }
        and payload.get("status")
        in {
            "sent",
            "already_sent",
            "failed",
            "unsupported",
            "unknown",
        }
        and str(payload.get("session_id") or "") == str(session_id or "")
        and str(payload.get("message_id") or "")
    )


def _durable_tool_result_row_id(tool_name: str, result: str, session_id: str) -> uuid.UUID | None:
    """Resolve a tool-owned durable result row when the tool persisted it itself."""
    if not _send_media_result_is_durable_in_current_session(tool_name, result, session_id):
        return None
    try:
        payload = json.loads(result)
        return uuid.UUID(str(payload.get("message_id") or ""))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


@dataclass
class _RoundDoneToolCall:
    event: dict[str, Any]
    row_id: uuid.UUID | None


async def _persist_tool_call_events_strict(
    events: list[dict],
    *,
    agent_id,
    user_id,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> dict[str, uuid.UUID]:
    """Durably append tool-call markers before the loop relies on them.

    Returns an empty mapping for non-persistable test/background calls without UUID ids.
    For real chat turns, DB errors propagate and stop execution before side
    effects can happen without a recovery marker.
    """
    if not events or not session_id:
        return {}
    agent_uuid = _coerce_uuid(agent_id)
    user_uuid = _coerce_uuid(user_id)
    if agent_uuid is None or user_uuid is None:
        return {}

    from app.services.chat_history import persist_tool_call_row
    from app.services.conversation_turn_lifecycle import (
        lock_conversation_turn_running,
    )

    persisted: dict[str, uuid.UUID] = {}
    async with async_session() as db:
        await lock_conversation_turn_running(
            db,
            agent_id=agent_uuid,
            conversation_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        for evt in events:
            row_id = await persist_tool_call_row(
                db,
                agent_id=agent_uuid,
                user_id=user_uuid,
                conversation_id=session_id,
                evt=evt,
                turn_anchor_id=turn_anchor_id,
                turn_fence_locked=True,
            )
            if row_id is not None:
                persisted[str(evt.get("call_id") or "")] = row_id
        await db.commit()
    return persisted


async def _reconcile_round_tool_outputs(
    rewrites: list[ToolOutputRewrite],
    done_records: list[_RoundDoneToolCall],
    *,
    agent_id,
    user_id,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> None:
    """Make durable done rows match the fresh provider transcript exactly."""
    if not rewrites:
        return

    records_by_call_id: dict[str, _RoundDoneToolCall] = {}
    for record in done_records:
        call_id = str(record.event.get("call_id") or "")
        if not call_id or call_id in records_by_call_id:
            raise RuntimeError(f"ambiguous durable tool result call_id: {call_id!r}")
        records_by_call_id[call_id] = record

    final_by_call_id: dict[str, str] = {}
    for rewrite in rewrites:
        if not rewrite.tool_call_id or rewrite.tool_call_id in final_by_call_id:
            raise RuntimeError(f"ambiguous tool output rewrite call_id: {rewrite.tool_call_id!r}")
        if rewrite.tool_call_id not in records_by_call_id:
            raise RuntimeError(f"tool output rewrite has no done result: {rewrite.tool_call_id!r}")
        final_by_call_id[rewrite.tool_call_id] = rewrite.final_content

    agent_uuid = _coerce_uuid(agent_id)
    user_uuid = _coerce_uuid(user_id)
    durable_turn = bool(session_id and agent_uuid is not None and user_uuid is not None)
    if durable_turn:
        replacements: dict[uuid.UUID, tuple[str, str]] = {}
        for call_id, final_content in final_by_call_id.items():
            row_id = records_by_call_id[call_id].row_id
            if row_id is None:
                raise RuntimeError(f"durable tool result row is missing for call_id={call_id!r}")
            replacements[row_id] = (call_id, final_content)

        from app.services.chat_history import rewrite_tool_call_done_results

        async with async_session() as db:
            await rewrite_tool_call_done_results(
                db,
                agent_id=agent_uuid,
                user_id=user_uuid,
                conversation_id=session_id,
                replacements=replacements,
                turn_anchor_id=turn_anchor_id,
            )
            await db.commit()

    # Mutate callback payloads only after the durable transaction commits. On
    # failure they retain the original values that are still authoritative in DB.
    for call_id, final_content in final_by_call_id.items():
        records_by_call_id[call_id].event["result"] = final_content


async def _emit_round_done_events(
    done_records: list[_RoundDoneToolCall],
    on_tool_call,
) -> None:
    if on_tool_call is None:
        return
    for record in done_records:
        try:
            await on_tool_call(record.event)
        except Exception:
            pass


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
        from app.models.agent import Agent as _AgentModel
        from app.models.user import User as _UserModel

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
    import copy
    import re as _re_v

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


def _attach_turn_context(api_messages: list, dynamic_prompt: str | None) -> list:
    """Attach one immutable runtime-context snapshot to the current turn.

    Layout after injection:
        last_user.content = f"<context>\\n{dynamic_prompt}\\n</context>\\n\\n{original}"

    The returned list becomes the caller's canonical in-memory message list for
    the complete tool loop.  Every later LLM request therefore sees the same
    memory/runtime snapshot, while assistant/tool messages are appended after
    it without changing any previously-dispatched bytes.

    Why wrap only a *tail* user instead of searching history?
    - A tail user is the current turn and can safely receive volatile context.
    - A non-user tail means the execution is resuming from assistant/tool
      history (for example after confirmation); rewriting an older user would
      corrupt persisted-history semantics and invalidate prefix caching.
    - Leaves the stable system + persisted conversation prefix untouched.
    - Places turn-volatile data at the tail, after the cacheable history.
    - Does not persist the wrapper to ChatMessage; the next turn rebuilds a
      fresh snapshot around its own current user message.

    When there is no user message, append a context-only user message so
    unattended executions still receive the snapshot.  With no dynamic
    context, return a shallow copy unchanged.  The input list is never mutated.

    The returned list contains fresh ``LLMMessage`` instances for any message
    we modify, so the caller's ``api_messages`` stays byte-identical for the
    next round's cache prefix.
    """
    out = list(api_messages)
    if not dynamic_prompt:
        return out

    # Only the final message can be the current user turn.  Confirmation
    # continuation and other resume paths intentionally end in assistant/tool;
    # append a context-only tail there rather than modifying historical input.
    if not out or out[-1].role != "user":
        out.append(
            LLMMessage(
                role="user",
                content=f"<context>\n{dynamic_prompt}\n</context>",
            )
        )
        return out

    last_user_idx = len(out) - 1
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


async def _build_turn_context(
    *,
    agent_id,
    agent_name: str,
    role_description: str,
    user_id,
    current_user_name_override: str | None,
    is_group: bool,
    session_id: str,
    channel_context: dict | None,
    include_soul: bool = True,
    include_memory: bool = True,
) -> tuple[str, str]:
    """Build the immutable static/dynamic context pair for one logical turn."""
    if current_user_name_override:
        user_name = current_user_name_override
    else:
        user_name = await _get_user_name(user_id)

    from app.services.agent_context import build_agent_context

    runtime_channel_context = dict(channel_context or {})
    if session_id:
        runtime_channel_context["session_id"] = session_id

    return await build_agent_context(
        agent_id,
        agent_name,
        role_description,
        current_user_name=user_name,
        current_user_id=None if current_user_name_override else user_id,
        is_group=is_group,
        channel_context=runtime_channel_context,
        include_soul=include_soul,
        include_memory=include_memory,
    )


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
    tools_for_llm: list[dict] | None = None,
    on_code_output=None,
    emit_running: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
    before_execute=None,
    round_done_records: list[_RoundDoneToolCall] | None = None,
    round_id: str | None = None,
    round_tool_index: int | None = None,
    assistant_content: str | None = None,
    recovery_prefix_messages: list[dict[str, str]] | None = None,
) -> str:
    """Process a single tool call and return result."""
    args = _canonicalize_tc_arguments(tc, session_id)
    tool_name = tc["function"]["name"]
    logger.info(f"[LLM] Calling tool: {tool_name}({_observable_tool_args(tool_name, args)[:500]})")

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        done_evt = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "done",
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "result": error_msg,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "recovery_prefix_messages": recovery_prefix_messages or [],
        }
        persisted = await _persist_tool_call_events_strict(
            [done_evt],
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        row_id = persisted.get(str(done_evt.get("call_id") or "")) if persisted else None
        if persisted:
            done_evt["_durable_persisted"] = True
        record = _RoundDoneToolCall(event=done_evt, row_id=row_id)
        if round_done_records is not None:
            round_done_records.append(record)
        else:
            await _emit_round_done_events([record], on_tool_call)
        return error_msg

    if tool_name not in allowed_tool_names:
        result = _tool_not_enabled_message(tool_name)
        logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
        done_evt = {
            "name": tool_name,
            "call_id": tc.get("id", ""),
            "args": args,
            "status": "done",
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "result": result,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "recovery_prefix_messages": recovery_prefix_messages or [],
        }
        persisted = await _persist_tool_call_events_strict(
            [done_evt],
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        row_id = persisted.get(str(done_evt.get("call_id") or "")) if persisted else None
        if persisted:
            done_evt["_durable_persisted"] = True
        record = _RoundDoneToolCall(event=done_evt, row_id=row_id)
        if round_done_records is not None:
            round_done_records.append(record)
        else:
            await _emit_round_done_events([record], on_tool_call)
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
            "round_id": round_id,
            "round_tool_index": round_tool_index,
            "reasoning_content": full_reasoning_content,
            "assistant_content": assistant_content,
            "recovery_prefix_messages": recovery_prefix_messages or [],
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
    if before_execute is not None:
        await before_execute()
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    _tool_t0 = perf_counter()
    result = await execute_tool(
        tool_name,
        args,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        tool_call_id=str(tc.get("id") or ""),
        turn_anchor_id=turn_anchor_id,
        on_output=_on_output,
        tools_for_llm=tools_for_llm,
    )
    logger.info(f"[LLM Timing] tool={tool_name} exec={perf_counter() - _tool_t0:.2f}s agent={agent_id}")
    observable_result = _observable_tool_result(tool_name, result)
    logger.debug(f"[LLM] Tool result: {observable_result[:100]}")

    # Materialize oversize output and produce the canonical llm_view string.
    # This is the single shape point — DB, WS live stream, historical replay
    # all consume this string, keeping the messages sequence append-only.
    llm_view = await finalize_tool_output(
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
            from app.services.agent_runtime_workspace import current_agent_runtime_workspace
            from app.services.vision_inject import try_inject_screenshot_vision

            ws_path = current_agent_runtime_workspace(agent_id).local_root
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
        "round_id": round_id,
        "round_tool_index": round_tool_index,
        "result": llm_view,
        "reasoning_content": full_reasoning_content,
        "assistant_content": assistant_content,
        "recovery_prefix_messages": recovery_prefix_messages or [],
    }
    done_row_id = _durable_tool_result_row_id(tool_name, str(llm_view), session_id)
    if done_row_id is not None:
        done_evt["_durable_persisted"] = True
    else:
        persisted_done_rows = await _persist_tool_call_events_strict(
            [done_evt],
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        if persisted_done_rows:
            done_evt["_durable_persisted"] = True
            if isinstance(persisted_done_rows, dict):
                done_row_id = persisted_done_rows.get(str(done_evt.get("call_id") or ""))

    done_record = _RoundDoneToolCall(event=done_evt, row_id=done_row_id)
    if round_done_records is not None:
        round_done_records.append(done_record)
    else:
        await _emit_round_done_events([done_record], on_tool_call)

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


@serialize_conversation_execution
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
    on_usage=None,
    max_tool_rounds_override: int | None = None,
    skip_tools: bool = False,
    is_group: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    channel_context: dict | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    turn_anchor_agent_id: uuid.UUID | None = None,
    turn_type: str | None = None,
    prepared_turn_context: tuple[str, str] | None = None,
    prepared_tools: list[dict] | None = None,
    context_recovery=None,
    before_round=None,
    before_tool_execution=None,
    include_soul: bool = True,
    include_memory: bool = True,
) -> str:
    """Call LLM via unified client with function-calling tool loop."""
    # Normalize only the legacy Agent-UUID sentinel. A genuine ``None`` remains
    # anonymous/fail-safe here; durable background entrypoints resolve their
    # missing execution user to the creator before calling this shared layer.
    agent_uuid = _coerce_uuid(agent_id)
    viewer_uuid = _coerce_uuid(user_id)

    async def _assert_durable_turn_running() -> None:
        durable_agent_id = turn_anchor_agent_id or agent_uuid
        if durable_agent_id is None or not session_id or turn_anchor_id is None:
            return
        from app.services.conversation_turn_lifecycle import (
            assert_conversation_turn_running,
        )

        await assert_conversation_turn_running(
            agent_id=durable_agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=turn_anchor_id,
        )

    async def _before_tool_execution_guard() -> None:
        # This is an exact indexed point lookup at a tool boundary, not a
        # timer/poll. It is intentionally repeated between tools so STOP that
        # lands while a previous tool is running fences the next side effect.
        await _assert_durable_turn_running()
        if before_tool_execution is not None:
            await before_tool_execution()

    async def _admit_tool_execution() -> None:
        durable_agent_id = turn_anchor_agent_id or agent_uuid
        if durable_agent_id is not None and session_id and turn_anchor_id is not None:
            from app.services.conversation_turn_lifecycle import (
                admit_conversation_turn_side_effect,
            )

            await admit_conversation_turn_side_effect(
                agent_id=durable_agent_id,
                conversation_id=str(session_id),
                turn_anchor_id=turn_anchor_id,
            )
        if before_tool_execution is not None:
            await before_tool_execution()
    if agent_uuid is not None and viewer_uuid == agent_uuid:
        from app.models.agent import Agent as AgentModel

        async with async_session() as identity_db:
            creator_id = await identity_db.scalar(select(AgentModel.creator_id).where(AgentModel.id == agent_uuid))
        if creator_id is not None:
            user_id = creator_id

    if agent_id and user_id and session_id:
        try:
            owner_uuid = uuid.UUID(str(user_id))
            agent_uuid = uuid.UUID(str(agent_id))
        except (TypeError, ValueError):
            # Keep the long-standing low-level test/adapter contract that allows
            # opaque ids. Production turn entrypoints always supply UUIDs.
            pass
        else:
            await ensure_active_turn(
                owner_user_id=owner_uuid,
                agent_id=agent_uuid,
                session_id=str(session_id),
                turn_type=turn_type,
                turn_anchor_id=turn_anchor_id,
                turn_anchor_agent_id=turn_anchor_agent_id,
            )
    supports_vision = bool(getattr(model, "supports_vision", False))
    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id)
    if _token_limit_msg:
        return _token_limit_msg
    if max_tool_rounds_override is not None:
        _max_tool_rounds = max(1, min(200, int(max_tool_rounds_override)))

    # Auto-assign fallback tool call logger if none provided but conversation context exists
    if on_tool_call is None and session_id:
        from app.services.chat_history import persist_tool_call

        async def _default_on_tool_call(data: dict):
            if data.get("status") in {"running", "done"} and agent_id:
                await persist_tool_call(
                    async_session,
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=session_id,
                    evt=data,
                    turn_anchor_id=turn_anchor_id,
                )

        on_tool_call = _default_on_tool_call

    # Direct callers build their own turn snapshot.  The failover wrapper passes
    # one prepared pair to both providers so a single logical turn cannot cross
    # a memory/date/trigger boundary between primary and fallback attempts.
    if prepared_turn_context is None:
        static_prompt, dynamic_prompt = await _build_turn_context(
            agent_id=agent_id,
            agent_name=agent_name,
            role_description=role_description,
            user_id=user_id,
            current_user_name_override=current_user_name_override,
            is_group=is_group,
            session_id=session_id,
            channel_context=channel_context,
            include_soul=include_soul,
            include_memory=include_memory,
        )
    else:
        static_prompt, dynamic_prompt = prepared_turn_context

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn — the bootstrap response is a
    # structured templated greeting that never needs to call tools, so we
    # save ~3-5k tokens of prompt and cut TTFT by passing an empty list.
    # Sort by function.name so Anthropic's tools[-1] cache_control lands on a
    # stable tool block across calls — DB iteration-order churn would otherwise
    # invalidate the tools prefix cache every request.
    if prepared_tools is not None:
        tools_for_llm = list(prepared_tools)
    elif skip_tools:
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

    # Convert messages to LLMMessage format.  The system message contains only
    # the byte-stable static prompt.  Per-turn context (memory, current user,
    # channel, time, triggers) is attached to the current user message at the
    # tail, preserving the cacheable system + historical prefix.
    async def _assemble_api_messages(source_messages: list[dict]) -> list[LLMMessage]:
        from app.services.image_context import prepare_messages_for_model

        prepared_messages = await prepare_messages_for_model(
            source_messages,
            agent_id=agent_id,
            supports_vision=supports_vision,
        )
        assembled = [LLMMessage(role="system", content=static_prompt)]
        for msg in prepared_messages:
            assembled.append(
                LLMMessage(
                    role=msg.get("role", "user"),
                    content=msg.get("content"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                )
            )
        assembled = _convert_messages_for_vision(assembled, supports_vision)
        return _attach_turn_context(assembled, dynamic_prompt)

    api_messages = await _assemble_api_messages(messages)

    # Create the unified LLM client
    try:
        client = create_llm_client(
            provider=model.provider,
            api_key=get_model_api_key(model),
            model=model.model,
            base_url=model.base_url,
            timeout=_get_model_timeout(model),
            provider_managed_timeout=True,
        )
        client_guard = LLMClientCloseGuard(client)
    except Exception as e:
        return f"[Error] Failed to create LLM client: {e}"

    max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))
    _unsaved_usage = TokenUsage()
    last_authoritative_prompt_tokens: int | None = None
    anchor_agent_id = _coerce_uuid(turn_anchor_agent_id) or _coerce_uuid(agent_id)
    if anchor_agent_id is not None and session_id:
        from app.services.session_token_usage import load_latest_round_context_usage

        try:
            last_authoritative_prompt_tokens = await load_latest_round_context_usage(
                agent_id=anchor_agent_id,
                session_id=session_id,
                provider=str(getattr(model, "provider", "") or ""),
                model=str(getattr(model, "model", "") or ""),
                model_record_id=str(getattr(model, "id", "") or ""),
                endpoint=str(getattr(model, "base_url", "") or ""),
            )
        except Exception as exc:
            logger.error(
                f"[context_usage] latest usage load failed session={session_id}: {exc}"
            )

    async def _track_response_usage(response, usage_messages: list[LLMMessage], round_number: int) -> None:
        nonlocal last_authoritative_prompt_tokens
        usage = _usage_from_response(response)
        _unsaved_usage.add(usage)
        if on_usage is not None:
            try:
                await on_usage(usage)
            except Exception as exc:
                logger.warning(f"[LLM] usage callback failed (ignored): {exc}")

        raw_usage = getattr(response, "usage", None)
        authoritative = extract_token_usage(raw_usage)
        if authoritative is not None and authoritative.context_input_tokens > 0:
            last_authoritative_prompt_tokens = authoritative.context_input_tokens
            if anchor_agent_id is not None and turn_anchor_id is not None and session_id:
                from app.services.session_token_usage import persist_round_context_usage

                try:
                    await persist_round_context_usage(
                        agent_id=anchor_agent_id,
                        session_id=session_id,
                        turn_anchor_id=turn_anchor_id,
                        input_tokens=authoritative.context_input_tokens,
                        output_tokens=authoritative.output_tokens,
                        provider=str(getattr(model, "provider", "") or ""),
                        model=str(getattr(model, "model", "") or ""),
                        model_record_id=str(getattr(model, "id", "") or ""),
                        endpoint=str(getattr(model, "base_url", "") or ""),
                        usage_details=_authoritative_usage_details(raw_usage),
                    )
                except Exception as exc:
                    # Accounting persistence must not erase an otherwise valid
                    # model response; the context guard still uses the exact
                    # in-memory value for this running turn.
                    logger.error(
                        "[context_usage] round usage backfill failed "
                        f"session={session_id} round={round_number}: {exc}"
                    )
        ratio = _cache_hit_ratio(raw_usage)
        if ratio is not None and isinstance(raw_usage, dict):
            prompt_tokens = raw_usage.get("prompt_tokens")
            line = f"[LLM] Round {round_number} cache-hit-ratio={ratio} prompt={prompt_tokens}"
            if ratio < float(os.environ.get("CLAWITH_CACHE_HIT_WARN_RATIO", "0.7")):
                logger.warning(line + " (LOW — prefix cache may be missing the tool-loop tail)")
            else:
                logger.info(line)

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
    # Non-empty model content is user-visible even when the same response also
    # carries tool calls. Keep it until this logical turn finishes or suspends.
    visible_response_segments: list[str] = []
    turn_execution_id = uuid.uuid4().hex
    durable_user_id = _coerce_uuid(user_id)

    async def _persist_intermediate_segment(
        content: str,
        *,
        visible_joiner_before: str,
        max_output_resume_prompt: str | None = None,
        thinking: str | None = None,
        created_at=None,
    ) -> bool:
        """Persist provider output that must survive another model dispatch."""

        if not (content or "").strip():
            return False
        if (
            anchor_agent_id is None
            or durable_user_id is None
            or turn_anchor_id is None
            or not session_id
        ):
            return False
        from app.services.chat_history import persist_intermediate_assistant_reply

        await persist_intermediate_assistant_reply(
            async_session,
            agent_id=anchor_agent_id,
            user_id=durable_user_id,
            conversation_id=session_id,
            content=content,
            thinking=thinking,
            turn_anchor_id=turn_anchor_id,
            visible_joiner_before=visible_joiner_before,
            max_output_resume_prompt=max_output_resume_prompt,
            created_at=created_at,
        )
        return True

    # Tool-calling loop. A subagent may receive parent messages while a model
    # request is in flight. ``skip_before_round_once`` avoids draining the same
    # round twice when the final-reply gate below has already prefetched those
    # messages for the next round.
    skip_before_round_once = False
    preflight_compaction_not_applicable = False
    async def _dispatch_round_with_context_recovery(
        current_messages: list[LLMMessage],
        current_budget: DispatchBudget,
        round_number: int,
    ):
        """Dispatch one logical model round; overflow retry does not consume it."""
        nonlocal api_messages, last_authoritative_prompt_tokens
        while True:
            meaningful_progress = False

            async def _chunk(text: str):
                nonlocal meaningful_progress
                meaningful_progress = meaningful_progress or bool(text)
                if on_chunk is not None:
                    await on_chunk(text)

            async def _thinking(text: str):
                nonlocal meaningful_progress
                meaningful_progress = meaningful_progress or bool(text)
                if on_thinking is not None:
                    await on_thinking(text)

            async def _tool_delta(data: dict):
                nonlocal meaningful_progress
                meaningful_progress = meaningful_progress or bool(data)
                if on_tool_delta is not None:
                    await on_tool_delta(data)

            try:
                response = await _stream_with_throttle_retry(
                    client,
                    model=model,
                    round_i=round_number,
                    messages=current_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    temperature=model.temperature,
                    max_tokens=max_tokens,
                    on_chunk=_chunk,
                    on_tool_delta=_tool_delta,
                    on_thinking=_thinking,
                )
                return response, current_messages, current_budget
            except LLMError as exc:
                if (
                    meaningful_progress
                    or not _is_provider_context_overflow(exc)
                    or context_recovery is None
                ):
                    raise
                overflow_budget = replace(
                    current_budget,
                    provider_overflow=True,
                    token_overflow=True,
                    count_source="provider_context_rejection",
                )
                recovered_messages = await context_recovery(model, overflow_budget)
                if recovered_messages is None:
                    raise
                api_messages = await _assemble_api_messages(recovered_messages)
                current_messages = list(api_messages)
                last_authoritative_prompt_tokens = None
                current_budget = measure_dispatch(
                    model=model,
                    messages=current_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    max_output_tokens=max_tokens,
                )
                logger.warning(
                    "[context_guard] provider rejected context; compacted "
                    f"and retrying the same round session={session_id} "
                    f"model={getattr(model, 'model', '?')}"
                )

    for round_i in range(_max_tool_rounds):
        await _assert_durable_turn_running()
        if skip_before_round_once:
            skip_before_round_once = False
        elif before_round is not None:
            injected = await _invoke_before_round(before_round, round_i)
            if injected:
                from app.services.image_context import prepare_messages_for_model

                prepared_injected = await prepare_messages_for_model(
                    injected,
                    agent_id=agent_id,
                    supports_vision=supports_vision,
                )
                api_messages.extend(
                    LLMMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content"),
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                    )
                    for msg in prepared_injected
                )
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

        # Dispatch a shallow view.  api_messages already contains the immutable
        # turn-context snapshot; later rounds only append assistant/tool items.
        dispatch_messages = list(api_messages)

        # Check token usage limit mid-loop (every 3 rounds)
        if round_i > 0 and round_i % 3 == 0:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
                _unsaved_usage = TokenUsage()
                _, _token_limit_msg = await _get_agent_config(agent_id)
                if _token_limit_msg:
                    logger.warning(f"[LLM] Token limit exceeded mid-loop: {_token_limit_msg}")
                    await client_guard.close()
                    _log_turn_timing("token_limit", round_i + 1)
                    return _token_limit_msg

        dispatch_budget = measure_dispatch(
            model=model,
            messages=dispatch_messages,
            tools=tools_for_llm if tools_for_llm else None,
            max_output_tokens=max_tokens,
            authoritative_prompt_tokens=last_authoritative_prompt_tokens,
            count_source=(
                "previous_provider_usage"
                if last_authoritative_prompt_tokens is not None
                else "unavailable"
            ),
        )
        # Qwen does not expose an official preflight token counter.  Drive
        # compaction from the exact provider usage returned by the preceding
        # round; never reinterpret local text/byte size as model tokens.
        from app.services.llm.compactor import should_compact

        preflight_compaction_required, _, _ = should_compact(
            model=model,
            last_prompt_tokens=last_authoritative_prompt_tokens,
            pre_flight_estimate=None,
        )
        if (
            (not dispatch_budget.fits or preflight_compaction_required)
            and turn_anchor_id is not None
            and context_recovery is not None
        ):
            recovered_messages = await context_recovery(model, dispatch_budget)
            if recovered_messages is not None:
                preflight_compaction_not_applicable = bool(
                    getattr(recovered_messages, "preflight_not_applicable", False)
                )
                api_messages = await _assemble_api_messages(recovered_messages)
                dispatch_messages = list(api_messages)
                # The observed count belongs to the pre-compaction request.
                # The rebuilt request will receive its own authoritative count
                # from the provider response.
                last_authoritative_prompt_tokens = None
                dispatch_budget = measure_dispatch(
                    model=model,
                    messages=dispatch_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    max_output_tokens=max_tokens,
                )

        # A threshold hit based on real usage must lead to a successful
        # compaction/reload before another model round.  Local estimates do not
        # participate in this decision.
        if preflight_compaction_required and not (
            last_authoritative_prompt_tokens is None
            or (preflight_compaction_not_applicable and dispatch_budget.fits)
        ):
            logger.error(
                "[context_guard] required compaction did not produce a safe prompt "
                f"session={session_id} provider_tokens={last_authoritative_prompt_tokens}"
            )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("required_compaction_failed", round_i + 1)
            return PROVIDER_CONTEXT_BLOCKED_MESSAGE

        context_stop = None
        if not dispatch_budget.fits:
            context_stop = await _guard_provider_dispatch(
                model=model,
                messages=dispatch_messages,
                tools=tools_for_llm if tools_for_llm else None,
                max_output_tokens=max_tokens,
                session_id=session_id,
            )
        if context_stop:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("context_blocked", round_i + 1)
            return context_stop

        try:
            # Use streaming API for real-time responses
            response, dispatch_messages, dispatch_budget = (
                await _dispatch_round_with_context_recovery(
                    dispatch_messages,
                    dispatch_budget,
                    round_i + 1,
                )
            )
            await _track_response_usage(response, dispatch_messages, round_i + 1)

            # ── P4: max_output_tokens recovery ────────────────────────────
            # If the response was cut off because we hit the per-call output
            # cap, push the partial text into history as an assistant turn,
            # nudge the model to "continue", and re-stream. Accumulate the
            # partial pieces so the caller still receives the complete text.
            # Every partial/resume pair remains in the canonical transcript:
            # once bytes have been dispatched, later tool rounds may only
            # append to them.
            accumulated_partials: list[str] = []
            recovery_prefix_messages: list[dict[str, str]] = []
            recovery_count_this_round = 0
            durable_partial_count = 0
            round_visible_joiner = "\n\n" if visible_response_segments else ""
            while (
                _response_was_truncated_by_length(response) and max_output_recoveries < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT
            ):
                max_output_recoveries += 1
                recovery_count_this_round += 1
                partial_text = response.content or ""
                partial_persisted = await _persist_intermediate_segment(
                    partial_text,
                    visible_joiner_before=(
                        round_visible_joiner
                        if recovery_count_this_round == 1
                        else ""
                    ),
                    max_output_resume_prompt=RESUME_PROMPT,
                    thinking=response.reasoning_content,
                )
                accumulated_partials.append(partial_text)
                if not partial_persisted:
                    # Non-durable/internal callers retain the historical tool
                    # row fallback. Durable turns replay the committed
                    # intermediate row instead, avoiding a duplicate prefix.
                    recovery_prefix_messages.extend(
                        [
                            {"role": "assistant", "content": partial_text},
                            {"role": "user", "content": RESUME_PROMPT},
                        ]
                    )
                else:
                    durable_partial_count += 1
                # Durable in-turn recovery transcript.  Do not remove these
                # messages after dispatch: a later tool round must retain this
                # request as an exact prefix.
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

                # Rebuild the shallow dispatch view.  The original turn-context
                # snapshot remains in api_messages and is carried into resumes.
                dispatch_messages = list(api_messages)
                context_stop = await _guard_provider_dispatch(
                    model=model,
                    messages=dispatch_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    max_output_tokens=max_tokens,
                    session_id=session_id,
                )
                if context_stop:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client_guard.close()
                    _log_turn_timing("context_blocked", round_i + 1)
                    return context_stop
                resume_budget = measure_dispatch(
                    model=model,
                    messages=dispatch_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    max_output_tokens=max_tokens,
                )
                response, dispatch_messages, resume_budget = (
                    await _dispatch_round_with_context_recovery(
                        dispatch_messages,
                        resume_budget,
                        round_i + 1,
                    )
                )
                await _track_response_usage(response, dispatch_messages, round_i + 1)

            # Still truncated after exhausting the resume budget — surface
            # a clear error. The failover layer will see a [LLM Error] and
            # can decide whether to try the fallback model (which may have
            # a larger output cap).
            if _response_was_truncated_by_length(response):
                logger.error(
                    f"[LLM] Output token limit not recoverable after {MAX_OUTPUT_TOKENS_RECOVERY_LIMIT} resume attempts"
                )
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client_guard.close()
                _log_turn_timing("output_limit", round_i + 1)
                return "[LLM Error] Output token limit exceeded after 3 resume attempts"

            # Keep the provider response as the final continuation segment for
            # message-history correctness.  The assembled value is used only
            # for the user-visible reply/card text.
            complete_response_content = "".join(accumulated_partials) + (response.content or "")
        except ProviderThrottleExhausted as e:
            logger.error(
                f"[LLM] Provider throttle exhausted: "
                f"provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}"
            )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("throttle_exhausted", round_i + 1)
            return PROVIDER_THROTTLE_USER_MESSAGE
        except LLMError as e:
            logger.error(
                f"[LLM] LLMError: provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}"
            )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("llm_error", round_i + 1)
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("call_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        if complete_response_content and complete_response_content.strip():
            visible_response_segments.append(complete_response_content)

        # Plain assistant text normally ends the turn. Before returning, drain
        # the external round inbox once more: a parent message may have arrived
        # while this provider request was in flight. In that case the reply is
        # retained as an assistant prefix and the new message interrupts the
        # same logical turn at the next round instead of being deferred to a
        # separate turn.
        if not response.tool_calls:
            # Claim late inbox messages only when another provider dispatch is
            # guaranteed to happen inside this logical turn.  On the final
            # allowed round, claiming here would mark the message processing and
            # then fall out of the loop without ever showing it to the model.
            has_next_round = round_i + 1 < _max_tool_rounds
            intermediate_persisted = bool(
                accumulated_partials
                and durable_partial_count == len(accumulated_partials)
                and not (response.content or "").strip()
            )

            async def _persist_before_late_injection(
                *,
                created_at=None,
                _response_content=response.content or "",
                _has_accumulated_partials=bool(accumulated_partials),
                _complete_response_content=complete_response_content,
                _visible_joiner=round_visible_joiner,
                _thinking=response.reasoning_content,
            ):
                nonlocal intermediate_persisted
                if intermediate_persisted:
                    return
                late_segment = (
                    _response_content
                    if _has_accumulated_partials
                    else _complete_response_content
                )
                intermediate_persisted = await _persist_intermediate_segment(
                    late_segment,
                    visible_joiner_before=(
                        "" if _has_accumulated_partials else _visible_joiner
                    ),
                    thinking=_thinking,
                    created_at=created_at,
                )

            late_injected = (
                await _invoke_before_round(
                    before_round,
                    round_i + 1,
                    before_injection=_persist_before_late_injection,
                )
                if before_round is not None and has_next_round
                else []
            )
            if late_injected:
                from app.services.image_context import prepare_messages_for_model

                if (
                    turn_anchor_id is not None
                    and anchor_agent_id is not None
                    and durable_user_id is not None
                    and session_id
                    and not intermediate_persisted
                ):
                    raise RuntimeError(
                        "late injection claimed before intermediate assistant persistence"
                    )

                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        reasoning_content=response.reasoning_content,
                    )
                )
                prepared_injected = await prepare_messages_for_model(
                    late_injected,
                    agent_id=agent_id,
                    supports_vision=supports_vision,
                )
                api_messages.extend(
                    LLMMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content"),
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                    )
                    for msg in prepared_injected
                )
                skip_before_round_once = True
                continue
            # No next tool round will naturally hit the top-of-loop recovery
            # gate. Compact now from this round's authoritative provider usage
            # so the following user turn starts from the reduced durable
            # history. The already-generated reply remains valid and is not
            # sent to the model a second time.
            if (
                last_authoritative_prompt_tokens is not None
                and context_recovery is not None
            ):
                post_round_required, _, _ = should_compact(
                    model=model,
                    last_prompt_tokens=last_authoritative_prompt_tokens,
                    pre_flight_estimate=None,
                )
                if post_round_required:
                    post_round_budget = replace(
                        dispatch_budget,
                        authoritative_prompt_tokens=last_authoritative_prompt_tokens,
                        count_source="provider_usage",
                        token_overflow=True,
                    )
                    recovered = await context_recovery(model, post_round_budget)
                    if recovered is None:
                        logger.error(
                            "[context_guard] post-round compaction exhausted "
                            f"session={session_id} provider_tokens="
                            f"{last_authoritative_prompt_tokens}"
                        )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("reply", round_i + 1)
            return _join_visible_response_segments(*visible_response_segments) or "[LLM returned empty content]"

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
                    intro_text=_join_visible_response_segments(*visible_response_segments),
                    title=conf_call.title,
                    summary=conf_call.summary,
                    action=conf_call.action,
                    risk_level=conf_call.risk_level,
                    buttons=conf_call.buttons,
                    force_confirmation=conf_call.force_confirmation,
                    turn_anchor_id=turn_anchor_id,
                    assistant_content=response.content or None,
                    recovery_prefix_messages=recovery_prefix_messages,
                    reasoning_content=response.reasoning_content,
                    round_id=(
                        f"{turn_anchor_id or session_id}:{turn_execution_id}:round:{round_i + 1}"
                        if turn_anchor_id or session_id
                        else f"round:{round_i + 1}"
                    ),
                )
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client_guard.close()
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
                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )
                api_messages.append(
                    LLMMessage(
                        role="tool",
                        content=f"❌ {_conf_reason}",
                        tool_call_id=conf_call.call_id,
                    )
                )
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
            await client_guard.close()
            _log_turn_timing("repeat_guard", round_i + 1)
            return _join_visible_response_segments(*visible_response_segments) or REPEAT_TOOL_CALL_BREAK_MESSAGE

        # A durable background owner may have been cancelled while the provider
        # request was in flight.  Revalidate immediately before persisting tool
        # markers or starting any external side effect.
        await _before_tool_execution_guard()

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

        round_done_records: list[_RoundDoneToolCall] = []
        durable_round_id = (
            f"{turn_anchor_id or session_id}:{turn_execution_id}:round:{round_i + 1}"
            if turn_anchor_id or session_id
            else f"round:{round_i + 1}"
        )
        # Persist the complete planned round before executing its first tool.
        # A crash after A but before B must not erase B/C from the model's
        # already-issued assistant response. Only the first row carries the
        # shared assistant/resume prefix so replay does not duplicate it.
        running_events: list[dict[str, Any]] = []
        for tool_index, tc in enumerate(sanitized_tool_calls or []):
            args = _canonicalize_tc_arguments(tc, session_id)
            running_events.append(
                {
                    "name": tc["function"]["name"],
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "running",
                    "round_id": durable_round_id,
                    "round_tool_index": tool_index,
                    "reasoning_content": full_reasoning_content,
                    "assistant_content": (
                        response.content or None
                    ) if tool_index == 0 else None,
                    "recovery_prefix_messages": (
                        recovery_prefix_messages if tool_index == 0 else []
                    ),
                }
            )
        try:
            persisted_running = await _persist_tool_call_events_strict(
                running_events,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
            )
        except Exception as e:
            logger.exception(f"[LLM] Failed to persist complete planned tool round: {e}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("tool_round_persist_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
        for event in running_events:
            if persisted_running:
                event["_durable_persisted"] = True
            if on_tool_call is not None:
                try:
                    await on_tool_call(event)
                except Exception:
                    pass

        for tool_index, tc in enumerate(sanitized_tool_calls or []):
            await _before_tool_execution_guard()
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
                    tools_for_llm=tools_for_llm,
                    emit_running=False,
                    turn_anchor_id=turn_anchor_id,
                    before_execute=_admit_tool_execution,
                    round_done_records=round_done_records,
                    round_id=durable_round_id,
                    round_tool_index=tool_index,
                    assistant_content=(response.content or None) if tool_index == 0 else None,
                    recovery_prefix_messages=(
                        recovery_prefix_messages if tool_index == 0 else None
                    ),
                )
            except Exception as e:
                logger.exception(f"[LLM] Tool execution or durable result persistence failed: {e}")
                await _emit_round_done_events(round_done_records, on_tool_call)
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client_guard.close()
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
        try:
            rewrites = await enforce_message_budget(
                api_messages,
                fresh_start_idx=fresh_start,
                agent_id=agent_id,
                session_id=session_id,
            )
        except Exception as e:
            logger.exception(f"[LLM] Fresh tool round exceeds the hard message budget: {e}")
            await _emit_round_done_events(round_done_records, on_tool_call)
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("tool_result_budget_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"
        try:
            await _reconcile_round_tool_outputs(
                rewrites,
                round_done_records,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                turn_anchor_id=turn_anchor_id,
            )
        except Exception as e:
            logger.exception(f"[LLM] Failed to reconcile durable tool results: {e}")
            # The old per-tool values remain authoritative because the rewrite
            # transaction failed. Surface those completed results, but never
            # dispatch the divergent in-memory transcript to the provider.
            await _emit_round_done_events(round_done_records, on_tool_call)
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            _log_turn_timing("tool_result_reconcile_error", round_i + 1)
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        await _emit_round_done_events(round_done_records, on_tool_call)

        # Repeated tool-call nudge. On the 2nd identical-call round, append one
        # corrective message (append-only → prefix cache stays intact) so the
        # model gets a chance to change approach or answer before the guard
        # above hard-stops it on the 3rd.
        if _max_repeat == REPEAT_TOOL_CALL_NUDGE:
            api_messages.append(LLMMessage(role="user", content=REPEAT_TOOL_CALL_NUDGE_PROMPT))

    # Record tokens even on "too many rounds" exit
    if agent_id and _unsaved_usage.total_tokens > 0:
        await record_token_usage(agent_id, _unsaved_usage)
    await client_guard.close()
    _log_turn_timing("round_limit", _max_tool_rounds)
    return "[Error] Too many tool call rounds"


@serialize_conversation_execution
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
    on_usage=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_failover=None,
    skip_tools: bool = False,
    is_group: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    channel_context: dict | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    turn_anchor_agent_id: uuid.UUID | None = None,
    turn_type: str | None = None,
    context_recovery=None,
    prepared_tools: list[dict] | None = None,
    before_round=None,
    before_tool_execution=None,
    include_soul: bool = True,
    include_memory: bool = True,
    max_tool_rounds_override: int | None = None,
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

    if _same_model_record(primary_model, fallback_model):
        logger.info("[Failover] Primary and fallback reference the same model id; skipping fallback")
        fallback_model = None

    turn_messages = list(messages)
    injected_turn_messages: list[dict] = []
    from app.services.llm.turn_partition import effective_keep_recent_turns

    initial_provider_overflow_keep = effective_keep_recent_turns(
        primary_model,
        fallback_model,
    )

    def _recovery_message_key(message: dict) -> str:
        """Canonical equality for one live-vs-durable injected message.

        Durable history includes ``attachments=[]`` when the row metadata owns
        that key; live IM/subagent injection omits it when there are no files.
        Normalize only that representational difference. A counted comparison
        below preserves two legitimate identical messages instead of treating
        membership as a set.
        """
        normalized = dict(message)
        if normalized.get("attachments") == []:
            normalized.pop("attachments", None)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _surviving_baseline_suffix_keys(recovered: list[dict]) -> list[str]:
        """Return the longest contiguous baseline suffix present in recovery.

        Compaction can replace an arbitrary old prefix, but the uncompressed
        current-turn baseline survives as a contiguous suffix.  Reserving only
        that observed suffix prevents an older identical message from being
        mistaken for a newly injected message that is absent from a racing
        recovery read.
        """
        baseline_keys = [_recovery_message_key(message) for message in messages]
        recovered_keys = [_recovery_message_key(message) for message in recovered]
        if not baseline_keys or not recovered_keys:
            return []

        longest = 0
        last_baseline_key = baseline_keys[-1]
        for recovered_end, key in enumerate(recovered_keys):
            if key != last_baseline_key:
                continue
            matched = 1
            max_match = min(len(baseline_keys), recovered_end + 1)
            while (
                matched < max_match
                and baseline_keys[-1 - matched] == recovered_keys[recovered_end - matched]
            ):
                matched += 1
            longest = max(longest, matched)
            if longest == len(baseline_keys):
                break
        return baseline_keys[-longest:] if longest else []

    next_provider_overflow_keep = initial_provider_overflow_keep
    provider_overflow_exhausted = False

    async def _recover_once(model, budget):
        nonlocal turn_messages, next_provider_overflow_keep, provider_overflow_exhausted
        if context_recovery is None:
            return None

        provider_overflow = bool(getattr(budget, "provider_overflow", False))
        if provider_overflow and provider_overflow_exhausted:
            return None
        recovered = None
        while True:
            attempt_budget = budget
            attempted_keep: int | None = None
            if provider_overflow:
                attempted_keep = max(0, next_provider_overflow_keep)
                attempt_budget = replace(
                    budget,
                    keep_recent_turns_override=attempted_keep,
                )
            try:
                recovered = await context_recovery(model, attempt_budget)
            except Exception as exc:
                logger.error(
                    "[context_guard] safe context recovery failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                return None

            if provider_overflow:
                # A provider rejection permits one explicit protection level.
                # If that level has no expired turn, step down locally until
                # useful work is possible. A second provider rejection resumes
                # at N-1, eventually reaching current-turn-only (zero history).
                if attempted_keep == 0:
                    provider_overflow_exhausted = True
                else:
                    next_provider_overflow_keep = attempted_keep - 1
            if recovered is not None or not provider_overflow or attempted_keep == 0:
                break
        if recovered is not None:
            preflight_not_applicable = bool(
                getattr(recovered, "preflight_not_applicable", False)
            )
            # Recovery normally reloads the complete durable current-turn tail,
            # including already-delivered round-boundary injections. Preserve
            # any injection that is not present in that snapshot as well: a
            # read immediately following the standard ordered commits must not
            # drop a trailing user message that this logical turn consumed.
            # Canonical occurrence counts keep duplicate texts and the normal
            # durable path both correct.
            turn_messages = list(recovered)
            recovered_counts: dict[str, int] = {}
            for message in turn_messages:
                key = _recovery_message_key(message)
                recovered_counts[key] = recovered_counts.get(key, 0) + 1
            # Only recovered occurrences beyond the actually surviving
            # pre-injection suffix can satisfy live injections. This remains
            # correct when an older protected message has the same text as a
            # trailing injection and when compaction has replaced older rows.
            for baseline_key in _surviving_baseline_suffix_keys(turn_messages):
                recovered_counts[baseline_key] = max(
                    0,
                    recovered_counts.get(baseline_key, 0) - 1,
                )
            for injected in injected_turn_messages:
                key = _recovery_message_key(injected)
                if recovered_counts.get(key, 0) > 0:
                    recovered_counts[key] -= 1
                    continue
                turn_messages.append(injected)
            if preflight_not_applicable:
                from app.services.llm.compactor import ContextRecoveryMessages

                return ContextRecoveryMessages(
                    turn_messages,
                    preflight_not_applicable=True,
                )
            return turn_messages
        return None

    # Freeze one context snapshot for the whole logical turn.  Runtime
    # failover is a provider retry, not a new Agent turn, so both models must
    # see the same memory, clock, trigger and relationship state.
    prepared_turn_context = await _build_turn_context(
        agent_id=agent_id,
        agent_name=agent_name,
        role_description=role_description,
        user_id=user_id,
        current_user_name_override=current_user_name_override,
        is_group=is_group,
        session_id=session_id,
        channel_context=channel_context,
        include_soul=include_soul,
        include_memory=include_memory,
    )
    if prepared_tools is not None:
        prepared_tools = sorted(
            prepared_tools,
            key=lambda tool: tool.get("function", {}).get("name", ""),
        )
    elif skip_tools:
        prepared_tools = []
    else:
        from app.services.agent_tools import AGENT_TOOLS

        prepared_tools = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
        prepared_tools = sorted(
            prepared_tools or [],
            key=lambda tool: tool.get("function", {}).get("name", ""),
        )

    async def _wrapped_before_round(round_i: int, *, before_injection=None):
        if before_round is None:
            return []
        injected = await _invoke_before_round(
            before_round,
            round_i,
            before_injection=before_injection,
        )
        if injected:
            normalized = [dict(message) for message in injected]
            injected_turn_messages.extend(normalized)
            turn_messages.extend(normalized)
        return injected

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_thinking(text: str):
        guard.mark_streaming_started()
        if on_thinking:
            await on_thinking(text)

    async def _wrapped_on_tool_delta(data: dict):
        guard.mark_streaming_started()
        if on_tool_delta:
            await on_tool_delta(data)

    async def _wrapped_on_tool_call(data: dict):
        if data.get("status") in {"running", "done"}:
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        turn_messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=_wrapped_on_tool_delta,
        on_thinking=_wrapped_on_thinking,
        on_usage=on_usage,
        skip_tools=skip_tools,
        is_group=is_group,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        channel_context=channel_context,
        turn_anchor_id=turn_anchor_id,
        turn_anchor_agent_id=turn_anchor_agent_id,
        turn_type=turn_type,
        prepared_turn_context=prepared_turn_context,
        prepared_tools=prepared_tools,
        context_recovery=_recover_once,
        before_round=_wrapped_before_round,
        before_tool_execution=before_tool_execution,
        max_tool_rounds_override=max_tool_rounds_override,
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

    async def _fallback_on_thinking(text: str):
        fallback_guard.mark_streaming_started()
        if on_thinking:
            await on_thinking(text)

    async def _fallback_on_tool_delta(data: dict):
        fallback_guard.mark_streaming_started()
        if on_tool_delta:
            await on_tool_delta(data)

    async def _fallback_on_tool_call(data: dict):
        if data.get("status") in {"running", "done"}:
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        turn_messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=_fallback_on_tool_delta,
        on_thinking=_fallback_on_thinking,
        on_usage=on_usage,
        skip_tools=skip_tools,
        is_group=is_group,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        channel_context=channel_context,
        turn_anchor_id=turn_anchor_id,
        turn_anchor_agent_id=turn_anchor_agent_id,
        turn_type=turn_type,
        prepared_turn_context=prepared_turn_context,
        prepared_tools=prepared_tools,
        # A normal primary provider request has already occurred. Even when it
        # failed before yielding output, fallback is a retry and may not mutate
        # persisted history or trigger compaction after that first dispatch.
        context_recovery=None,
        before_round=_wrapped_before_round,
        before_tool_execution=before_tool_execution,
        max_tool_rounds_override=max_tool_rounds_override,
    )

    if primary_result == PROVIDER_CONTEXT_BLOCKED_MESSAGE and fallback_result == PROVIDER_CONTEXT_BLOCKED_MESSAGE:
        return PROVIDER_CONTEXT_BLOCKED_MESSAGE

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
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.core.permissions import is_agent_expired
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"
    from app.core.okr_feature import is_retired_okr_agent

    if await is_retired_okr_agent(db, agent):
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "数字员工已过期并停止服务，请联系管理员延长有效期。"

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
            user_id=user_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
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
    execution_user_id: uuid.UUID | None = None,
    turn_type: str = "background",
) -> str:
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # Load agent and models
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 未找到数字员工"
    from app.core.okr_feature import is_retired_okr_agent

    if await is_retired_okr_agent(db, agent):
        return "⚠️ 未找到数字员工"

    if execution_user_id is None:
        # Rolling-upgrade compatibility for legacy background call sites.
        execution_user_id = agent.creator_id
    else:
        from app.services.execution_identity import resolve_execution_user_id

        execution_user_id = await resolve_execution_user_id(
            db,
            agent,
            execution_user_id,
        )

    await ensure_active_turn(
        owner_user_id=execution_user_id,
        agent_id=agent_id,
        session_id=session_id or f"{turn_type}:{uuid.uuid4()}",
        turn_type=turn_type,
        title=user_prompt.strip()[:40] or None,
    )

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

    if _same_model_record(primary_model, fallback_model):
        logger.info("[call_agent_llm_with_tools] Primary and fallback reference the same model id; skipping fallback")
        fallback_model = None

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)
    # ``session_id`` is stable for schedulers and heartbeats. A fresh execution
    # id prevents separate runs at the same round number from being grouped as
    # one historical multi-tool response.
    background_execution_id = uuid.uuid4()

    # Everything needed by the provider/tool loop is now an immutable snapshot.
    # End the read transaction before waiting for provider capacity or remote
    # model I/O so hundreds of queued turns do not pin one database connection
    # each. ``expire_on_commit=False`` keeps the loaded agent/model values usable.
    await db.commit()

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
        tool_executed = False
        visible_response_segments: list[str] = []
        client_guard: LLMClientCloseGuard | None = None
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=get_model_api_key(model),
                model=model.model,
                base_url=model.base_url,
                timeout=_get_model_timeout(model),
                provider_managed_timeout=True,
            )
            client_guard = LLMClientCloseGuard(client)

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
                            logger.warning(
                                f"[call_agent_llm_with_tools] Token limit exceeded mid-loop: {_token_limit_msg}"
                            )
                            await client_guard.close()
                            return _token_limit_msg, False, tool_executed

                context_stop = await _guard_provider_dispatch(
                    model=model,
                    messages=api_messages,
                    tools=tools_for_llm if tools_for_llm else None,
                    max_output_tokens=max_tokens,
                    session_id=session_id,
                )
                if context_stop:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client_guard.close()
                    return context_stop, False, tool_executed

                try:
                    response = await _complete_with_throttle_retry(
                        client,
                        model=model,
                        round_i=round_i + 1,
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client_guard.close()
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    raise

                # Track tokens for this round
                _usage_this_round = _usage_from_response(response)
                _accumulated_usage.add(_usage_this_round)
                _unsaved_usage.add(_usage_this_round)

                if response.content and response.content.strip():
                    visible_response_segments.append(response.content)

                # Plain assistant text (no tool calls) ends the turn — it IS the reply.
                if not response.tool_calls:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client_guard.close()
                    return (
                        _join_visible_response_segments(*visible_response_segments) or "[Empty response]",
                        True,
                        tool_executed,
                    )

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
                            # Bind background confirmation to the configured executor.
                            user_id=execution_user_id,
                            intro_text=_join_visible_response_segments(*visible_response_segments),
                            title=conf_call.title,
                            summary=conf_call.summary,
                            action=conf_call.action,
                            risk_level=conf_call.risk_level,
                            buttons=conf_call.buttons,
                            force_confirmation=conf_call.force_confirmation,
                            turn_anchor_id=None,
                            assistant_content=response.content or None,
                            recovery_prefix_messages=[],
                            reasoning_content=response.reasoning_content,
                            round_id=(
                                f"{session_id}:background:{background_execution_id}:round:{round_i + 1}"
                            ),
                        )
                        if agent_id and _unsaved_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _unsaved_usage)
                        await client_guard.close()
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
                        fresh_start = len(api_messages)
                        api_messages.append(
                            LLMMessage(
                                role="assistant",
                                content=response.content or None,
                                tool_calls=sanitized_tool_calls,
                                reasoning_content=response.reasoning_content,
                            )
                        )
                        api_messages.append(
                            LLMMessage(
                                role="tool",
                                content=f"❌ {_conf_reason}",
                                tool_call_id=conf_call.call_id,
                            )
                        )
                        await enforce_message_budget(
                            api_messages,
                            fresh_start_idx=fresh_start,
                            agent_id=agent_id,
                            session_id=session_id,
                        )
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
                    await client_guard.close()
                    return (
                        _join_visible_response_segments(*visible_response_segments) or REPEAT_TOOL_CALL_BREAK_MESSAGE,
                        True,
                        tool_executed,
                    )

                # Only this newly appended round may be materialized. Earlier
                # rounds were already dispatched and are an immutable cache
                # prefix.
                fresh_start = len(api_messages)

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
                            user_id=execution_user_id,
                            session_id=session_id,
                            tool_call_id=str(tc.get("id") or ""),
                            tools_for_llm=tools_for_llm,
                        )
                        logger.info(
                            f"[LLM Timing] tool={tool_name} exec={perf_counter() - _tool_t0:.2f}s agent={agent_id}"
                        )
                    llm_view = await finalize_tool_output(
                        result,
                        tool_name=tool_name,
                        agent_id=agent_id,
                        session_id=session_id,
                        tool_call_id=str(tc.get("id") or ""),
                    )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tc["id"],
                            content=llm_view,
                        )
                    )

                await enforce_message_budget(
                    api_messages,
                    fresh_start_idx=fresh_start,
                    agent_id=agent_id,
                    session_id=session_id,
                )

                # Repeated tool-call nudge (2nd identical round): one corrective
                # message so the model can change approach or answer before the
                # guard above hard-stops it on the 3rd.
                if _max_repeat == REPEAT_TOOL_CALL_NUDGE:
                    api_messages.append(LLMMessage(role="user", content=REPEAT_TOOL_CALL_NUDGE_PROMPT))

            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client_guard.close()
            return "[Error] Too many tool call rounds", False, tool_executed

        except Exception as e:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            if client_guard is not None:
                await client_guard.close()
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

    if reply == PROVIDER_CONTEXT_BLOCKED_MESSAGE and reply2 == PROVIDER_CONTEXT_BLOCKED_MESSAGE:
        return PROVIDER_CONTEXT_BLOCKED_MESSAGE

    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"


__all__ = [
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "FailoverGuard",
    "is_retryable_error",
]
