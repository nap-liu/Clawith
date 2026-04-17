"""Tests for the per-(tool, agent, user) sliding-window rate limiter.

Uses an in-memory fake that mimics the subset of redis.asyncio we rely on
(pipeline + zadd/zremrangebyscore/zcard/expire). We don't pull in a
real Redis client or fakeredis — the fake is ~40 lines, always available,
and its semantics are exactly what the limiter contract expects.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.cli_tools.rate_limiter import RateLimiter


class FakePipeline:
    """Captures commands and replays them against the fake's state on execute()."""

    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._ops: list = []

    def zremrangebyscore(self, key, mn, mx):
        self._ops.append(("zremrangebyscore", key, mn, mx))
        return self

    def zcard(self, key):
        self._ops.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "zremrangebyscore":
                _, key, mn, mx = op
                zset = self._redis.store.get(key, {})
                to_del = [m for m, s in zset.items() if mn <= s <= mx]
                for m in to_del:
                    del zset[m]
                results.append(len(to_del))
            elif op[0] == "zcard":
                _, key = op
                results.append(len(self._redis.store.get(key, {})))
            elif op[0] == "zadd":
                _, key, mapping = op
                zset = self._redis.store.setdefault(key, {})
                zset.update(mapping)
                results.append(len(mapping))
            elif op[0] == "expire":
                results.append(True)
        return results


class FakeRedis:
    def __init__(self):
        # key -> {member: score}
        self.store: dict[str, dict[str, float]] = {}

    def pipeline(self, transaction=True):
        return FakePipeline(self)


@pytest.mark.asyncio
async def test_allows_under_limit():
    limiter = RateLimiter(FakeRedis())
    for i in range(5):
        allowed, count = await limiter.check_and_record("tool", "agent", "user", 10)
        assert allowed is True
        assert count == i + 1


@pytest.mark.asyncio
async def test_blocks_at_limit():
    limiter = RateLimiter(FakeRedis())
    # Fill the window to exactly the limit.
    for _ in range(3):
        allowed, _ = await limiter.check_and_record("tool", "agent", "user", 3)
        assert allowed is True
    # Next call must be denied and count reflects the full window.
    allowed, count = await limiter.check_and_record("tool", "agent", "user", 3)
    assert allowed is False
    assert count == 3
    # A second denied call also reports the same count (we don't record denials).
    allowed, count = await limiter.check_and_record("tool", "agent", "user", 3)
    assert allowed is False
    assert count == 3


@pytest.mark.asyncio
async def test_window_slides_old_entries_expire(monkeypatch):
    """Entries older than 60s must be evicted by the next check."""
    import app.services.cli_tools.rate_limiter as rl

    fake_redis = FakeRedis()
    limiter = RateLimiter(fake_redis)

    # Freeze time to t0 and fill the window.
    t = [1_000_000.0]  # seconds
    monkeypatch.setattr(rl.time, "time", lambda: t[0])

    for _ in range(5):
        allowed, _ = await limiter.check_and_record("tool", "agent", "user", 5)
        assert allowed is True

    # At t0 we're at the limit.
    allowed, count = await limiter.check_and_record("tool", "agent", "user", 5)
    assert allowed is False
    assert count == 5

    # Advance past the 60s window — the zremrangebyscore call in the next
    # check should drop all old entries and the slot is free again.
    t[0] += 61
    allowed, count = await limiter.check_and_record("tool", "agent", "user", 5)
    assert allowed is True
    assert count == 1  # window is empty + this call


@pytest.mark.asyncio
async def test_zero_limit_bypasses_redis():
    """limit=0 means unlimited; the Redis client must not be touched."""
    redis = MagicMock()
    # Any attribute access on the pipeline would raise because it's a
    # MagicMock configured to explode on use — but we rely on the simpler
    # assertion that pipeline() is never called.
    limiter = RateLimiter(redis)
    allowed, count = await limiter.check_and_record("tool", "agent", "user", 0)
    assert allowed is True
    assert count == 0
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_different_triples_are_isolated():
    """Changing tool / agent / user each yields a separate window."""
    limiter = RateLimiter(FakeRedis())

    # Fill window for one triple.
    for _ in range(2):
        await limiter.check_and_record("tool-A", "agent-1", "user-x", 2)
    allowed, _ = await limiter.check_and_record("tool-A", "agent-1", "user-x", 2)
    assert allowed is False

    # Different tool — fresh window.
    allowed, count = await limiter.check_and_record("tool-B", "agent-1", "user-x", 2)
    assert allowed is True and count == 1

    # Different agent — fresh window.
    allowed, count = await limiter.check_and_record("tool-A", "agent-2", "user-x", 2)
    assert allowed is True and count == 1

    # Different user — fresh window.
    allowed, count = await limiter.check_and_record("tool-A", "agent-1", "user-y", 2)
    assert allowed is True and count == 1


@pytest.mark.asyncio
async def test_fail_open_on_redis_error():
    """Redis outage must not block the call — log and allow (decision documented
    in rate_limiter.py: fail-open)."""

    class ExplodingRedis:
        def pipeline(self, transaction=True):
            raise ConnectionError("redis down")

    limiter = RateLimiter(ExplodingRedis())
    allowed, count = await limiter.check_and_record("tool", "agent", "user", 10)
    assert allowed is True
    assert count == 0
