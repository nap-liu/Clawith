"""Bounded, single-lane retries around one immutable provider request."""

from __future__ import annotations

import asyncio
import copy
import os
from time import perf_counter

import httpx
from loguru import logger

from .client import LLMError

RATE_LIMIT_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
CONNECTION_RETRY_DELAYS = (1.0, 2.0)
PROVIDER_RECOVERY_RETRY_DELAY = 0.25
PROVIDER_MAX_IN_FLIGHT_ENV = "LLM_PROVIDER_MAX_IN_FLIGHT"
PROVIDER_MAX_IN_FLIGHT_DEFAULT = 2


class ProviderThrottleExhausted(LLMError):
    """Transient HTTP 429 persisted through all automatic retries."""

    def __init__(self, error: LLMError, *, retry_count: int):
        super().__init__(
            str(error),
            status_code=error.status_code,
            error_code=error.error_code,
            error_type=error.error_type,
            request_id=error.request_id,
            retry_after_seconds=error.retry_after_seconds,
        )
        self.retry_count = retry_count
        self.had_progress = bool(getattr(error, "had_progress", False))


class ProviderRecoveryExhausted(LLMError):
    """A bounded same-request recovery was consumed and must not fail over."""

    def __init__(self, error: LLMError):
        super().__init__(
            str(error),
            status_code=error.status_code,
            error_code=error.error_code,
            error_type=error.error_type,
            request_id=error.request_id,
            retry_after_seconds=error.retry_after_seconds,
        )
        self.had_progress = bool(getattr(error, "had_progress", False))


def _is_hard_quota_error(error: LLMError) -> bool:
    structured = " ".join(
        str(value or "").lower()
        for value in (error.error_code, error.error_type, error)
    )
    return any(
        marker in structured
        for marker in (
            "insufficient_quota",
            "billing",
            "credit balance",
            "credits exhausted",
            "hard_limit",
            "payment_required",
            "exceeded your current quota",
        )
    )


def _is_provider_throttle_error(error: LLMError) -> bool:
    """Recognize transient rate limiting without retrying auth or hard quota."""
    if error.status_code in {401, 403} or _is_hard_quota_error(error):
        return False
    if error.status_code is not None:
        return error.status_code == 429
    structured = " ".join(
        str(value or "").lower()
        for value in (error.error_code, error.error_type, error)
    )
    return any(
        marker in structured
        for marker in (
            "limit_burst_rate",
            "rate_limit_exceeded",
            "rate_limit_error",
            "request rate increased too quickly",
            "too many requests",
            "throttled due to system capacity",
            "system capacity limits",
        )
    )


def _is_provider_recovery_error(error: LLMError) -> bool:
    """Narrow non-429 failures eligible for one identical-request retry."""
    if error.status_code in {500, 502, 503, 504}:
        return True
    structured = " ".join(
        str(value or "").lower()
        for value in (error.error_code, error.error_type, error)
    )
    return (
        "backend buffer overflow" in structured
        or (
            "internalerror.algo.invalidparameter" in structured
            and "url" in structured
            and ("not appear to be valid" in structured or "invalid" in structured)
        )
    )


def _is_connection_error(error: LLMError) -> bool:
    structured = " ".join(
        str(value or "").lower()
        for value in (error.error_code, error.error_type, error)
    )
    return any(
        marker in structured
        for marker in (
            "connection_error",
            "connection failed",
            "connecterror",
            "connecttimeout",
            "network unreachable",
        )
    )


def _normalize_transport_error(error: BaseException) -> LLMError:
    if isinstance(error, LLMError):
        return error
    return LLMError(
        f"Connection failed: {type(error).__name__}: {error}",
        error_code=type(error).__name__,
        error_type="connection_error",
    )


