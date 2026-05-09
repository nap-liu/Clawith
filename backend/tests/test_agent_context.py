"""Tests for build_agent_context's is_group / Current Conversation logic."""

from __future__ import annotations

import uuid

import pytest

# Import the full model graph so FK references resolve at table-mapping time.
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.agent import Agent
from app.database import async_session, engine
from app.services.agent_context import build_agent_context


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    """Dispose the global async engine before each test."""
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_basic_agent() -> uuid.UUID:
    """Seed a minimal Identity → User → Agent chain so build_agent_context's
    internal DB queries succeed; return the agent id.

    Agent.creator_id is a NOT NULL FK to users — a random UUID will FK-violate.
    """
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"creator_{suffix}",
            email=f"creator_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Test Agent",
            creator_id=user.id,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def test_p2p_includes_current_conversation():
    """P2P 场景 (is_group=False): 必须含 ## Current Conversation 段."""
    agent_id = await _seed_basic_agent()
    static_p, dynamic_p = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name="Alice",
        is_group=False,
    )
    assert "## Current Conversation" in dynamic_p
    assert "Alice" in dynamic_p
    # The Message Sender Tag rules block is unconditional — it must be in
    # static_parts even for P2P (regression guard against someone making
    # the rules-block injection conditional on is_group).
    assert "## Message Sender Tag (Group Chat)" in static_p


async def test_group_excludes_current_conversation():
    """群聊场景 (is_group=True): 不含 ## Current Conversation 段."""
    agent_id = await _seed_basic_agent()
    static_p, dynamic_p = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name="Alice",
        is_group=True,
    )
    assert "## Current Conversation" not in dynamic_p


async def test_static_includes_message_sender_tag_section():
    """static_prompt (system prompt) 必含新加的 ## Message Sender Tag 段."""
    agent_id = await _seed_basic_agent()
    static_p, _ = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name=None,
        is_group=False,
    )
    assert "## Message Sender Tag (Group Chat)" in static_p
    assert "<sender id=" in static_p
    assert "VERY BEGINNING" in static_p


async def test_no_user_name_no_current_conversation_either_mode():
    """current_user_name=None 时, P2P 和 group 都不应注入 Current Conversation."""
    agent_id = await _seed_basic_agent()
    for is_group in (False, True):
        _, dynamic_p = await build_agent_context(
            agent_id,
            "Test Agent",
            "role",
            current_user_name=None,
            is_group=is_group,
        )
        assert "## Current Conversation" not in dynamic_p


async def test_static_message_sender_tag_section_documents_stable_user_id():
    """The Message Sender Tag section must tell the LLM the `id` is the
    platform's stable user identifier (not a session-scoped UUID), so the
    model can safely feed it into tool calls."""
    agent_id = await _seed_basic_agent()
    static_p, _ = await build_agent_context(
        agent_id,
        "Test Agent",
        "role",
        current_user_name=None,
        is_group=False,
    )
    # The section is present
    assert "## Message Sender Tag (Group Chat)" in static_p
    # The id stability invariant must be stated explicitly
    assert "stable user identifier" in static_p
    assert "across sessions" in static_p
    assert "NOT a session-scoped" in static_p or "not a session-scoped" in static_p.lower()
