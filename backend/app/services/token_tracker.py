"""Reusable token usage tracking for all LLM call paths.

Provides a single function to record token consumption against an Agent,
used by web chat, heartbeat, triggers, and A2A communication.
"""

import asyncio
import uuid
from dataclasses import dataclass

from loguru import logger


@dataclass
class TokenUsage:
    """Normalized token accounting returned by model providers."""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_eligible_input_tokens: int = 0
    estimated_tokens: int = 0

    @property
    def context_input_tokens(self) -> int:
        """Provider-authoritative full context input, including cached input."""
        return max(self.input_tokens, self.cache_eligible_input_tokens)

    def add(self, other: "TokenUsage") -> None:
        self.total_tokens += other.total_tokens
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_eligible_input_tokens += other.cache_eligible_input_tokens
        self.estimated_tokens += other.estimated_tokens


def _int_token(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _token_counter(source: dict, *keys: str) -> int:
    return sum(_int_token(source.get(key)) for key in keys)


def extract_token_usage(usage: dict | None) -> TokenUsage | None:
    """Extract normalized token usage, including prompt-cache counters when available."""
    if not usage:
        return None

    # OpenAI compatible:
    # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N,
    #  "prompt_tokens_details": {"cached_tokens": N}}
    if "total_tokens" in usage:
        detail_sources = [
            details
            for details in (
                usage.get("prompt_tokens_details"),
                usage.get("input_tokens_details"),
            )
            if isinstance(details, dict)
        ]
        cached = _token_counter(
            usage,
            "cached_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
        )
        cache_creation = _token_counter(
            usage,
            "cache_creation_tokens",
            "cache_creation_input_tokens",
        )
        for details in detail_sources:
            cached += _token_counter(
                details,
                "cached_tokens",
                "cache_read_tokens",
                "cache_read_input_tokens",
            )
            cache_creation += _token_counter(
                details,
                "cache_creation_tokens",
                "cache_creation_input_tokens",
            )
        if cached or cache_creation:
            logger.info(
                f"[Token Cache] API Provider -> Created: {cache_creation} tokens, "
                f"Read: {cached} tokens"
            )
        input_tokens = _int_token(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        output_tokens = _int_token(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        total_tokens = _int_token(usage.get("total_tokens", input_tokens + output_tokens))
        return TokenUsage(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            cache_creation_tokens=cache_creation,
            cache_eligible_input_tokens=input_tokens,
        )

    # Anthropic:
    # {"input_tokens": N, "output_tokens": N,
    #  "cache_creation_input_tokens": N, "cache_read_input_tokens": N}
    if "input_tokens" in usage or "output_tokens" in usage:
        cache_creation = _token_counter(usage, "cache_creation_input_tokens", "cache_creation_tokens")
        cache_read = _token_counter(usage, "cache_read_input_tokens", "cache_read_tokens", "cached_tokens")
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_creation += _token_counter(details, "cache_creation_input_tokens", "cache_creation_tokens")
            cache_read += _token_counter(details, "cached_tokens", "cache_read_input_tokens", "cache_read_tokens")
        if cache_creation or cache_read:
            logger.info(f"[Token Cache] Anthropic Native Hit -> Created: {cache_creation}, Read: {cache_read} tokens")
        input_tokens = _int_token(usage.get("input_tokens", 0))
        output_tokens = _int_token(usage.get("output_tokens", 0))
        context_input_tokens = input_tokens + cache_read + cache_creation
        return TokenUsage(
            total_tokens=context_input_tokens + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            cache_eligible_input_tokens=context_input_tokens,
        )

    # Gemini usage metadata can be normalized by the client, but keep a direct
    # fallback for providers that pass it through.
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        input_tokens = _int_token(usage.get("promptTokenCount", 0))
        output_tokens = _int_token(usage.get("candidatesTokenCount", 0))
        total_tokens = _int_token(usage.get("totalTokenCount", input_tokens + output_tokens))
        cached = _int_token(usage.get("cachedContentTokenCount", 0))
        return TokenUsage(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            cache_eligible_input_tokens=input_tokens,
        )

    return None


def extract_usage_tokens(usage: dict | None) -> int | None:
    """Extract total token count from an LLM response usage dict.

    Supports both OpenAI format (prompt_tokens + completion_tokens)
    and Anthropic format (input_tokens + output_tokens).
    Returns None if usage data is not available.
    """
    parsed = extract_token_usage(usage)
    return parsed.total_tokens if parsed else None


async def record_token_usage(
    agent_id: uuid.UUID,
    tokens: int | TokenUsage,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    estimated_tokens: int = 0,
) -> None:
    """Record token consumption for an agent.

    Safely updates tokens_used_today, tokens_used_month, and tokens_used_total.
    Uses an independent DB session to avoid interfering with the caller's transaction.
    """
    usage = tokens if isinstance(tokens, TokenUsage) else TokenUsage(
        total_tokens=tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        estimated_tokens=estimated_tokens,
    )
    persisted_values = {
        "total_tokens": usage.total_tokens,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "estimated_tokens": usage.estimated_tokens,
    }
    if any(not isinstance(value, int) or value < 0 for value in persisted_values.values()):
        logger.warning(
            "Rejected invalid token usage agent_id={} usage={}",
            agent_id,
            persisted_values,
        )
        return
    if usage.total_tokens == 0:
        return

    for attempt in range(3):
        try:
            await _record_token_usage_once(agent_id, usage)
            return
        except Exception as exc:
            sqlstate = _sqlstate(exc)
            retryable = sqlstate in {"40001", "40P01"}
            logger.warning(
                "Token usage persistence failed agent_id={} total_tokens={} "
                "sqlstate={} attempt={} retryable={} error={}",
                agent_id,
                usage.total_tokens,
                sqlstate,
                attempt + 1,
                retryable,
                str(exc)[:300],
            )
            if not retryable or attempt == 2:
                return
            await asyncio.sleep(0.05 * (attempt + 1))


def _sqlstate(exc: Exception) -> str | None:
    current = exc
    for _ in range(4):
        value = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if value:
            return str(value)
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
        if current is None:
            break
    return None


async def _record_token_usage_once(agent_id: uuid.UUID, usage: TokenUsage) -> None:
    """Atomically update agent and daily counters in one transaction."""
    from datetime import datetime, timezone

    from sqlalchemy import case, func, or_, update
    from sqlalchemy.dialects.postgresql import insert

    from app.database import async_session
    from app.models.activity_log import DailyTokenUsage
    from app.models.agent import Agent

    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    daily_reset = or_(Agent.last_daily_reset.is_(None), Agent.last_daily_reset < day_start)
    monthly_reset = or_(
        Agent.last_monthly_reset.is_(None), Agent.last_monthly_reset < month_start
    )

    def increment(column, delta: int):
        return func.coalesce(column, 0) + delta

    async with async_session() as db:
        result = await db.execute(
            update(Agent)
            .where(Agent.id == agent_id)
            .values(
                tokens_used_today=case(
                    (daily_reset, usage.total_tokens),
                    else_=increment(Agent.tokens_used_today, usage.total_tokens),
                ),
                cache_read_tokens_today=case(
                    (daily_reset, usage.cache_read_tokens),
                    else_=increment(Agent.cache_read_tokens_today, usage.cache_read_tokens),
                ),
                cache_creation_tokens_today=case(
                    (daily_reset, usage.cache_creation_tokens),
                    else_=increment(
                        Agent.cache_creation_tokens_today,
                        usage.cache_creation_tokens,
                    ),
                ),
                last_daily_reset=case(
                    (daily_reset, now), else_=Agent.last_daily_reset
                ),
                tokens_used_month=case(
                    (monthly_reset, usage.total_tokens),
                    else_=increment(Agent.tokens_used_month, usage.total_tokens),
                ),
                cache_read_tokens_month=case(
                    (monthly_reset, usage.cache_read_tokens),
                    else_=increment(Agent.cache_read_tokens_month, usage.cache_read_tokens),
                ),
                cache_creation_tokens_month=case(
                    (monthly_reset, usage.cache_creation_tokens),
                    else_=increment(
                        Agent.cache_creation_tokens_month,
                        usage.cache_creation_tokens,
                    ),
                ),
                last_monthly_reset=case(
                    (monthly_reset, now), else_=Agent.last_monthly_reset
                ),
                tokens_used_total=increment(Agent.tokens_used_total, usage.total_tokens),
                cache_read_tokens_total=increment(
                    Agent.cache_read_tokens_total, usage.cache_read_tokens
                ),
                cache_creation_tokens_total=increment(
                    Agent.cache_creation_tokens_total, usage.cache_creation_tokens
                ),
            )
            .returning(Agent.tenant_id, Agent.name)
        )
        agent_row = result.one_or_none()
        if agent_row is None:
            await db.rollback()
            return

        stmt = insert(DailyTokenUsage).values(
            tenant_id=agent_row.tenant_id,
            agent_id=agent_id,
            date=day_start,
            tokens_used=usage.total_tokens,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            estimated_tokens=usage.estimated_tokens,
        ).on_conflict_do_update(
            index_elements=["agent_id", "date"],
            set_={
                "tokens_used": DailyTokenUsage.tokens_used + usage.total_tokens,
                "input_tokens": DailyTokenUsage.input_tokens + usage.input_tokens,
                "output_tokens": DailyTokenUsage.output_tokens + usage.output_tokens,
                "cache_read_tokens": (
                    DailyTokenUsage.cache_read_tokens + usage.cache_read_tokens
                ),
                "cache_creation_tokens": (
                    DailyTokenUsage.cache_creation_tokens + usage.cache_creation_tokens
                ),
                "estimated_tokens": (
                    DailyTokenUsage.estimated_tokens + usage.estimated_tokens
                ),
                "updated_at": now,
            },
        )
        await db.execute(stmt)
        await db.commit()
        logger.debug(
            "Recorded {:,} tokens for agent {} (cache_read={:,})",
            usage.total_tokens,
            agent_row.name,
            usage.cache_read_tokens,
        )
