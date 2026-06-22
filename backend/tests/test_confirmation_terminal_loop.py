"""Integration tests for Task 4: request_confirmation as a turn-terminal tool.

Verifies that when the LLM calls request_confirmation:
  (a) The turn is terminated immediately (挟带动作不执行).
  (b) A pending AgentConfirmation row is created in the DB.
  (c) The pre-card assistant text is returned as the turn reply.
  (d) When action.tool is not in allowed_tool_names, the turn continues
      with an error message — confirmation is NOT created.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.database import async_session, engine, Base
from app.models.agent_confirmation import AgentConfirmation
from app.models.agent import Agent  # noqa: F401 — FK resolution
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.services.llm.caller import call_llm
from app.services.llm.client import LLMMessage, LLMResponse

pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────────
# DB setup
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def _setup_tables():
    """Ensure agent_confirmations table exists (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[AgentConfirmation.__table__],
                checkfirst=True,
            )
        )
    yield
    await engine.dispose()


async def _make_agent() -> tuple[uuid.UUID, uuid.UUID]:
    """Seed Tenant + Identity + User + Agent; return (agent.id, user.id)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()

        identity = Identity(
            username=f"u_{suffix}", email=f"{suffix}@t.local", password_hash="x"
        )
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id,
            display_name="U",
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


async def _fetch_confirmations(conversation_id: str) -> list[AgentConfirmation]:
    async with async_session() as db:
        from sqlalchemy import select as _select
        result = await db.execute(
            _select(AgentConfirmation).where(
                AgentConfirmation.conversation_id == conversation_id
            )
        )
        return list(result.scalars().all())


# ──────────────────────────────────────────────────────────────────────────────
# Fake LLM model
# ──────────────────────────────────────────────────────────────────────────────

class _FakeModel:
    provider = "qwen"
    model = "qwen-turbo"
    base_url = "https://example.invalid"
    api_key_encrypted = ""
    temperature = 0.7
    max_output_tokens = None
    request_timeout = 30.0
    id = "model-fake"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_conf_tool_call(
    title: str,
    summary: str,
    action_tool: str,
    action_args: dict,
    tc_id: str = "call_conf_1",
) -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": "request_confirmation",
            "arguments": json.dumps({
                "title": title,
                "summary": summary,
                "action": {"tool": action_tool, "args": action_args},
                "risk_level": "high",
            }),
        },
    }


def _make_finish_tool_call(content: str = "done", tc_id: str = "call_finish") -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": "finish",
            "arguments": json.dumps({"content": content}),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Common call_llm monkeypatches (everything except create_llm_client)
# ──────────────────────────────────────────────────────────────────────────────

def _patch_caller_collaborators(monkeypatch, tools_for_llm: list | None = None):
    """Patch away all DB/context collaborators used by call_llm."""
    if tools_for_llm is None:
        # Provide sql_execute as an allowed tool so action.tool validation passes.
        tools_for_llm = [
            {"type": "function", "function": {"name": "sql_execute", "description": "sql"}},
            {"type": "function", "function": {"name": "finish", "description": "finish"}},
            {
                "type": "function",
                "function": {"name": "request_confirmation", "description": "rc"},
            },
        ]

    monkeypatch.setattr(
        "app.services.llm.caller.get_max_tokens",
        lambda *a, **k: 1024,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_model_api_key",
        lambda m: "fake-key",
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config",
        AsyncMock(return_value=(50, None)),
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_user_name",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=tools_for_llm),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage",
        AsyncMock(return_value=None),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: valid request_confirmation terminates the turn
# ──────────────────────────────────────────────────────────────────────────────

async def test_request_confirmation_terminates_turn_without_executing(monkeypatch):
    """
    When LLM calls request_confirmation with action.tool=sql_execute (enabled):
      - turn ends immediately (returns pre-card assistant text)
      - sql_execute is NOT called
      - one pending AgentConfirmation is created in the DB
    """
    agent_id, user_id = await _make_agent()
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"

    assistant_text = "我准备创建这张采购单,请确认。"

    # Two-round fake client:
    #   round 1 → request_confirmation tool call (with pre-card text)
    #   (round 2 never reached because the loop should terminate after round 1)
    _round = [0]

    class _FakeClient:
        closed = False

        async def stream(self, messages, tools=None, temperature=None,
                         max_tokens=None, on_chunk=None, on_tool_delta=None,
                         on_thinking=None, **kwargs):
            _round[0] += 1
            if _round[0] == 1:
                return LLMResponse(
                    content=assistant_text,
                    tool_calls=[
                        _make_conf_tool_call(
                            title="创建采购单",
                            summary="金额 ¥48,500",
                            action_tool="sql_execute",
                            action_args={"sql": "INSERT INTO orders ..."},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=None,
                )
            # Should never be called
            raise AssertionError("LLM was called more than once — turn was not terminated")

        async def close(self):
            _FakeClient.closed = True

    fake_client = _FakeClient()
    _patch_caller_collaborators(monkeypatch)
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fake_client,
    )

    # Ensure sql_execute is never actually called
    sql_execute_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr(
        "app.services.llm.caller.execute_tool",
        sql_execute_mock,
    )

    reply = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "帮我创建采购单"}],
        agent_name="TestAgent",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=conv_id,
    )

    # (a) Turn returned immediately (only 1 LLM round)
    assert _round[0] == 1, f"Expected 1 LLM round, got {_round[0]}"

    # (b) Pre-card assistant text is returned
    assert assistant_text in reply, f"Expected pre-card text in reply, got: {reply!r}"

    # (c) sql_execute was NOT called
    sql_execute_mock.assert_not_awaited()

    # (d) One pending AgentConfirmation was created
    rows = await _fetch_confirmations(conv_id)
    assert len(rows) == 1, f"Expected 1 confirmation row, got {len(rows)}"
    assert rows[0].status == "pending"
    assert rows[0].title == "创建采购单"
    assert rows[0].risk_level == "high"


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: action.tool not in allowed_tool_names → error injected, loop continues
# ──────────────────────────────────────────────────────────────────────────────

async def test_request_confirmation_disabled_tool_does_not_terminate(monkeypatch):
    """
    When action.tool is NOT in the agent's allowed_tool_names:
      - The turn does NOT terminate immediately.
      - An error tool message is appended and the LLM loop continues.
      - No AgentConfirmation is created.
      - On the next round the LLM returns finish().
    """
    agent_id, user_id = await _make_agent()
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"

    _round = [0]
    # Tools for this test: sql_execute is ABSENT → disabled
    tools_for_llm = [
        {"type": "function", "function": {"name": "finish", "description": "finish"}},
        {
            "type": "function",
            "function": {"name": "request_confirmation", "description": "rc"},
        },
        # sql_execute intentionally omitted
    ]

    class _FakeClient2:
        closed = False
        captured_tool_messages: list[LLMMessage] = []
        captured_messages: list[LLMMessage] = []

        async def stream(self, messages, tools=None, temperature=None,
                         max_tokens=None, on_chunk=None, on_tool_delta=None,
                         on_thinking=None, **kwargs):
            _round[0] += 1
            if _round[0] == 1:
                # Round 1: LLM calls request_confirmation with disabled tool
                return LLMResponse(
                    content="",
                    tool_calls=[
                        _make_conf_tool_call(
                            title="操作",
                            summary="要做的事",
                            action_tool="sql_execute",  # NOT in allowed
                            action_args={"sql": "DELETE FROM ..."},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=None,
                )
            if _round[0] == 2:
                # Capture what messages arrived in api_messages
                _FakeClient2.captured_messages = list(messages)
                _FakeClient2.captured_tool_messages = [
                    m for m in messages if m.role == "tool"
                ]
                # Round 2: LLM finishes cleanly
                return LLMResponse(
                    content="",
                    tool_calls=[_make_finish_tool_call("抱歉,无法执行该操作")],
                    finish_reason="tool_calls",
                    usage=None,
                )
            raise AssertionError(f"Unexpected round {_round[0]}")

        async def close(self):
            _FakeClient2.closed = True

    fake_client2 = _FakeClient2()
    _patch_caller_collaborators(monkeypatch, tools_for_llm=tools_for_llm)
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fake_client2,
    )

    # Patch create_confirmation to assert it's NOT called
    create_conf_mock = AsyncMock()
    with patch("app.services.confirmation_service.create_confirmation", create_conf_mock):
        reply = await call_llm(
            model=_FakeModel(),
            messages=[{"role": "user", "content": "帮我删除数据"}],
            agent_name="TestAgent",
            role_description="",
            agent_id=agent_id,
            user_id=user_id,
            session_id=conv_id,
        )

    # Loop ran 2 rounds (not terminated on round 1)
    assert _round[0] == 2, f"Expected 2 LLM rounds, got {_round[0]}"

    # No confirmation was created
    create_conf_mock.assert_not_awaited()
    rows = await _fetch_confirmations(conv_id)
    assert len(rows) == 0, f"Expected 0 confirmation rows, got {len(rows)}"

    # An error tool message was injected into round 2's context
    error_msgs = [m for m in _FakeClient2.captured_tool_messages if "❌" in (m.content or "")]
    assert len(error_msgs) >= 1, "Expected at least one ❌ error tool message in round 2"
    assert "sql_execute" in error_msgs[0].content, f"Error should mention sql_execute: {error_msgs[0].content}"

    # The error tool message must be preceded by an assistant message carrying the
    # matching tool_calls — an orphaned role="tool" reply gets rejected (HTTP 400)
    # by strict providers on the next round. This guards the B1 regression.
    msgs = _FakeClient2.captured_messages
    err = error_msgs[0]
    err_idx = next(i for i, m in enumerate(msgs) if m is err)
    preceding_tc_ids = {
        tc.get("id")
        for m in msgs[:err_idx]
        if m.role == "assistant" and getattr(m, "tool_calls", None)
        for tc in (m.tool_calls or [])
    }
    assert err.tool_call_id in preceding_tc_ids, (
        "orphaned tool message: error tool_call_id has no matching tool_calls in any "
        f"preceding assistant message (got ids={preceding_tc_ids}, need={err.tool_call_id})"
    )

    # Final reply is from finish()
    assert reply == "抱歉,无法执行该操作"
