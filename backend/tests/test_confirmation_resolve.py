"""Tests for confirmation_service.resolve_confirmation.

Covers:
1. confirm with action → _execute_tool_direct awaited, status==executed, _run_continuation called
2. confirm no action (pure gate) → status==executed, result=="用户已确认", no _execute_tool_direct
3. _execute_tool_direct raises → status==failed, result starts with "Error"
4. cancel → status==cancelled, _run_continuation text contains "拒绝"
5. idempotent: second resolve on already-executed → no re-execution
6. expired (expires_at < now, status pending) → status==expired, no execution
"""

import asyncio
import uuid
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import async_session, engine, Base
from app.models.agent_confirmation import AgentConfirmation
from app.models.agent import Agent  # noqa: F401 — FK dependency for create_all
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.services import confirmation_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _setup_table():
    """Ensure agent_confirmations table exists (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[AgentConfirmation.__table__], checkfirst=True
            )
        )
    yield
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_agent() -> tuple[uuid.UUID, uuid.UUID]:
    """Seed Tenant + Identity + User + Agent; return (agent.id, user.id)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()

        identity = Identity(username=f"u_{suffix}", email=f"{suffix}@t.local", password_hash="x")
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id,
            display_name="Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()

        agent = Agent(name=f"Agent_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id, user.id


async def _insert_pending(
    agent_id: uuid.UUID,
    *,
    action: dict | None = None,
    status: str = "pending",
    expires_at: datetime.datetime | None = None,
    conversation_id: str = "conv-test",
    chat_session_id: uuid.UUID | None = None,
) -> AgentConfirmation:
    """Insert an AgentConfirmation row directly, bypassing create_confirmation."""
    if expires_at is None:
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
    async with async_session() as db:
        c = AgentConfirmation(
            agent_id=agent_id,
            conversation_id=conversation_id,
            chat_session_id=chat_session_id,
            source_channel="web",
            title="测试操作",
            summary="这是测试摘要",
            action=action,
            risk_level="high",
            status=status,
            expires_at=expires_at,
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)
        return c


# ---------------------------------------------------------------------------
# Test 1: confirm with action → tool executed, status==executed
# ---------------------------------------------------------------------------


async def test_confirm_with_action_executes_tool():
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(
        agent_id,
        action={"tool": "sql_execute", "args": {"sql": "SELECT 1"}},
    )

    tool_result = "rows: [{'1': 1}]"

    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(return_value=tool_result),
        ) as mock_exec,
        patch(
            "app.services.confirmation_service._broadcast",
            new=AsyncMock(),
        ) as mock_broadcast,
        patch(
            "app.services.confirmation_service._run_continuation",
            new=AsyncMock(),
        ) as mock_cont,
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id,
            agent_id=agent_id,
            decision="confirm",
            resolving_user_id=user_id,
        )

    assert result_c.status == "executed"
    assert result_c.result == tool_result
    # Executes through the full dispatch (execute_tool) with autonomy bypassed.
    mock_exec.assert_awaited_once()
    _ex_args, _ex_kwargs = mock_exec.await_args
    assert _ex_args[0] == "sql_execute" and _ex_args[1] == {"sql": "SELECT 1"}
    assert _ex_args[2] == agent_id
    assert _ex_kwargs.get("skip_autonomy") is True
    mock_broadcast.assert_awaited()
    mock_cont.assert_awaited_once()
    cont_kwargs = mock_cont.await_args.kwargs
    assert "已确认" in cont_kwargs["text"]
    assert tool_result in cont_kwargs["text"]


# ---------------------------------------------------------------------------
# Test 2: confirm with no action (pure gate) → no tool call, status==executed
# ---------------------------------------------------------------------------


async def test_confirm_no_action_pure_gate():
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(agent_id, action=None)

    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(),
        ) as mock_exec,
        patch("app.services.confirmation_service._broadcast", new=AsyncMock()),
        patch(
            "app.services.confirmation_service._run_continuation",
            new=AsyncMock(),
        ) as mock_cont,
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id,
            agent_id=agent_id,
            decision="confirm",
            resolving_user_id=user_id,
        )

    assert result_c.status == "executed"
    assert result_c.result == "用户已确认"
    mock_exec.assert_not_awaited()
    mock_cont.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 3: tool raises → status==failed, result starts with "Error"
# ---------------------------------------------------------------------------


async def test_confirm_tool_raises_sets_failed():
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(
        agent_id,
        action={"tool": "write_file", "args": {"path": "/etc/passwd", "content": "x"}},
    )

    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(side_effect=PermissionError("access denied")),
        ) as mock_exec,
        patch("app.services.confirmation_service._broadcast", new=AsyncMock()),
        patch("app.services.confirmation_service._run_continuation", new=AsyncMock()),
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id,
            agent_id=agent_id,
            decision="confirm",
            resolving_user_id=user_id,
        )

    assert result_c.status == "failed"
    assert result_c.result is not None
    assert result_c.result.lower().startswith("error")
    mock_exec.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 4: cancel → status==cancelled, text contains "拒绝", no tool execution
