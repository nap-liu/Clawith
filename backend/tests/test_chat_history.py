"""Integration tests for chat_history.load_history_for_llm."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from unittest.mock import patch

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


async def test_load_history_group_lookup_failure_falls_back_to_anonymous():
    """If the display_name batch lookup raises, the loader logs a warning
    and returns history WITHOUT sender wrapping (spec §4.5)."""
    agent_id = uuid.uuid4()
    u_alice, u_bob = await _seed_two_users()
    conv_id = await _seed_group_history(agent_id, u_alice, u_bob)

    with patch(
        "app.services.chat_history._batch_load_display_names",
        side_effect=RuntimeError("simulated DB outage"),
    ):
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
    # Crucial: fallback must be UNWRAPPED — no <sender> prefix at all.
    for m in user_msgs:
        assert not m["content"].startswith("<sender")
        assert "<sender" not in m["content"]
    # Original content preserved
    assert user_msgs[0]["content"] == "昨天的报告写好了吗？"
    assert user_msgs[1]["content"] == "帮我订下午3点会议室"


async def test_load_history_expands_tool_call_rows():
    """tool_call 行必须被展开成 assistant(tool_calls) + tool(result) 对,
    而不是原样保留为 role=tool_call —— 否则它会在喂给 LLM 前被丢弃,
    数字员工跨轮就看不到自己之前的工具调用历史(IM 通道的核心缺陷)。"""
    agent_id = uuid.uuid4()
    u_alice, _ = await _seed_two_users()
    conv_id = f"test_tc_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_alice.id,
                "role": "user",
                "content": "查下上海天气",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=100),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_alice.id,
                "role": "tool_call",
                "content": json.dumps(
                    {"name": "get_weather", "args": {"city": "SH"}, "status": "done", "result": "sunny"}
                ),
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=50),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": u_alice.id,
                "role": "assistant",
                "content": "今天上海晴",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=10),
            },
        ]
    )
    async with async_session() as db:
        history = await load_history_for_llm(db, agent_id=agent_id, conversation_id=conv_id, ctx_size=50)

    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "tool_call" not in roles  # no raw tool_call row leaks through

    tc_asst = history[1]
    assert tc_asst["content"] is None
    assert tc_asst["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(tc_asst["tool_calls"][0]["function"]["arguments"]) == {"city": "SH"}

    tool_msg = history[2]
    assert tool_msg["tool_call_id"] == tc_asst["tool_calls"][0]["id"]
    assert tool_msg["content"] == "sunny"


@asynccontextmanager
async def _fk_bypass_session():
    """An async_session whose FK triggers are disabled, so persist_tool_call
    can write a chat_messages row without a real agent/user present."""
    async with async_session() as db:
        await db.execute(text("SET session_replication_role = replica"))
        yield db


async def test_persist_tool_call_done_roundtrips_via_canonical_schema():
    """A done tool call persisted via the shared writer must read back through
    load_history_for_llm as the canonical assistant+tool pair, with secret args
    kept RAW — the LLM replays this verbatim, so masking storage would make the
    model copy ``"******"`` into new tool calls. Secrets are masked only at the
    human-facing display boundary (asserted below)."""
    from app.services.chat_history import parse_tool_call_for_display, persist_tool_call

    agent_id = uuid.uuid4()
    conv_id = f"test_persist_{uuid.uuid4().hex[:8]}"
    await persist_tool_call(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        evt={
            "name": "read_file",
            "call_id": "c1",
            "args": {"path": "x.txt", "password": "SECRET"},
            "status": "done",
            "result": "file body",
            "reasoning_content": "reading",
        },
    )
    async with async_session() as db:
        history = await load_history_for_llm(db, agent_id=agent_id, conversation_id=conv_id, ctx_size=50)

    assert [m["role"] for m in history] == ["assistant", "tool"]
    fn = history[0]["tool_calls"][0]["function"]
    assert fn["name"] == "read_file"
    args = json.loads(fn["arguments"])
    assert args["path"] == "x.txt"
    assert args["password"] == "SECRET"  # RAW in the LLM-replay path (not masked at storage)
    assert history[1]["content"] == "file body"

    # The masking moved to the display boundary, not gone: what a human sees is
    # masked, what the model replays is raw.
    from sqlalchemy import select as _select

    async with async_session() as db:
        _rows = (
            await db.execute(
                _select(ChatMessage).where(
                    ChatMessage.conversation_id == conv_id,
                    ChatMessage.role == "tool_call",
                )
            )
        ).scalars().all()
    assert len(_rows) == 1
    display = parse_tool_call_for_display(_rows[0].content)
    assert display["toolArgs"]["password"] == "******"
    assert display["toolArgs"]["path"] == "x.txt"


async def test_persist_assistant_reply_roundtrips():
    """The assistant reply persisted via the shared writer reads back through
    load_history_for_llm with its content and optional thinking intact. Using
    its own session (not the channel's long-lived transaction) stamps
    created_at at save time — after the tool loop — so the web UI orders it
    after the turn's tool calls instead of folding it into the analysis card."""
    from app.services.chat_history import persist_assistant_reply

    agent_id = uuid.uuid4()
    conv_id = f"test_areply_{uuid.uuid4().hex[:8]}"
    await persist_assistant_reply(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        content="上海今天晴",
        thinking="先查天气再回答",
    )
    async with async_session() as db:
        history = await load_history_for_llm(db, agent_id=agent_id, conversation_id=conv_id, ctx_size=50)

    assert [m["role"] for m in history] == ["assistant"]
    assert history[0]["content"] == "上海今天晴"


async def test_persist_assistant_reply_skips_empty_content():
    """No-op for empty/blank replies — never write a blank assistant row."""
    from app.services.chat_history import persist_assistant_reply

    agent_id = uuid.uuid4()
    conv_id = f"test_areply_empty_{uuid.uuid4().hex[:8]}"
    await persist_assistant_reply(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        content="",
    )
    async with async_session() as db:
        history = await load_history_for_llm(db, agent_id=agent_id, conversation_id=conv_id, ctx_size=50)
    assert history == []


async def test_persist_tool_call_running_status_is_not_stored():
    """Only completed (done) tool calls are persisted; running is a no-op."""
    from app.services.chat_history import persist_tool_call

    agent_id = uuid.uuid4()
    conv_id = f"test_persist_run_{uuid.uuid4().hex[:8]}"
    await persist_tool_call(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        evt={"name": "x", "args": {}, "status": "running"},
    )
    async with async_session() as db:
        history = await load_history_for_llm(db, agent_id=agent_id, conversation_id=conv_id, ctx_size=50)
    assert history == []


async def test_assistant_reply_stamped_after_tool_calls():
    """Headline-fix regression guard: persist_assistant_reply uses its OWN
    session, so a reply written after the tool loop gets a created_at no earlier
    than the turn's tool_call rows — it sorts AFTER them instead of being folded
    into the web 'ran N tools' analysis card. Reverting the reply to the
    channel's long-lived request transaction (PostgreSQL now() = txn-start time)
    would stamp an earlier created_at and fail this. Compares timestamps
    directly, so it is independent of any sort tiebreak and never flaky (>=)."""
    from sqlalchemy import select as _select

    from app.services.chat_history import persist_assistant_reply, persist_tool_call

    agent_id = uuid.uuid4()
    conv_id = f"test_order_{uuid.uuid4().hex[:8]}"
    await persist_tool_call(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        evt={"name": "get_weather", "args": {"city": "SH"}, "status": "done", "result": "sunny"},
    )
    await persist_assistant_reply(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        content="今天上海晴",
    )
    async with async_session() as db:
        rows = (
            await db.execute(_select(ChatMessage).where(ChatMessage.conversation_id == conv_id))
        ).scalars().all()
    by_role = {r.role: r for r in rows}
    assert set(by_role) == {"tool_call", "assistant"}
    assert by_role["assistant"].created_at >= by_role["tool_call"].created_at


async def test_load_history_for_llm_drops_cancelled_turn_tail():
    """A /stop can leave the interrupted turn as user + tool_call rows with no
    final assistant reply. That tail must not be replayed before the next user
    message, otherwise strict providers can reject the role sequence."""
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conv_id = f"test_cancelled_tail_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "上一轮问题",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=40),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "assistant",
                "content": "上一轮回复",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=30),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "被 /stop 中断的问题",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=20),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "tool_call",
                "content": json.dumps(
                    {
                        "name": "web_search",
                        "args": {"q": "x"},
                        "status": "done",
                        "result": "partial",
                    },
                    ensure_ascii=False,
                ),
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=10),
            },
        ]
    )

    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
        )

    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "上一轮问题"),
        ("assistant", "上一轮回复"),
    ]


async def test_cleanup_incomplete_session_tail_deletes_cancelled_turn_rows():
    from sqlalchemy import select as _select

    from app.services.chat_history import cleanup_incomplete_session_tail

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conv_id = f"test_cleanup_tail_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "ok user",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=40),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "assistant",
                "content": "ok assistant",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=30),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "cancelled user",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=20),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "tool_call",
                "content": json.dumps(
                    {"name": "read_file", "args": {}, "status": "done", "result": "partial"},
                    ensure_ascii=False,
                ),
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=10),
            },
        ]
    )

    async with async_session() as db:
        deleted = await cleanup_incomplete_session_tail(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
        )
        await db.commit()

    assert deleted == 2
    async with async_session() as db:
        rows = (
            await db.execute(
                _select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all()
    assert [(row.role, row.content) for row in rows] == [
        ("user", "ok user"),
        ("assistant", "ok assistant"),
    ]
