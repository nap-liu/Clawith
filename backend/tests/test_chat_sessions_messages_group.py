"""Integration tests for chat_sessions.get_session_messages group-chat sender resolution."""

from __future__ import annotations

import uuid

import pytest

from app.models.user import Identity, User  # noqa: F401
from app.models.agent import Agent  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.audit import ChatMessage  # noqa: F401
from app.models.chat_session import ChatSession
from app.database import async_session, engine

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_user(suffix: str, display_name: str) -> User:
    async with async_session() as db:
        ident = Identity(
            username=f"u_{suffix}",
            email=f"u_{suffix}@test.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name=display_name, role="member", is_active=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def _seed_agent(creator_user_id) -> uuid.UUID:
    async with async_session() as db:
        agent = Agent(name="GroupTestAgent", creator_id=creator_user_id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def _seed_group_session(agent_id, owner_user_id, conv_id_str: str) -> uuid.UUID:
    """Seed a group ChatSession AND the matching chat_messages with FK bypass."""
    sess_id = uuid.uuid4()
    # ChatSession bound by agent_id FK; insert via ORM after agent exists
    async with async_session() as db:
        session = ChatSession(
            id=sess_id,
            agent_id=agent_id,
            user_id=owner_user_id,
            title="Group Test",
            source_channel="dingtalk",
            external_conv_id=conv_id_str,
            is_group=True,
            group_name="Group Test",
        )
        db.add(session)
        await db.commit()
    return sess_id


async def _insert_messages_bypass_fk(rows: list[dict]) -> None:
    """Insert chat_messages with FK checks disabled (postgres-only).

    Mirrors the pattern in test_chat_history.py — keeps the test self-contained
    without forcing every test to seed a full Agent + Tenant + Participant chain.
    """
    from sqlalchemy import text
    from datetime import datetime, timezone

    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        for row in rows:
            await db.execute(
                text(
                    "INSERT INTO chat_messages"
                    " (id, agent_id, user_id, role, content, conversation_id, created_at)"
                    " VALUES (:id, :agent_id, :user_id, :role, :content, :conv_id, :created_at)"
                ),
                {
                    "id": str(row["id"]),
                    "agent_id": str(row["agent_id"]),
                    "user_id": str(row["user_id"]),
                    "role": row["role"],
                    "content": row["content"],
                    "conv_id": row["conv_id"],
                    "created_at": row.get("created_at") or datetime.now(timezone.utc),
                },
            )
        await db.commit()
        await db.execute(text("SET session_replication_role = DEFAULT"))
        await db.commit()


async def test_group_messages_api_returns_sender_user_id_and_sender_name():
    """get_session_messages for a group session must return sender_user_id +
    sender_name on every user-role message, resolved from User.display_name."""
    from datetime import datetime, timedelta, timezone
    from app.api.chat_sessions import get_session_messages

    # Create owner (creator) and two distinct group speakers.
    # Use random suffixes to avoid unique-constraint collisions across test runs.
    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"owner_{run}", "Owner")
    alice = await _seed_user(f"alice_{run}", "Alice")
    bob = await _seed_user(f"bob_{run}", "Bob")

    agent_id = await _seed_agent(owner.id)
    sess_id = await _seed_group_session(agent_id, owner.id, f"dingtalk_group_{uuid.uuid4().hex[:8]}")

    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {"id": uuid.uuid4(), "agent_id": agent_id, "user_id": alice.id,
         "role": "user", "content": "alice msg", "conv_id": str(sess_id),
         "created_at": now - timedelta(seconds=300)},
        {"id": uuid.uuid4(), "agent_id": agent_id, "user_id": owner.id,
         "role": "assistant", "content": "agent reply", "conv_id": str(sess_id),
         "created_at": now - timedelta(seconds=200)},
        {"id": uuid.uuid4(), "agent_id": agent_id, "user_id": bob.id,
         "role": "user", "content": "bob msg", "conv_id": str(sess_id),
         "created_at": now - timedelta(seconds=100)},
    ])

    # Call the FastAPI handler directly — we need an authorized current_user.
    # Build a minimal mock current_user that the permission check accepts.
    # Simplest path: pass owner (who created the agent and is the session owner).
    async with async_session() as db:
        out = await get_session_messages(
            agent_id=agent_id,
            session_id=sess_id,
            current_user=owner,
            db=db,
        )

    user_msgs = [m for m in out if m["role"] == "user"]
    assert len(user_msgs) == 2
    assert user_msgs[0]["sender_user_id"] == str(alice.id)
    assert user_msgs[0]["sender_name"] == "Alice"
    assert user_msgs[1]["sender_user_id"] == str(bob.id)
    assert user_msgs[1]["sender_name"] == "Bob"

    # Assistant rows are NOT decorated with sender_user_id (group spec carves
    # out only user-role messages — agent rows render with the agent's own
    # avatar/name in the UI).
    assistant_msgs = [m for m in out if m["role"] == "assistant"]
    assert all("sender_user_id" not in m for m in assistant_msgs)


async def test_p2p_messages_api_does_not_add_sender_user_id_field():
    """For non-group sessions get_session_messages must NOT add sender_user_id /
    sender_name on user messages — preserve byte-identical legacy behavior."""
    from datetime import datetime, timedelta, timezone
    from app.api.chat_sessions import get_session_messages

    run = uuid.uuid4().hex[:8]
    owner = await _seed_user(f"p2pown_{run}", "P2POwner")
    agent_id = await _seed_agent(owner.id)

    # Seed a P2P session
    conv_id = f"web_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        sess = ChatSession(
            agent_id=agent_id,
            user_id=owner.id,
            title="P2P Test",
            source_channel="web",
            external_conv_id=conv_id,
            is_group=False,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        sess_id = sess.id

    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk([
        {"id": uuid.uuid4(), "agent_id": agent_id, "user_id": owner.id,
         "role": "user", "content": "p2p hi", "conv_id": str(sess_id),
         "created_at": now - timedelta(seconds=100)},
    ])

    async with async_session() as db:
        out = await get_session_messages(
            agent_id=agent_id,
            session_id=sess_id,
            current_user=owner,
            db=db,
        )

    user_msgs = [m for m in out if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert "sender_user_id" not in user_msgs[0]
    # sender_name is also absent for non-group, non-A2A
    assert "sender_name" not in user_msgs[0]