# ---------------------------------------------------------------------------


async def test_cancel_sets_cancelled_and_no_exec():
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(
        agent_id,
        action={"tool": "sql_execute", "args": {"sql": "DELETE FROM users"}},
    )

    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(),
        ) as mock_exec,
        patch("app.services.confirmation_service._broadcast", new=AsyncMock()),
        patch(
            "app.services.confirmation_service._run_continuation",
            new=AsyncMock(),
        ) as mock_cont,
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id,
            agent_id=agent_id,
            decision="cancel",
            resolving_user_id=user_id,
        )

    assert result_c.status == "cancelled"
    mock_exec.assert_not_awaited()
    mock_cont.assert_awaited_once()
    cont_kwargs = mock_cont.await_args.kwargs
    assert "取消" in cont_kwargs["text"]


# ---------------------------------------------------------------------------
# Test 5: idempotent — second resolve on already-executed skips re-execution
# ---------------------------------------------------------------------------


async def test_idempotent_already_executed():
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(agent_id, action={"tool": "sql_execute", "args": {}}, status="executed")

    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(),
        ) as mock_exec,
        patch("app.services.confirmation_service._broadcast", new=AsyncMock()),
        patch("app.services.confirmation_service._run_continuation", new=AsyncMock()) as mock_cont,
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id,
            agent_id=agent_id,
            decision="confirm",
            resolving_user_id=user_id,
        )

    assert result_c.status == "executed"
    mock_exec.assert_not_awaited()
    mock_cont.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 6: expired (expires_at < now, status pending) → status==expired, no exec
# ---------------------------------------------------------------------------


async def test_expired_returns_expired_status():
    agent_id, user_id = await _make_agent()
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    c = await _insert_pending(agent_id, action={"tool": "sql_execute", "args": {}}, expires_at=past)

    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(),
        ) as mock_exec,
        patch(
            "app.services.confirmation_service._broadcast",
            new=AsyncMock(),
        ) as mock_broadcast,
        patch("app.services.confirmation_service._run_continuation", new=AsyncMock()) as mock_cont,
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id,
            agent_id=agent_id,
            decision="confirm",
            resolving_user_id=user_id,
        )

    assert result_c.status == "expired"
    mock_exec.assert_not_awaited()
    mock_cont.assert_not_awaited()
    mock_broadcast.assert_awaited()


# ---------------------------------------------------------------------------
# Test 7: B1 regression — two concurrent resolves execute the action exactly once
# ---------------------------------------------------------------------------


async def test_concurrent_resolve_executes_action_once():
    """SELECT ... FOR UPDATE must serialize concurrent resolves of the same pending
    card so the carried (dangerous) action runs exactly once — not once per click."""
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(
        agent_id,
        action={"tool": "sql_execute", "args": {"sql": "INSERT INTO orders VALUES (1)"}},
    )

    exec_mock = AsyncMock(return_value="ok rows=1")
    with (
        patch("app.services.confirmation_service.execute_tool", new=exec_mock),
        patch("app.services.confirmation_service._broadcast", new=AsyncMock()),
        patch("app.services.confirmation_service._run_continuation", new=AsyncMock()),
    ):
        results = await asyncio.wait_for(
            asyncio.gather(
                confirmation_service.resolve_confirmation(
                    confirmation_id=c.id, agent_id=agent_id, decision="confirm", resolving_user_id=user_id
                ),
                confirmation_service.resolve_confirmation(
                    confirmation_id=c.id, agent_id=agent_id, decision="confirm", resolving_user_id=user_id
                ),
            ),
            timeout=20,
        )

    # The dangerous action executed exactly once despite two concurrent resolves.
    assert exec_mock.await_count == 1, f"expected exactly one execution, got {exec_mock.await_count}"
    assert all(r.status == "executed" for r in results)

    # The persisted row is in a single terminal state.
    from sqlalchemy import select as _select

    async with async_session() as db:
        fresh = (
            await db.execute(_select(AgentConfirmation).where(AgentConfirmation.id == c.id))
        ).scalar_one()
    assert fresh.status == "executed"


# ---------------------------------------------------------------------------
# Test 8: M2 — unsupported-tool sentinel is classified as failed, not executed
# ---------------------------------------------------------------------------


async def test_unsupported_tool_marked_failed():
    agent_id, user_id = await _make_agent()
    c = await _insert_pending(agent_id, action={"tool": "some_unmapped_tool", "args": {}})

    sentinel = "Tool some_unmapped_tool does not support post-approval execution"
    with (
        patch(
            "app.services.confirmation_service.execute_tool",
            new=AsyncMock(return_value=sentinel),
        ),
        patch("app.services.confirmation_service._broadcast", new=AsyncMock()),
        patch("app.services.confirmation_service._run_continuation", new=AsyncMock()),
    ):
        result_c = await confirmation_service.resolve_confirmation(
            confirmation_id=c.id, agent_id=agent_id, decision="confirm", resolving_user_id=user_id
        )

    assert result_c.status == "failed"
    assert result_c.result == sentinel
