"""Observable round-budget durability contracts for the shared LLM loop."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.participant import Participant  # noqa: F401 - registers ChatMessage FK target
from app.models.tenant import Tenant
from app.models.user import User
from app.services import chat_history
from app.services.chat_history import expand_tool_call_row
from app.services.llm import caller
from app.services.llm.client import LLMResponse
from app.services import turn_recovery


pytestmark = pytest.mark.asyncio


class _BudgetClient:
    def __init__(self, tool_rounds: int, *, tool_content: str = "") -> None:
        self.tool_rounds = tool_rounds
        self.tool_content = tool_content
        self.dispatches: list[list[dict]] = []
        self.object_ids: list[list[int]] = []
        self.closed = False

    async def stream(self, messages, **_kwargs):
        self.dispatches.append(
            [
                {
                    "role": message.role,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": message.tool_calls,
                }
                for message in messages
            ]
        )
        self.object_ids.append([id(message) for message in messages])
        round_number = len(self.dispatches)
        if round_number <= self.tool_rounds:
            prefix = f"r{round_number}"
            return LLMResponse(
                content=self.tool_content,
                tool_calls=[
                    {
                        "id": f"{prefix}-{index}",
                        "type": "function",
                        "function": {
                            "name": "grep",
                            "arguments": json.dumps({"pattern": f"{prefix}-{index}"}),
                        },
                    }
                    for index in range(1, 4)
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="stable-final", finish_reason="stop")

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
async def _fresh_engine_pool():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def durable_identity():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with async_session() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Tool budget test",
                slug=f"tool-budget-{tenant_id.hex[:12]}",
            )
        )
        db.add(
            User(
                id=user_id,
                identity_id=None,
                tenant_id=tenant_id,
                display_name="Tool budget user",
                role="member",
                is_active=True,
            )
        )
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=user_id,
                name="Tool budget agent",
                agent_type="native",
                status="running",
            )
        )
        await db.commit()

    yield agent_id, user_id

    async with async_session() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.agent_id == agent_id))
        await db.execute(delete(Agent).where(Agent.id == agent_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()


def _model():
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="qwen",
        model="budget-test",
        base_url="https://example.invalid",
        api_key_encrypted="",
        temperature=0.0,
        max_output_tokens=1_024,
        request_timeout=30.0,
        supports_vision=False,
        context_window=1_000_000,
        context_usage_ratio=0.7,
        compact_trigger_ratio=0.85,
    )


def _patch_turn_dependencies(
    monkeypatch,
    client: _BudgetClient,
    *,
    tool_output_chars: int = 30_000,
) -> None:
    monkeypatch.setenv("CLAWITH_TOOL_OUTPUT_MAX_CHARS", "32000")
    monkeypatch.setenv("CLAWITH_MSG_TOOL_BUDGET", "64000")
    monkeypatch.setattr(caller, "create_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr(caller, "get_model_api_key", lambda _model: "key")
    monkeypatch.setattr(caller, "get_max_tokens", lambda *_args, **_kwargs: 1_024)
    monkeypatch.setattr(caller, "_get_agent_config", AsyncMock(return_value=(6, None)))
    monkeypatch.setattr(caller, "ensure_active_turn", AsyncMock(return_value=None))
    monkeypatch.setattr(caller, "record_token_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(
        caller,
        "execute_tool",
        AsyncMock(
            side_effect=lambda *_args, **kwargs: str(kwargs["tool_call_id"])[-1]
            * tool_output_chars
        ),
    )


def _tool_results(dispatch: list[dict], call_prefix: str) -> dict[str, str]:
    return {
        str(message["tool_call_id"]): str(message["content"])
        for message in dispatch
        if message["role"] == "tool"
        and str(message["tool_call_id"] or "").startswith(call_prefix)
    }


async def _durable_done_rows(agent_id: uuid.UUID, conversation_id: str) -> list[ChatMessage]:
    async with async_session() as db:
        result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == "tool_call",
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
        return [
            row
            for row in result.scalars().all()
            if json.loads(row.content).get("status") == "done"
        ]


@pytest.mark.parametrize("tool_output_chars", [30_000, 60_000])
async def test_two_rounds_are_durable_recoverable_and_cache_stable(
    monkeypatch,
    tmp_path,
    durable_identity,
    tool_output_chars,
):
    agent_id, user_id = durable_identity
    session_id = f"budget-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    client = _BudgetClient(tool_rounds=2)
    _patch_turn_dependencies(
        monkeypatch,
        client,
        tool_output_chars=tool_output_chars,
    )
    callback_events: list[dict] = []

    async def on_tool_call(event: dict):
        callback_events.append(dict(event))

    result = await caller.call_llm(
        model=_model(),
        messages=[{"role": "user", "content": "stress tool outputs"}],
        agent_name="Budget agent",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_tool_call=on_tool_call,
        prepared_turn_context=("STATIC", "DYNAMIC"),
        prepared_tools=[
            {
                "type": "function",
                "function": {"name": "grep", "description": "search"},
            }
        ],
    )

    assert result == "stable-final"
    assert client.closed is True
    assert len(client.dispatches) == 3

    # The exact bytes and message instances dispatched in round 2 remain the
    # immutable prefix of round 3; round 2 only appends its new assistant/tools.
    round_two_prefix_len = len(client.dispatches[1])
    assert client.dispatches[2][:round_two_prefix_len] == client.dispatches[1]
    assert client.object_ids[2][:round_two_prefix_len] == client.object_ids[1]

    provider_results = {
        **_tool_results(client.dispatches[1], "r1-"),
        **_tool_results(client.dispatches[2], "r2-"),
    }
    assert len(provider_results) == 6
    assert sum(len(value) for value in _tool_results(client.dispatches[1], "r1-").values()) <= 64_000
    assert sum(len(value) for value in _tool_results(client.dispatches[2], "r2-").values()) <= 64_000

    done_rows = await _durable_done_rows(agent_id, session_id)
    db_results = {
        str(payload["call_id"]): str(payload["result"])
        for payload in (json.loads(row.content) for row in done_rows)
    }
    callback_results = {
        str(event["call_id"]): str(event["result"])
        for event in callback_events
        if event.get("status") == "done"
    }

    # Simulate restart recovery with a new engine connection/session and the
    # same durable row expander used by Web and IM history.
    await engine.dispose()
    recovered_rows = await _durable_done_rows(agent_id, session_id)
    recovered_results: dict[str, str] = {}
    for row in recovered_rows:
        assistant_message, tool_message = expand_tool_call_row(row)
        recovered_results[str(assistant_message["tool_calls"][0]["id"])] = str(tool_message["content"])

    assert provider_results == db_results == recovered_results == callback_results
    assert len(done_rows) == len(recovered_rows) == 6

    if tool_output_chars > 32_000:
        stored = tmp_path / str(agent_id) / ".tool_results" / session_id
        raw_files = list(stored.glob("grep_*.txt"))
        assert len(raw_files) == 6
        assert all(len(path.read_text()) == tool_output_chars for path in raw_files)
    if tool_output_chars == 30_000:
        assert sum(len(value) < 30_000 for value in provider_results.values()) == 2
        assert sum(len(value) == 30_000 for value in provider_results.values()) == 4
    else:
        assert sum(len(value) < 32_000 for value in provider_results.values()) == 4
        assert sum(len(value) == 32_000 for value in provider_results.values()) == 2
    get_settings.cache_clear()


async def test_rewrite_db_failure_stops_before_next_provider_dispatch(
    monkeypatch,
    tmp_path,
    durable_identity,
):
    agent_id, user_id = durable_identity
    session_id = f"budget-fail-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    client = _BudgetClient(tool_rounds=1)
    _patch_turn_dependencies(monkeypatch, client)
    monkeypatch.setattr(
        chat_history,
        "rewrite_tool_call_done_results",
        AsyncMock(side_effect=RuntimeError("injected rewrite failure")),
    )
    callback_events: list[dict] = []

    async def on_tool_call(event: dict):
        callback_events.append(dict(event))

    result = await caller.call_llm(
        model=_model(),
        messages=[{"role": "user", "content": "force rewrite failure"}],
        agent_name="Budget agent",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_tool_call=on_tool_call,
        prepared_turn_context=("STATIC", "DYNAMIC"),
        prepared_tools=[
            {
                "type": "function",
                "function": {"name": "grep", "description": "search"},
            }
        ],
    )

    assert "injected rewrite failure" in result
    assert len(client.dispatches) == 1
    done_rows = await _durable_done_rows(agent_id, session_id)
    stored_results = {
        str(payload["call_id"]): str(payload["result"])
        for payload in (json.loads(row.content) for row in done_rows)
    }
    callback_results = {
        str(event["call_id"]): str(event["result"])
        for event in callback_events
        if event.get("status") == "done"
    }
    assert len(stored_results) == len(callback_results) == 3
    assert all(len(value) == 30_000 for value in stored_results.values())
    assert callback_results == stored_results
    get_settings.cache_clear()


async def test_complete_multi_tool_plan_is_durable_before_first_execution(
    monkeypatch,
    tmp_path,
    durable_identity,
):
    agent_id, user_id = durable_identity
    conversation_id = f"planned-round-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    client = _BudgetClient(
        tool_rounds=1,
        tool_content="I will run all three checks.",
    )
    _patch_turn_dependencies(monkeypatch, client, tool_output_chars=100)
    executions = 0

    async def crash_during_second_tool(*_args, **_kwargs):
        nonlocal executions
        executions += 1
        if executions == 2:
            raise RuntimeError("simulated process crash window")
        return "first-result"

    monkeypatch.setattr(caller, "execute_tool", crash_during_second_tool)
    async with async_session() as db:
        anchor = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="user",
            content="run three checks",
            conversation_id=conversation_id,
            message_meta={"turn_status": "running"},
        )
        db.add(anchor)
        await db.commit()
        await db.refresh(anchor)

    result = await caller.call_llm(
        model=_model(),
        messages=[{"role": "user", "content": "run three checks"}],
        agent_name="Budget agent",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=conversation_id,
        turn_anchor_id=anchor.id,
        prepared_turn_context=("STATIC", "DYNAMIC"),
        prepared_tools=[
            {"type": "function", "function": {"name": "grep", "description": "search"}}
        ],
    )

    assert "simulated process crash window" in result
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.conversation_id == conversation_id,
                        ChatMessage.role == "tool_call",
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        payloads = [json.loads(row.content) for row in rows]
        running = [payload for payload in payloads if payload["status"] == "running"]
        running.sort(key=lambda payload: payload["round_tool_index"])
        assert [payload["call_id"] for payload in running] == ["r1-1", "r1-2", "r1-3"]
        assert running[0]["assistant_content"] == "I will run all three checks."
        assert all(payload.get("assistant_content") is None for payload in running[1:])

        recovered = await turn_recovery.prepare_recoverable_turn_history(
            db,
            anchor,
            execution_agent_id=agent_id,
            ctx_size=100,
        )

    assert executions == 2
    assert sum(
        message.get("content") == "I will run all three checks."
        for message in recovered
    ) == 1
    recovered_assistant_rounds = [
        message for message in recovered if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert len(recovered_assistant_rounds) == 1
    assert [
        tool_call["id"] for tool_call in recovered_assistant_rounds[0]["tool_calls"]
    ] == ["r1-1", "r1-2", "r1-3"]
    recovered_tools = [message for message in recovered if message.get("role") == "tool"]
    assert len(recovered_tools) == 3
    assert [message["tool_call_id"] for message in recovered_tools] == [
        "r1-1",
        "r1-2",
        "r1-3",
    ]
    assert sum(
        "Recovery blocked automatic replay" in str(message.get("content"))
        for message in recovered_tools
    ) == 2


async def test_restart_normalizes_done_rows_from_crash_before_live_round_rewrite(
    monkeypatch,
    tmp_path,
    durable_identity,
):
    agent_id, user_id = durable_identity
    conversation_id = f"budget-crash-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAWITH_MSG_TOOL_BUDGET", "64000")
    from app.config import get_settings

    get_settings.cache_clear()
    async with async_session() as db:
        anchor = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="user",
            content="crash-window",
            conversation_id=conversation_id,
            message_meta={"turn_status": "running"},
        )
        db.add(anchor)
        await db.flush()
        round_id = f"{anchor.id}:execution:round:1"
        for index in range(1, 4):
            await chat_history.persist_tool_call_row(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                evt={
                    "name": "grep",
                    "call_id": f"crash-{index}",
                    "args": {"pattern": str(index)},
                    "status": "done",
                    "result": str(index) * 30_000,
                    "round_id": round_id,
                },
                turn_anchor_id=anchor.id,
                turn_fence_locked=True,
            )
        await db.commit()

    # This is the exact startup window: every done row is durable, but the live
    # process died before enforce_message_budget + transactional reconciliation.
    await turn_recovery._normalize_completed_tool_rounds_for_recovery(
        anchor,
        execution_agent_id=agent_id,
    )

    recovered = await _durable_done_rows(agent_id, conversation_id)
    recovered_values = [str(json.loads(row.content)["result"]) for row in recovered]
    assert len(recovered_values) == 3
    assert sum(map(len, recovered_values)) <= 64_000
    assert any("<persisted-output>" in value for value in recovered_values)
    stored = tmp_path / str(agent_id) / ".tool_results" / conversation_id
    archived_values = [path.read_text() for path in stored.glob("grep_*.txt")]
    assert archived_values
    assert all(len(value) == 30_000 and len(set(value)) == 1 for value in archived_values)
    get_settings.cache_clear()
