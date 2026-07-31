"""Persistent context-termination state for unrecoverable conversations."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.llm.session_context_guard import (
    CONTEXT_REQUEST_TOO_LARGE_MESSAGE,
    SESSION_CONTEXT_TERMINATED_MESSAGE,
    get_session_context_termination,
    terminate_session_context,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _fresh_pool():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_termination_is_persisted_and_blocks_later_turns():
    async with async_session() as db:
        suffix = uuid.uuid4().hex
        tenant = Tenant(
            name=f"Context guard {suffix}",
            slug=f"context-guard-{suffix}",
        )
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"context_guard_{suffix}",
            email=f"context-guard-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Context Guard Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Context Guard Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            title="context guard test",
            source_channel="web",
        )
        db.add(session)
        await db.commit()
        session_id = str(session.id)

    try:
        first = await terminate_session_context(
            session_id,
            "preflight_compaction_failed:validation_failed",
        )
        later = await get_session_context_termination(session_id)

        assert first == SESSION_CONTEXT_TERMINATED_MESSAGE
        assert later == SESSION_CONTEXT_TERMINATED_MESSAGE
        async with async_session() as db:
            reason = (
                await db.execute(
                    select(ChatSession.context_terminated_reason).where(
                        ChatSession.id == uuid.UUID(session_id)
                    )
                )
            ).scalar_one()
        assert reason == "preflight_compaction_failed:validation_failed"
    finally:
        async with async_session() as db:
            await db.execute(
                delete(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
            )
            await db.commit()


async def test_missing_chat_session_never_claims_that_it_was_stopped():
    missing_id = str(uuid.uuid4())

    reply = await terminate_session_context(missing_id, "provider_dispatch_oversized")
    later = await get_session_context_termination(missing_id)

    assert reply == CONTEXT_REQUEST_TOO_LARGE_MESSAGE
    assert "已停止" not in reply
    assert later is None


async def test_non_uuid_context_id_gets_simple_new_session_guidance():
    reply = await terminate_session_context("background-task", "context_limit")

    assert reply == CONTEXT_REQUEST_TOO_LARGE_MESSAGE
    assert reply == "上下文过长，请新开会话。"
