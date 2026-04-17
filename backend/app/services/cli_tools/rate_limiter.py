"""Per-(tool, agent, user) sliding-window rate limiter backed by Redis.

Why: CLI tools are called by agents, and an agent driven by prompt injection
can issue the same tool call in an unbounded loop — hammering downstream
services (svc reports, paid APIs) or bloating the sandbox host. A cheap
sliding-window counter per (tool, agent, user) triple cuts this off before
the executor even renders the command.

Implementation: Redis sorted set per triple, score = timestamp (ms), value
= uuid per call. Each `check_and_record` in one pipeline:
  1. ZREMRANGEBYSCORE to evict entries older than 60s
  2. ZCARD to count remaining
  3. ZADD the current call (only if under the limit — otherwise we'd leak
     entries on denied calls and they'd cascade into a self-inflicted DoS)
  4. EXPIRE so keys for idle triples disappear eventually

Failure policy: **fail-open**. If Redis is unreachable or any command
raises, log a warning and return `(True, 0)`. Rationale: a Redis outage
must not block every CLI tool call in the platform — that would escalate
a cache/infra hiccup into a full business outage. The rate limiter is a
safety cap for misbehaving agents, not a hard security boundary; other
layers (tenant check, schema validation, sandbox caps) still apply.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Hard-coded 60s window. If product ever wants 10s/5m/etc, make it a
# separate PR that also threads the window into the key or ZSET keyspace
# (mixing windows in one key gives wrong counts).
WINDOW_SECONDS = 60
_WINDOW_MS = WINDOW_SECONDS * 1000
# Expire keys a bit after the window so idle triples don't live forever.
_KEY_TTL_SECONDS = WINDOW_SECONDS * 2


class RateLimiter:
    """Sliding-window rate limiter per (tool_id, agent_id, user_id) triple.

    Uses Redis sorted sets: key `cli-tools:rl:{tool}:{agent}:{user}`,
    score = timestamp (ms), value = uuid per call. Prune old entries,
    count remaining, reject if >= limit.
    """

    def __init__(self, redis_client: Any):
        self._redis = redis_client

    @staticmethod
    def _key(tool_id: Any, agent_id: Any, user_id: Any) -> str:
        return f"cli-tools:rl:{tool_id}:{agent_id}:{user_id}"

    async def check_and_record(
        self,
        tool_id: Any,
        agent_id: Any,
        user_id: Any,
        limit_per_minute: int,
    ) -> tuple[bool, int]:
        """Returns (allowed, current_count_in_window).

        When `limit_per_minute == 0` the limiter is disabled and Redis is
        never touched — important because many deployments may have the
        default (0 or high limit) and we want zero overhead on the hot path.
        """
        if limit_per_minute <= 0:
            return True, 0

        key = self._key(tool_id, agent_id, user_id)
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - _WINDOW_MS

        try:
            # Step 1: prune + count. We must decide allow/deny *before*
            # adding the new entry, otherwise the entry we just inserted
            # inflates the count and the limit becomes off-by-one.
            pipe = self._redis.pipeline(transaction=True)
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            results = await pipe.execute()
            current_count = int(results[1] or 0)

            if current_count >= limit_per_minute:
                # Deny. Do NOT record the attempt — if we did, a hot loop
                # hitting a denied tool would keep the window saturated
                # forever instead of draining.
                return False, current_count

            # Step 2: record the allowed call. Use uuid as member so
            # concurrent calls in the same millisecond don't collide.
            pipe = self._redis.pipeline(transaction=True)
            pipe.zadd(key, {str(uuid.uuid4()): now_ms})
            pipe.expire(key, _KEY_TTL_SECONDS)
            await pipe.execute()
            return True, current_count + 1
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning(
                "cli_tools.rate_limiter.redis_error",
                extra={
                    "tool_id": str(tool_id),
                    "agent_id": str(agent_id),
                    "user_id": str(user_id),
                    "error": str(exc),
                },
            )
            # Fail-open: don't escalate a cache outage into a platform outage.
            return True, 0
