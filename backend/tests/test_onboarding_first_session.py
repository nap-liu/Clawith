import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.agent import Agent, AgentUserOnboarding
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401 - registers FK target
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 - registers FK target
from app.models.tenant import Tenant
from app.models.user import User
from app.services.onboarding import (
    PHASE_COMPLETED,
    PHASE_GREETED,
    PHASE_PENDING,
    claim_fixed_welcome_slot,
    claim_normal_first_turn,
    claim_onboarding_greeting,
    mark_onboarded,
    mark_onboarding_phase,
    onboarding_claim_is_current,
    release_onboarding_claim,
    resolve_onboarding_eligibility,
)


@pytest.fixture
async def first_session_pair():
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    first_session_id = uuid.uuid4()
    second_session_id = uuid.uuid4()
    base = datetime.now(timezone.utc) - timedelta(minutes=5)

    async with session_factory() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Onboarding test tenant",
                slug=f"onboarding-{tenant_id.hex}",
                im_provider="web_only",
            )
        )
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                display_name="First user",
                role="member",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=user_id,
                name="First agent",
                role_description="assistant",
                status="running",
            )
        )
        await db.flush()
        db.add_all(
            [
                ChatSession(
                    id=first_session_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    source_channel="web",
                    title="First",
                    created_at=base,
                ),
                ChatSession(
                    id=second_session_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    source_channel="miniprogram",
                    title="Second",
                    created_at=base + timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()

    yield {
        "session_factory": session_factory,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "first_session_id": first_session_id,
        "second_session_id": second_session_id,
    }

    async with session_factory() as db:
        await db.execute(
            delete(AgentUserOnboarding).where(
                AgentUserOnboarding.agent_id == agent_id,
                AgentUserOnboarding.user_id == user_id,
            )
        )
        await db.execute(
            delete(ChatMessage).where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.user_id == user_id,
            )
        )
        await db.execute(
            delete(ChatSession).where(
                ChatSession.id.in_([first_session_id, second_session_id])
            )
        )
        await db.execute(delete(Agent).where(Agent.id == agent_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_only_earliest_pristine_platform_session_is_eligible(first_session_pair):
    async with first_session_pair["session_factory"]() as db:
        first = await resolve_onboarding_eligibility(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )
        second = await resolve_onboarding_eligibility(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["second_session_id"],
        )

    assert first.required is True
    assert first.reason == "required"
    assert second.required is False
    assert second.reason == "not_first_session"


@pytest.mark.asyncio
async def test_existing_pair_history_blocks_late_onboarding(first_session_pair):
    async with first_session_pair["session_factory"]() as db:
        db.add(
            ChatMessage(
                agent_id=first_session_pair["agent_id"],
                user_id=first_session_pair["user_id"],
                role="user",
                content="already spoke",
                conversation_id=str(first_session_pair["second_session_id"]),
            )
        )
        await db.commit()

        eligibility = await resolve_onboarding_eligibility(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )

    assert eligibility.required is False
    assert eligibility.reason == "existing_history"


@pytest.mark.asyncio
async def test_database_claim_allows_only_one_greeting(first_session_pair):
    async with first_session_pair["session_factory"]() as first_db:
        first_claim = await claim_onboarding_greeting(
            first_db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )
    async with first_session_pair["session_factory"]() as second_db:
        second_claim = await claim_onboarding_greeting(
            second_db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )

    assert first_claim.acquired is True
    assert second_claim.acquired is False
    assert second_claim.reason == "in_progress"

    async with first_session_pair["session_factory"]() as db:
        row = await db.get(
            AgentUserOnboarding,
            (
                first_session_pair["agent_id"],
                first_session_pair["user_id"],
            ),
        )
        assert row is not None
        assert row.phase == PHASE_PENDING

        assert first_claim.claimed_at is not None
        released = await release_onboarding_claim(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_claim.claimed_at,
        )
        assert released is True


@pytest.mark.asyncio
async def test_real_message_wins_before_trigger(first_session_pair):
    async with first_session_pair["session_factory"]() as db:
        phase = await claim_normal_first_turn(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
        )
    async with first_session_pair["session_factory"]() as db:
        greeting_claim = await claim_onboarding_greeting(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )
        stored_phase = await db.scalar(
            select(AgentUserOnboarding.phase).where(
                AgentUserOnboarding.agent_id == first_session_pair["agent_id"],
                AgentUserOnboarding.user_id == first_session_pair["user_id"],
            )
        )

    assert phase == PHASE_COMPLETED
    assert greeting_claim.acquired is False
    assert greeting_claim.reason == "already_started"
    assert stored_phase == PHASE_COMPLETED


@pytest.mark.asyncio
async def test_completed_first_contact_invalidates_an_existing_greeting_claim(
    first_session_pair,
):
    async with first_session_pair["session_factory"]() as db:
        claim = await claim_onboarding_greeting(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )
    assert claim.acquired is True
    assert claim.claimed_at is not None

    async with first_session_pair["session_factory"]() as db:
        assert await onboarding_claim_is_current(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            claim.claimed_at,
        )

    # A fixed welcome on another connection wins the same first-contact slot.
    async with first_session_pair["session_factory"]() as db:
        await mark_onboarded(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
        )

    async with first_session_pair["session_factory"]() as db:
        assert not await onboarding_claim_is_current(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            claim.claimed_at,
        )
        assert not await release_onboarding_claim(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            claim.claimed_at,
        )


@pytest.mark.asyncio
async def test_fixed_welcome_can_take_over_only_before_onboarding_output(
    first_session_pair,
):
    async with first_session_pair["session_factory"]() as db:
        claim = await claim_onboarding_greeting(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )
    assert claim.acquired is True
    assert claim.claimed_at is not None

    async with first_session_pair["session_factory"]() as db:
        assert await claim_fixed_welcome_slot(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
        )
        await db.commit()
        assert not await onboarding_claim_is_current(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            claim.claimed_at,
        )


@pytest.mark.asyncio
async def test_fixed_welcome_loses_after_onboarding_publishes_output(
    first_session_pair,
):
    async with first_session_pair["session_factory"]() as db:
        claim = await claim_onboarding_greeting(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            first_session_pair["first_session_id"],
        )
    assert claim.claimed_at is not None

    async with first_session_pair["session_factory"]() as db:
        assert await mark_onboarding_phase(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
            PHASE_GREETED,
            expected_phase=PHASE_PENDING,
            expected_onboarded_at=claim.claimed_at,
        )
        greeted = await db.get(
            AgentUserOnboarding,
            (
                first_session_pair["agent_id"],
                first_session_pair["user_id"],
            ),
        )
        assert greeted is not None
        assert greeted.onboarded_at > claim.claimed_at

    async with first_session_pair["session_factory"]() as db:
        assert not await claim_fixed_welcome_slot(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
        )


@pytest.mark.asyncio
async def test_real_message_waits_until_visible_greeting_is_durable(
    first_session_pair,
):
    visible_at = datetime.now(timezone.utc)
    async with first_session_pair["session_factory"]() as db:
        db.add(
            AgentUserOnboarding(
                agent_id=first_session_pair["agent_id"],
                user_id=first_session_pair["user_id"],
                phase=PHASE_GREETED,
                onboarded_at=visible_at,
            )
        )
        db.add(
            ChatMessage(
                agent_id=first_session_pair["agent_id"],
                user_id=first_session_pair["user_id"],
                role="assistant",
                content="older unrelated reply",
                conversation_id=str(first_session_pair["first_session_id"]),
                created_at=visible_at - timedelta(minutes=1),
            )
        )
        await db.commit()

    async with first_session_pair["session_factory"]() as db:
        assert (
            await claim_normal_first_turn(
                db,
                first_session_pair["agent_id"],
                first_session_pair["user_id"],
            )
            == PHASE_PENDING
        )

    async with first_session_pair["session_factory"]() as db:
        db.add(
            ChatMessage(
                agent_id=first_session_pair["agent_id"],
                user_id=first_session_pair["user_id"],
                role="assistant",
                content="durable greeting",
                conversation_id=str(first_session_pair["first_session_id"]),
                created_at=visible_at + timedelta(seconds=1),
            )
        )
        await db.commit()

    async with first_session_pair["session_factory"]() as db:
        assert (
            await claim_normal_first_turn(
                db,
                first_session_pair["agent_id"],
                first_session_pair["user_id"],
            )
            == PHASE_GREETED
        )


@pytest.mark.asyncio
async def test_real_message_recovers_stale_greeted_without_message(
    first_session_pair,
):
    stale_started_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    async with first_session_pair["session_factory"]() as db:
        db.add(
            AgentUserOnboarding(
                agent_id=first_session_pair["agent_id"],
                user_id=first_session_pair["user_id"],
                phase=PHASE_GREETED,
                onboarded_at=stale_started_at,
            )
        )
        await db.commit()

    async with first_session_pair["session_factory"]() as db:
        phase = await claim_normal_first_turn(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
        )

    assert phase == PHASE_COMPLETED


@pytest.mark.asyncio
async def test_real_message_recovers_stale_greeting_claim(first_session_pair):
    stale_started_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    async with first_session_pair["session_factory"]() as db:
        db.add(
            AgentUserOnboarding(
                agent_id=first_session_pair["agent_id"],
                user_id=first_session_pair["user_id"],
                phase=PHASE_PENDING,
                onboarded_at=stale_started_at,
            )
        )
        await db.commit()

    async with first_session_pair["session_factory"]() as db:
        phase = await claim_normal_first_turn(
            db,
            first_session_pair["agent_id"],
            first_session_pair["user_id"],
        )

    assert phase == PHASE_COMPLETED
