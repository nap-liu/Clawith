"""Focused coverage for renewable Redis distributed leases."""

import asyncio
import uuid

import pytest

import app.services.conversation_execution_lock as conversation_lock_module
import app.services.redis_lease_lock as lease_module
from app import database
from app.core.events import get_redis
from app.services import channel_dispatch, trigger_daemon
from app.services.redis_lease_lock import (
    RedisLeaseBusyError,
    RedisLeaseLock,
    RedisLeaseLostError,
    RedisLeaseUnavailableError,
)


async def test_conversation_execution_lock_is_reentrant_in_one_task(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()

    async with conversation_lock_module.conversation_execution_lock(
        agent_id=agent_id,
        session_id=session_id,
    ):
        assert len(redis.values) == 1
        async with conversation_lock_module.conversation_execution_lock(
            agent_id=agent_id,
            session_id=session_id,
        ):
            assert len(redis.values) == 1

    assert redis.values == {}


async def test_conversation_execution_lock_serializes_distinct_tasks(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    order: list[str] = []

    async def owner() -> None:
        async with conversation_lock_module.conversation_execution_lock(
            agent_id=agent_id,
            session_id=session_id,
        ):
            order.append("owner")
            owner_entered.set()
            await release_owner.wait()

    async def contender() -> None:
        await owner_entered.wait()
        async with conversation_lock_module.conversation_execution_lock(
            agent_id=other_agent_id,
            session_id=session_id,
        ):
            order.append("contender")

    owner_task = asyncio.create_task(owner())
    contender_task = asyncio.create_task(contender())
    await owner_entered.wait()
    await asyncio.sleep(0.02)
    assert order == ["owner"]
    release_owner.set()
    await asyncio.gather(owner_task, contender_task)

    assert order == ["owner", "contender"]
    assert redis.values == {}


class FakeRedis:
    """Minimal expiring Redis surface used by the lease implementation."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float]] = {}
        self.renewals = 0
        self.fail_renewal = False
        self.fail_release = False
        self.delay_set_response = False
        self.delay_release_response = False
        self.block_renewal = False
        self.set_applied = asyncio.Event()
        self.release_applied = asyncio.Event()
        self._never = asyncio.Event()

    def _purge_expired(self, key: str) -> None:
        value = self.values.get(key)
        if value is not None and value[1] <= asyncio.get_running_loop().time():
            self.values.pop(key, None)

    async def set(self, key: str, owner: str, *, px: int, nx: bool) -> bool | None:
        assert nx is True
        self._purge_expired(key)
        if key in self.values:
            return None
        self.values[key] = (owner, asyncio.get_running_loop().time() + px / 1000)
        self.set_applied.set()
        if self.delay_set_response:
            await self._never.wait()
        return True

    async def eval(self, script: str, _key_count: int, key: str, owner: str, *args: int) -> int:
        self._purge_expired(key)
        value = self.values.get(key)
        if "pexpire" in script:
            if self.fail_renewal:
                raise ConnectionError("redis disconnected")
            if self.block_renewal:
                await self._never.wait()
            if value is None or value[0] != owner:
                return 0
            self.renewals += 1
            self.values[key] = (
                owner,
                asyncio.get_running_loop().time() + args[0] / 1000,
            )
            return 1
        if self.fail_release:
            raise ConnectionError("redis disconnected during release")
        if value is None or value[0] != owner:
            return 0
        self.values.pop(key, None)
        self.release_applied.set()
        if self.delay_release_response:
            await self._never.wait()
        return 1


async def test_acquisition_is_bounded_and_release_checks_owner(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    first = RedisLeaseLock(
        "session-a",
        ttl_seconds=1,
        acquire_timeout_seconds=0.02,
        retry_interval_seconds=0.005,
    )
    second = RedisLeaseLock(
        "session-a",
        ttl_seconds=1,
        acquire_timeout_seconds=0.02,
        retry_interval_seconds=0.005,
    )

    await first.acquire()
    with pytest.raises(RedisLeaseBusyError):
        await second.acquire()

    redis.values[first.key] = ("replacement-owner", asyncio.get_running_loop().time() + 1)
    with pytest.raises(RedisLeaseLostError):
        await first.release()
    assert redis.values[first.key][0] == "replacement-owner"


async def test_acquisition_io_timeout_is_bounded_and_cleans_uncertain_owner(monkeypatch) -> None:
    redis = FakeRedis()
    redis.delay_set_response = True

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    lock = RedisLeaseLock(
        "session-acquire-timeout",
        ttl_seconds=1,
        acquire_timeout_seconds=0.1,
        redis_io_timeout_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(RedisLeaseUnavailableError):
        await lock.acquire()

    assert asyncio.get_running_loop().time() - started < 0.2
    assert redis.values == {}


async def test_cancelling_inflight_acquisition_cleans_owner_and_propagates(monkeypatch) -> None:
    redis = FakeRedis()
    redis.delay_set_response = True

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    lock = RedisLeaseLock(
        "session-acquire-cancel",
        ttl_seconds=1,
        acquire_timeout_seconds=0.2,
        redis_io_timeout_seconds=0.05,
    )
    acquire_task = asyncio.create_task(lock.acquire())
    await asyncio.wait_for(redis.set_applied.wait(), timeout=0.1)

    acquire_task.cancel()
    await asyncio.sleep(0.005)
    acquire_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire_task

    assert redis.values == {}


async def test_repeated_cancellation_drains_inflight_release(monkeypatch) -> None:
    redis = FakeRedis()
    redis.delay_release_response = True

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)

    async def use_lease() -> None:
        async with RedisLeaseLock(
            "session-release-cancel",
            ttl_seconds=1,
            acquire_timeout_seconds=0.1,
            redis_io_timeout_seconds=0.2,
        ):
            pass

    task = asyncio.create_task(use_lease())
    await asyncio.wait_for(redis.release_applied.wait(), timeout=0.1)
    task.cancel()
    await asyncio.sleep(0.005)
    task.cancel()
    redis._never.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert redis.values == {}


async def test_lease_renews_and_cancellation_releases_it(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    entered = asyncio.Event()

    async def hold_lease() -> None:
        async with RedisLeaseLock(
            "session-renew",
            ttl_seconds=0.06,
            acquire_timeout_seconds=0.02,
            retry_interval_seconds=0.005,
            renew_interval_seconds=0.01,
        ):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(hold_lease())
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    for _ in range(20):
        if redis.renewals >= 2:
            break
        await asyncio.sleep(0.01)
    assert redis.renewals >= 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert redis.values == {}


async def test_cancelling_a_waiter_does_not_release_the_current_owner(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    owner = RedisLeaseLock(
        "session-waiter-cancel",
        ttl_seconds=1,
        acquire_timeout_seconds=1,
        retry_interval_seconds=0.01,
    )
    waiter = RedisLeaseLock(
        "session-waiter-cancel",
        ttl_seconds=1,
        acquire_timeout_seconds=1,
        retry_interval_seconds=0.01,
    )
    await owner.acquire()

    waiting_task = asyncio.create_task(waiter.acquire())
    await asyncio.sleep(0.02)
    waiting_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task

    assert redis.values[owner.key][0] == owner.owner_token
    await owner.release()
    assert redis.values == {}


async def test_renewal_failure_interrupts_owner_with_retryable_error(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    redis.fail_renewal = True

    with pytest.raises(RedisLeaseUnavailableError) as exc_info:
        async with RedisLeaseLock(
            "session-loss",
            ttl_seconds=0.06,
            acquire_timeout_seconds=0.02,
            retry_interval_seconds=0.005,
            renew_interval_seconds=0.01,
        ):
            await asyncio.sleep(1)

    assert exc_info.value.retryable is True


async def test_renewal_io_timeout_interrupts_owner_before_ttl_expires(monkeypatch) -> None:
    redis = FakeRedis()
    redis.block_renewal = True

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)

    with pytest.raises(RedisLeaseUnavailableError):
        async with RedisLeaseLock(
            "session-renew-timeout",
            ttl_seconds=0.08,
            acquire_timeout_seconds=0.02,
            retry_interval_seconds=0.005,
            renew_interval_seconds=0.01,
            redis_io_timeout_seconds=0.01,
        ):
            await asyncio.sleep(1)

    assert redis.values == {}


async def test_release_transport_failure_is_retryable_and_does_not_claim_success(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    lock = RedisLeaseLock(
        "session-release-failure",
        ttl_seconds=1,
        acquire_timeout_seconds=0.02,
        redis_io_timeout_seconds=0.02,
    )
    await lock.acquire()
    redis.fail_release = True

    with pytest.raises(RedisLeaseUnavailableError) as exc_info:
        await lock.release()

    assert exc_info.value.retryable is True
    assert redis.values[lock.key][0] == lock.owner_token
    redis.fail_release = False
    await lock.release()
    assert redis.values == {}


async def test_context_release_uncertainty_keeps_success_and_ttl_exclusion(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    resource = "session-release-uncertain"
    redis.fail_release = True

    async with RedisLeaseLock(
        resource,
        ttl_seconds=0.08,
        acquire_timeout_seconds=0.01,
        retry_interval_seconds=0.002,
        renew_interval_seconds=0.02,
    ):
        result = "completed once"

    assert result == "completed once"
    contender = RedisLeaseLock(
        resource,
        ttl_seconds=0.08,
        acquire_timeout_seconds=0.01,
        retry_interval_seconds=0.002,
        renew_interval_seconds=0.02,
    )
    with pytest.raises(RedisLeaseBusyError):
        await contender.acquire()

    await asyncio.sleep(0.09)
    redis.fail_release = False
    await contender.acquire()
    await contender.release()
    assert redis.values == {}


async def test_context_release_lost_still_fails_after_successful_body(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    lock = RedisLeaseLock(
        "session-release-lost",
        ttl_seconds=1,
        acquire_timeout_seconds=0.02,
    )

    with pytest.raises(RedisLeaseLostError):
        async with lock:
            redis.values[lock.key] = (
                "replacement-owner",
                asyncio.get_running_loop().time() + 1,
            )


async def test_redis_unavailable_does_not_fall_back_to_database(monkeypatch) -> None:
    async def unavailable_redis():
        raise ConnectionError("redis unavailable")

    def database_session_must_not_be_opened():
        raise AssertionError("distributed lease must not open a database session")

    monkeypatch.setattr(lease_module, "get_redis", unavailable_redis)
    monkeypatch.setattr(database, "async_session", database_session_must_not_be_opened)

    with pytest.raises(RedisLeaseUnavailableError) as exc_info:
        await channel_dispatch.run_channel_message(
            "durable-session-unavailable",
            is_command=False,
            reactions=channel_dispatch.ChannelReactions(),
            work=lambda: asyncio.sleep(0, result="must-not-run"),
            distributed=True,
        )

    assert exc_info.value.retryable is True


async def test_distributed_channel_turn_renews_without_database_session(monkeypatch) -> None:
    redis = FakeRedis()

    async def get_fake_redis():
        return redis

    def database_session_must_not_be_opened():
        raise AssertionError("distributed turn must not open a database session")

    monkeypatch.setattr(lease_module, "get_redis", get_fake_redis)
    monkeypatch.setattr(database, "async_session", database_session_must_not_be_opened)

    result = await channel_dispatch.run_channel_message(
        "durable-session-success",
        is_command=False,
        reactions=channel_dispatch.ChannelReactions(),
        work=lambda: asyncio.sleep(0.01, result="done"),
        distributed=True,
    )

    assert result == "done"
    assert redis.values == {}


@pytest.mark.parametrize(
    "error",
    [
        RedisLeaseBusyError("busy"),
        RedisLeaseUnavailableError("unavailable"),
        RedisLeaseLostError("lost"),
    ],
)
def test_durable_trigger_retries_all_redis_lease_errors(error: Exception) -> None:
    assert trigger_daemon._is_retryable_invocation_error(error) is True


@pytest.mark.integration
async def test_real_redis_client_contract_and_lease_scripts() -> None:
    redis = await get_redis()
    contract_key = f"clawith:test:redis-contract:{uuid.uuid4().hex}"
    resource = f"real-redis-lease:{uuid.uuid4().hex}"
    lock = RedisLeaseLock(
        resource,
        ttl_seconds=0.2,
        acquire_timeout_seconds=0.1,
        retry_interval_seconds=0.01,
        renew_interval_seconds=0.03,
        redis_io_timeout_seconds=0.1,
    )
    contender = RedisLeaseLock(
        resource,
        ttl_seconds=0.2,
        acquire_timeout_seconds=0.03,
        retry_interval_seconds=0.01,
        redis_io_timeout_seconds=0.1,
    )

    try:
        assert await redis.set(contract_key, "first", px=1000, nx=True) is True
        assert await redis.set(contract_key, "second", px=1000, nx=True) is None
        await redis.delete(contract_key)

        async with lock:
            with pytest.raises(RedisLeaseBusyError):
                await contender.acquire()
            await asyncio.sleep(0.26)
            assert await redis.exists(lock.key) == 1

        assert await redis.exists(lock.key) == 0
    finally:
        await redis.delete(contract_key, lock.key, contender.key)
