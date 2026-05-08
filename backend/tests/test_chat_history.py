"""Integration tests for chat_history.load_history_for_llm."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

# Import the full model graph so FK references resolve at table-mapping time.
from app.models.user import Identity, User  # noqa: F401
from app.models.agent import Agent, AgentPermission, AgentTemplate  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.audit import ChatMessage  # noqa: F401
from app.database import async_session, engine
from app.services.chat_history import load_history_for_llm


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    """Dispose the global async engine before each test to avoid the
    'another operation is in progress' asyncpg pool issue between tests."""
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_two_users() -> tuple[User, User]:
    """Create two platform users with display names; commit and return."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        id1 = Identity(
            username=f"alice_{suffix}",
            email=f"alice_{suffix}@test.local",
            password_hash="x",
        )
        id2 = Identity(
            username=f"bob_{suffix}",
            email=f"bob_{suffix}@test.local",
            password_hash="x",
        )
        db.add_all([id1, id2])
        await db.flush()
        u_alice = User(
            identity_id=id1.id,
            display_name="Alice",
            role="member",
            is_active=True,
        )
        u_bob = User(
            identity_id=id2.id,
            display_name="Bob",
            role="member",
            is_active=True,
        )
        db.add_all([u_alice, u_bob])
        await db.commit()
        await db.refresh(u_alice)
        await db.refresh(u_bob)
        return u_alice, u_bob


async def _insert_messages_bypass_fk(rows: list[dict]) -> None:
    """Insert ChatMessage rows via raw SQL with FK checks disabled.

    ``rows`` is a list of dicts with keys:
      id, agent_id, user_id, role, content, conversation_id, created_at (optional)
    Using ``SET session_replication_role = replica`` disables FK triggers
    so tests can run against an otherwise-empty database (no pre-existing
    agents required).
    """
    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        for row in rows:
            created_at = row.get("created_at")
            if created_at is None:
                created_at = datetime.now(timezone.utc)
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
                    "conv_id": row["conversation_id"],
                    "created_at": created_at,
                },
            )
        await db.commit()
        await db.execute(text("SET session_replication_role = DEFAULT"))
        await db.commit()


async def _seed_group_history(agent_id: uuid.UUID, u_alice: User, u_bob: User) -> str:
    """Seed a synthetic group conversation: Alice → assistant → Bob → assistant.

    Explicit ``created_at`` offsets ensure deterministic chronological order
    even when all rows are inserted in the same DB transaction (which would
    give them identical server-side timestamps).
    """
    conv_id = f"test_group_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_alice.id,
                "role": "user",
                "content": "昨天的报告写好了吗？",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=300),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_alice.id,
                "role": "assistant",
                "content": "还在写",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=200),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_bob.id,
                "role": "user",
                "content": "帮我订下午3点会议室",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=100),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_bob.id,
                "role": "assistant",
                "content": "好的",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=50),
            },
        ]
    )
    return conv_id


async def test_load_history_p2p_unchanged():
    """is_group=False 与既有行为完全一致: user content 不带 sender 前缀."""
    agent_id = uuid.uuid4()
    u_alice, u_bob = await _seed_two_users()
    conv_id = await _seed_group_history(agent_id, u_alice, u_bob)
    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
        )
    user_msgs = [m for m in history if m["role"] == "user"]
    assert len(user_msgs) == 2
    for m in user_msgs:
        assert not m["content"].startswith("<sender ")


async def test_load_history_group_wraps_user_messages():
    """is_group=True 时每条 user 消息被 sender 标签包前缀, 历史里能区分发件人."""
    agent_id = uuid.uuid4()
    u_alice, u_bob = await _seed_two_users()
    conv_id = await _seed_group_history(agent_id, u_alice, u_bob)
    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
            is_group=True,
        )
    user_msgs = [m for m in history if m["role"] == "user"]
    assert len(user_msgs) == 2

    assert user_msgs[0]["content"].startswith(f'<sender id="{u_alice.id}">Alice</sender>\n')
    assert user_msgs[0]["content"].endswith("昨天的报告写好了吗？")

    assert user_msgs[1]["content"].startswith(f'<sender id="{u_bob.id}">Bob</sender>\n')
    assert user_msgs[1]["content"].endswith("帮我订下午3点会议室")


async def test_load_history_group_assistant_messages_untouched():
    """assistant / system / tool_call 消息不应该被 wrap."""
    agent_id = uuid.uuid4()
    u_alice, u_bob = await _seed_two_users()
    conv_id = await _seed_group_history(agent_id, u_alice, u_bob)
    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
            is_group=True,
        )
    assistant_msgs = [m for m in history if m["role"] == "assistant"]
    assert len(assistant_msgs) == 2
    for m in assistant_msgs:
        assert "<sender" not in m["content"]


async def test_load_history_group_unknown_user_falls_back():
    """如果 ChatMessage.user_id 指向不存在的 user, 用 Unknown 兜底而非崩溃.

    We insert the ghost message via raw SQL with FK checks disabled
    (SET session_replication_role = replica) to simulate an orphaned
    user_id that could arise from a hard delete / data migration in prod.
    """
    agent_id = uuid.uuid4()
    u_alice, _u_bob = await _seed_two_users()
    conv_id = f"test_orphan_{uuid.uuid4().hex[:8]}"
    ghost_uid = uuid.uuid4()  # 这个 user 不存在
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_alice.id,
                "role": "user",
                "content": "hi",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=100),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": ghost_uid,
                "role": "user",
                "content": "ghost",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=50),
            },
        ]
    )
    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
            is_group=True,
        )
    user_msgs = [m for m in history if m["role"] == "user"]
    assert user_msgs[0]["content"].startswith(f'<sender id="{u_alice.id}">Alice</sender>\n')
    assert user_msgs[1]["content"].startswith(f'<sender id="{ghost_uid}">Unknown</sender>\n')