async def _sleep_before_throttle_retry(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


async def _emit_retry_status(
    callback,
    *,
    retry_index: int,
    delay_seconds: float,
    recovered: bool = False,
) -> None:
    if callback is None:
        return
    try:
        status = {
            "kind": "provider_retry",
            "reason": "rate_limited",
            "state": "recovered" if recovered else "retrying",
            "retry_index": retry_index,
            "max_retries": len(RATE_LIMIT_RETRY_DELAYS),
            "delay_seconds": delay_seconds,
        }
        if not recovered:
            status["message_key"] = "status.providerRateLimitRetry"
        await callback(status)
    except Exception as exc:  # noqa: BLE001 - status delivery must not fail the turn
        logger.warning(f"[LLM] retry status callback failed (ignored): {exc}")


async def _close_cancelled_provider_client(client) -> None:
    """Best-effort close without allowing cleanup to replace cancellation."""
    close_task = asyncio.create_task(client.close())

    def _consume_close_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException as exc:  # noqa: BLE001 - also consume cancellation
            logger.warning(f"[LLM] cancelled provider client close failed (ignored): {exc}")

    close_task.add_done_callback(_consume_close_result)
    try:
        await asyncio.shield(close_task)
    except BaseException:  # noqa: BLE001, S110 - caller re-raises its cancellation
        pass


_provider_slots: dict[tuple[int, str, str, int, int], asyncio.Semaphore] = {}


def _provider_slot(model) -> asyncio.Semaphore:
    try:
        limit = int(os.environ.get(PROVIDER_MAX_IN_FLIGHT_ENV, PROVIDER_MAX_IN_FLIGHT_DEFAULT))
    except ValueError:
        limit = PROVIDER_MAX_IN_FLIGHT_DEFAULT
    limit = max(1, limit)
    key = (
        id(asyncio.get_running_loop()),
        str(getattr(model, "provider", "") or "").lower(),
        str(getattr(model, "base_url", "") or ""),
        hash(str(getattr(model, "api_key_encrypted", "") or "")),
        limit,
    )
    return _provider_slots.setdefault(key, asyncio.Semaphore(limit))


def _next_retry(
    error: LLMError,
    *,
    lane: str | None,
    rate_limit_retries: int,
    recovery_retries: int,
) -> tuple[str, int, float]:
    if _is_provider_throttle_error(error):
        if lane not in {None, "rate_limit_429"}:
            raise ProviderRecoveryExhausted(error) from error
        if rate_limit_retries >= len(RATE_LIMIT_RETRY_DELAYS):
            raise ProviderThrottleExhausted(error, retry_count=rate_limit_retries) from error
        delay = RATE_LIMIT_RETRY_DELAYS[rate_limit_retries]
        return "rate_limit_429", rate_limit_retries + 1, delay
    if _is_provider_recovery_error(error):
        if lane not in {None, "provider_recovery"} or recovery_retries >= 1:
            raise ProviderRecoveryExhausted(error) from error
        return "provider_recovery", recovery_retries + 1, PROVIDER_RECOVERY_RETRY_DELAY
    if _is_connection_error(error):
        if lane not in {None, "connection"} or recovery_retries >= len(CONNECTION_RETRY_DELAYS):
            raise ProviderRecoveryExhausted(error) from error
        delay = CONNECTION_RETRY_DELAYS[recovery_retries]
        return "connection", recovery_retries + 1, delay
    if lane is not None:
        raise ProviderRecoveryExhausted(error) from error
    raise error


async def _stream_with_throttle_retry(
    client,
    *,
    model,
    round_i: int,
    allow_retries: bool = True,
    **stream_kwargs,
):
    status_callback = stream_kwargs.pop("on_status", None)
    callbacks = {
        key: stream_kwargs.pop(key, None)
        for key in ("on_chunk", "on_thinking", "on_tool_delta")
    }
    frozen_kwargs = copy.deepcopy(stream_kwargs)
    provider_slot = _provider_slot(model)
    lane = None
    rate_limit_retries = 0
    recovery_retries = 0
    attempt_index = 0

    while True:
        first_progress_at: list[float] = []
        started_at = perf_counter()
        dispatch_started_at = started_at
        attempt_kwargs = stream_kwargs if attempt_index == 0 else copy.deepcopy(frozen_kwargs)
        try:
            async with provider_slot:
                dispatch_started_at = perf_counter()

                def _wrap_progress(callback, progress_marks=first_progress_at):
                    async def _marked(*args, **kwargs):
                        if not progress_marks:
                            progress_marks.append(perf_counter())
                        if callback is not None:
                            return await callback(*args, **kwargs)

                    return _marked

                for key, callback in callbacks.items():
                    attempt_kwargs[key] = _wrap_progress(callback)
                response = await client.stream(**attempt_kwargs)
            elapsed = perf_counter() - started_at
            ttft = f"{first_progress_at[0] - dispatch_started_at:.2f}s" if first_progress_at else "n/a"
            usage = getattr(response, "usage", None)
            output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            rate = f" ({output_tokens / elapsed:.0f} tok/s)" if output_tokens and elapsed > 0 else ""
            logger.info(
                f"[LLM Timing] round={round_i} model={getattr(model, 'model', '?')} "
                f"llm_call={elapsed:.2f}s ttft={ttft} output_tokens={output_tokens}{rate}"
            )
            if rate_limit_retries and status_callback is not None:
                await _emit_retry_status(
                    status_callback,
                    retry_index=0,
                    delay_seconds=0,
                    recovered=True,
                )
            return response
        except asyncio.CancelledError:
            await _close_cancelled_provider_client(client)
            raise
        except httpx.ReadTimeout:
            raise
        except (LLMError, httpx.TransportError) as caught:
            error = _normalize_transport_error(caught)
            if first_progress_at:
                error.had_progress = True
                if _is_provider_throttle_error(error):
                    raise ProviderThrottleExhausted(
                        error,
                        retry_count=rate_limit_retries,
                    ) from error
                if error is caught:
                    raise
                raise error from caught
            if not allow_retries:
                if error is caught:
                    raise
                raise error from caught
            lane, retry_count, delay = _next_retry(
                error,
                lane=lane,
                rate_limit_retries=rate_limit_retries,
                recovery_retries=recovery_retries,
            )
            if lane == "rate_limit_429":
                rate_limit_retries = retry_count
                await _emit_retry_status(
                    status_callback,
                    retry_index=retry_count,
                    delay_seconds=delay,
                )
            else:
                recovery_retries = retry_count
            logger.warning(
                f"[LLM] provider retry lane={lane} retry={retry_count} delay={delay:.1f}s "
                f"round={round_i} provider={getattr(model, 'provider', '?')} "
                f"model={getattr(model, 'model', '?')} code={error.error_code or '?'} "
                f"request_id={error.request_id or '?'}"
            )
            await _sleep_before_throttle_retry(delay)
            attempt_index += 1


async def _complete_with_throttle_retry(
    client,
    *,
    model,
    round_i: int,
    allow_retries: bool = True,
    **complete_kwargs,
):
    status_callback = complete_kwargs.pop("on_status", None)
    frozen_kwargs = copy.deepcopy(complete_kwargs)
    provider_slot = _provider_slot(model)
    lane = None
    rate_limit_retries = 0
    recovery_retries = 0
    attempt_index = 0

    while True:
        queued_at = perf_counter()
        try:
            async with provider_slot:
                dispatch_started_at = perf_counter()
                attempt_kwargs = complete_kwargs if attempt_index == 0 else copy.deepcopy(frozen_kwargs)
                response = await client.complete(**attempt_kwargs)
            logger.info(
                f"[LLM Timing] round={round_i} model={getattr(model, 'model', '?')} "
                f"queue={dispatch_started_at - queued_at:.2f}s "
                f"llm_call={perf_counter() - dispatch_started_at:.2f}s (complete)"
            )
            if rate_limit_retries and status_callback is not None:
                await _emit_retry_status(
                    status_callback,
                    retry_index=0,
                    delay_seconds=0,
                    recovered=True,
                )
            return response
        except asyncio.CancelledError:
            await _close_cancelled_provider_client(client)
            raise
        except httpx.ReadTimeout:
            raise
        except (LLMError, httpx.TransportError) as caught:
            error = _normalize_transport_error(caught)
            if not allow_retries:
                if error is caught:
                    raise
                raise error from caught
            lane, retry_count, delay = _next_retry(
                error,
                lane=lane,
                rate_limit_retries=rate_limit_retries,
                recovery_retries=recovery_retries,
            )
            if lane == "rate_limit_429":
                rate_limit_retries = retry_count
                await _emit_retry_status(
                    status_callback,
                    retry_index=retry_count,
                    delay_seconds=delay,
                )
            else:
                recovery_retries = retry_count
            logger.warning(
                f"[LLM] provider complete retry lane={lane} retry={retry_count} "
                f"delay={delay:.1f}s round={round_i} provider={getattr(model, 'provider', '?')} "
                f"model={getattr(model, 'model', '?')} code={error.error_code or '?'} "
                f"request_id={error.request_id or '?'}"
            )
            await _sleep_before_throttle_retry(delay)
            attempt_index += 1


__all__ = [name for name in globals() if not name.startswith("__")]
