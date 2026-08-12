"""PostgreSQL coverage for BIGINT and concurrent token aggregation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session, engine
from app.models.activity_log import DailyTokenUsage
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.token_tracker import TokenUsage, record_token_usage


async def _seed_agent() -> Agent:
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Token {suffix}", slug=f"token-{suffix}")
        identity = Identity(
            username=f"token_{suffix}",
            email=f"token_{suffix}@test.local",
            password_hash="x",
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Token User",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Token Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            tokens_used_total=2_147_483_640,
            tokens_used_today=0,
            tokens_used_month=0,
            cache_read_tokens_total=0,
            cache_read_tokens_today=0,
            cache_read_tokens_month=0,
            cache_creation_tokens_total=0,
            cache_creation_tokens_today=0,
            cache_creation_tokens_month=0,
            last_daily_reset=datetime.now(timezone.utc),
            last_monthly_reset=datetime.now(timezone.utc),
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent


async def test_bigint_and_concurrent_token_updates_are_lossless():
    await engine.dispose()
    agent = await _seed_agent()
    delta = TokenUsage(
        total_tokens=10,
        input_tokens=6,
        output_tokens=4,
        cache_read_tokens=3,
        cache_creation_tokens=2,
        estimated_tokens=1,
    )

    await asyncio.gather(*(record_token_usage(agent.id, delta) for _ in range(20)))

    async with async_session() as db:
        refreshed = await db.get(Agent, agent.id)
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        daily = (
            await db.execute(
                select(DailyTokenUsage).where(
                    DailyTokenUsage.agent_id == agent.id,
                    DailyTokenUsage.date == today,
                )
            )
        ).scalar_one()

    assert refreshed.tokens_used_total == 2_147_483_840
    assert refreshed.tokens_used_today == 200
    assert refreshed.tokens_used_month == 200
    assert refreshed.cache_read_tokens_total == 60
    assert refreshed.cache_creation_tokens_total == 40
    assert daily.tokens_used == 200
    assert daily.input_tokens == 120
    assert daily.output_tokens == 80
    assert daily.cache_read_tokens == 60
    assert daily.cache_creation_tokens == 40
    assert daily.estimated_tokens == 20
    await engine.dispose()


async def test_negative_token_detail_is_rejected_without_partial_write():
    await engine.dispose()
    agent = await _seed_agent()

    await record_token_usage(
        agent.id,
        TokenUsage(total_tokens=10, input_tokens=10, cache_read_tokens=-1),
    )

    async with async_session() as db:
        refreshed = await db.get(Agent, agent.id)
    assert refreshed.tokens_used_total == 2_147_483_640
    await engine.dispose()
