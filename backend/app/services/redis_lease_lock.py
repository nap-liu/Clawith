"""Small Redis-backed renewable leases for cross-replica exclusion.

The lease never opens a database session. Acquisition is bounded, ownership is
represented by an unguessable token, and release/renewal are atomic owner
checks. Losing Redis or ownership interrupts the protected task with a
retryable error instead of allowing two replicas to continue the same work.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable
from types import TracebackType
from typing import Any, Self, TypeVar

from loguru import logger

from app.core.events import get_redis

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRY_INTERVAL_SECONDS = 0.05
DEFAULT_REDIS_IO_TIMEOUT_SECONDS = 2.0

_T = TypeVar("_T")

_RELEASE_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisLeaseError(RuntimeError):
    """Base error for a retryable distributed lease failure."""

    retryable = True


class RedisLeaseBusyError(RedisLeaseError):
    """Raised when bounded acquisition expires while another owner holds the lease."""


class RedisLeaseUnavailableError(RedisLeaseError):
    """Raised when Redis cannot provide the distributed exclusion guarantee."""


class RedisLeaseLostError(RedisLeaseError):
    """Raised when a held lease expires or is no longer owned by this process."""


class RedisLeaseLock:
    """A cancellation-safe, renewable Redis lease for one logical resource."""

    def __init__(
        self,
        resource: str,
        *,
        namespace: str = "distributed-lock",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        acquire_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
        renew_interval_seconds: float | None = None,
        redis_io_timeout_seconds: float = DEFAULT_REDIS_IO_TIMEOUT_SECONDS,
    ) -> None:
        normalized_resource = resource.strip()
        if not normalized_resource:
            raise ValueError("resource cannot be empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")
        if retry_interval_seconds <= 0:
            raise ValueError("retry_interval_seconds must be positive")
        if redis_io_timeout_seconds <= 0:
            raise ValueError("redis_io_timeout_seconds must be positive")

        renew_interval = ttl_seconds / 3 if renew_interval_seconds is None else renew_interval_seconds
        if renew_interval <= 0 or renew_interval >= ttl_seconds:
            raise ValueError("renew_interval_seconds must be positive and less than ttl_seconds")

        digest = hashlib.sha256(normalized_resource.encode()).hexdigest()
        self.key = f"clawith:{namespace}:{digest}"
        self.owner_token = uuid.uuid4().hex
        self.ttl_milliseconds = max(1, round(ttl_seconds * 1000))
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self.renew_interval_seconds = renew_interval
        self.redis_io_timeout_seconds = redis_io_timeout_seconds
        # A renewal must fail closed while the current lease still has time left.
        # Cap the Redis operation below half of the post-renewal safety window,
        # including in short-TTL tests.
        self.renew_io_timeout_seconds = min(
            redis_io_timeout_seconds,
            (ttl_seconds - renew_interval) / 2,
        )

        self._redis = None
        self._acquired = False
        self._owner_task: asyncio.Task | None = None
        self._renew_task: asyncio.Task | None = None
        self._loss_error: RedisLeaseError | None = None

    async def acquire(self) -> None:
        """Acquire the lease within the configured bound."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.acquire_timeout_seconds
        self._redis = await self._redis_call(
            get_redis(),
            timeout_seconds=min(
                self.redis_io_timeout_seconds,
                self.acquire_timeout_seconds,
            ),
            operation="client acquisition",
        )

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RedisLeaseBusyError(f"Redis lease remained busy for {self.acquire_timeout_seconds:g}s")

            set_task = asyncio.create_task(
                self._redis_call(
                    self._redis.set(
                        self.key,
                        self.owner_token,
                        px=self.ttl_milliseconds,
                        nx=True,
                    ),
                    timeout_seconds=min(self.redis_io_timeout_seconds, remaining),
                    operation="acquisition",
                )
            )
            try:
                acquired = await asyncio.shield(set_task)
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._cleanup_cancelled_acquire(set_task),
                    name=f"redis-lease-acquire-cleanup:{self.key[-12:]}",
                )
                await self._drain_task(cleanup_task)
                raise
            except RedisLeaseUnavailableError:
                cleanup_task = asyncio.create_task(
                    self._cleanup_cancelled_acquire(set_task),
                    name=f"redis-lease-acquire-cleanup:{self.key[-12:]}",
                )
                cancelled = await self._drain_task(cleanup_task)
                if cancelled is not None:
                    raise cancelled
                raise
            if acquired:
                self._acquired = True
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RedisLeaseBusyError(f"Redis lease remained busy for {self.acquire_timeout_seconds:g}s")
            await asyncio.sleep(min(self.retry_interval_seconds, remaining))

    async def release(self) -> None:
        """Delete the lease only if its owner token still matches."""

        if not self._acquired or self._redis is None:
            return
        released = await self._release_owner_if_present()
        if released:
            self._acquired = False
            return
        self._acquired = False
        raise RedisLeaseLostError("Redis lease ownership was lost before release")

    async def __aenter__(self) -> Self:
        await self.acquire()
        self._owner_task = asyncio.current_task()
        self._renew_task = asyncio.create_task(
            self._renew_loop(),
            name=f"redis-lease-renew:{self.key[-12:]}",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del traceback
        await self._stop_renewal()

        release_error: RedisLeaseError | None = None
        try:
            await self._release_shielded()
        except RedisLeaseError as caught:
            release_error = caught

        if self._loss_error is not None:
            if self._loss_error.__cause__ is None:
                raise self._loss_error from exc
            raise self._loss_error
        if isinstance(release_error, RedisLeaseUnavailableError) and exc_type is None:
            logger.warning(
                "Redis lease protected work completed but release is uncertain; "
                "the existing TTL remains authoritative: {}",
                release_error,
            )
            return False
        if release_error is not None and exc_type is None:
            raise release_error
        if release_error is not None:
            logger.warning(
                "Redis lease cleanup failed while propagating the protected task error: {}",
                release_error,
            )
        return False

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self.renew_interval_seconds)
            try:
                renewed = await self._redis_call(
                    self._redis.eval(
                        _RENEW_IF_OWNER_SCRIPT,
                        1,
                        self.key,
                        self.owner_token,
                        self.ttl_milliseconds,
                    ),
                    timeout_seconds=self.renew_io_timeout_seconds,
                    operation="renewal",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- client transports expose varied errors
                self._interrupt_owner(
                    RedisLeaseUnavailableError("Redis is unavailable during lease renewal"),
                    cause=exc,
                )
                return
            if not renewed:
                self._interrupt_owner(RedisLeaseLostError("Redis lease ownership was lost"))
                return

    def _interrupt_owner(
        self,
        error: RedisLeaseError,
        *,
        cause: Exception | None = None,
    ) -> None:
        if cause is not None:
            error.__cause__ = cause
        self._loss_error = error
        if self._owner_task is not None:
            self._owner_task.cancel()

    async def _stop_renewal(self) -> None:
        if self._renew_task is None:
            return
        self._renew_task.cancel()
        try:
            await self._renew_task
        except asyncio.CancelledError:
            pass
        self._renew_task = None

    async def _release_shielded(self) -> None:
        release_task = asyncio.create_task(self.release())
        cancelled: asyncio.CancelledError | None = None
        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError as caught:
                cancelled = cancelled or caught

        try:
            release_task.result()
        except BaseException as release_error:
            if cancelled is not None:
                if not isinstance(release_error, asyncio.CancelledError):
                    logger.warning(
                        "Redis lease release failed while cancellation was propagating: {}",
                        release_error,
                    )
                raise cancelled from release_error
            raise
        if cancelled is not None:
            raise cancelled

    async def _cleanup_cancelled_acquire(self, set_task: asyncio.Task[Any]) -> None:
        """Resolve an uncertain SET and remove only this contender's token."""

        try:
            await set_task
        except (asyncio.CancelledError, RedisLeaseError):
            pass
        except Exception as exc:  # noqa: BLE001 -- client transports expose varied errors
            logger.warning("Redis lease acquisition cleanup observed an unexpected SET failure: {}", exc)

        try:
            await self._release_owner_if_present()
        except RedisLeaseError as exc:
            # The key retains a finite TTL, so cleanup failure affects availability
            # but never grants this cancelled contender permission to execute.
            logger.warning("Redis lease acquisition cancellation cleanup failed: {}", exc)

    async def _release_owner_if_present(self) -> bool:
        if self._redis is None:
            return False
        result = await self._redis_call(
            self._redis.eval(
                _RELEASE_IF_OWNER_SCRIPT,
                1,
                self.key,
                self.owner_token,
            ),
            timeout_seconds=self.redis_io_timeout_seconds,
            operation="release",
        )
        return bool(result)

    @staticmethod
    async def _drain_task(task: asyncio.Task[Any]) -> asyncio.CancelledError | None:
        """Wait for cleanup despite repeated cancellation and retrieve its result."""

        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as caught:
                cancelled = cancelled or caught
                continue
        task.result()
        return cancelled

    @staticmethod
    async def _redis_call(
        awaitable: Awaitable[_T],
        *,
        timeout_seconds: float,
        operation: str,
    ) -> _T:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await awaitable
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise RedisLeaseUnavailableError(f"Redis timed out during lease {operation}") from exc
        except RedisLeaseError:
            raise
        except Exception as exc:
            raise RedisLeaseUnavailableError(f"Redis is unavailable during lease {operation}") from exc


def redis_lease_lock(
    resource: str,
    *,
    namespace: str = "distributed-lock",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    acquire_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    renew_interval_seconds: float | None = None,
    redis_io_timeout_seconds: float = DEFAULT_REDIS_IO_TIMEOUT_SECONDS,
) -> RedisLeaseLock:
    """Construct a renewable lease with a compact public API."""

    return RedisLeaseLock(
        resource,
        namespace=namespace,
        ttl_seconds=ttl_seconds,
        acquire_timeout_seconds=acquire_timeout_seconds,
        retry_interval_seconds=retry_interval_seconds,
        renew_interval_seconds=renew_interval_seconds,
        redis_io_timeout_seconds=redis_io_timeout_seconds,
    )
