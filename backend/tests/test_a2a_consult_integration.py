"""Integration-style tests for the rewritten A2A consult branch.

Verifies that:
1. call_llm_with_failover is invoked (not the old wild loop)
2. The on_tool_call callback passed to call_llm_with_failover calls
   persist_tool_call with RAW args — no sanitize poisoning
3. The final return value contains the reply from call_llm_with_failover

No real DB or LLM connection required — all IO is mocked.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


class DummyResult:
    def __init__(self, values=None, scalar_value=None, scalars_list=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value
        self._scalars_list = scalars_list

    def scalar_one_or_none(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._scalars_list or self._values)

    def first(self):
        if self._scalars_list:
            return self._scalars_list[0] if self._scalars_list else None
        return self._values[0] if self._values else None

    def scalar(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._values[0] if self._values else None


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.committed = False
        self.flushed = False

    async def execute(self, _statement, _params=None):
        if not self.responses:
            raise AssertionError(f"unexpected execute() call — no responses left. Already added: {self.added}")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed = True


def _make_agent(agent_id=None, name="TestAgent", tenant_id=None, primary_model_id=None):
    agent = MagicMock()
    agent.id = agent_id or uuid.uuid4()
    agent.name = name
    agent.tenant_id = tenant_id or uuid.uuid4()
    agent.agent_type = "native"
    agent.is_expired = False
    agent.expires_at = None
    agent.creator_id = uuid.uuid4()
    agent.primary_model_id = primary_model_id
    agent.fallback_model_id = None
    agent.role_description = "A helpful agent"
    agent.max_tool_rounds = 50
    agent.context_window_size = 20
    return agent


def _make_participant(ref_id=None):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.type = "agent"
    p.ref_id = ref_id or uuid.uuid4()
    return p


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consult_routes_through_unified_loop_and_returns_reply():
    """Consult must call call_llm_with_failover and return a string containing the reply."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    model_id = uuid.uuid4()
    session_id = uuid.uuid4()

    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", primary_model_id=model_id)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    model = MagicMock()
    model.id = model_id
    model.enabled = True
    model.supports_vision = False

    tenant = MagicMock()
    tenant.a2a_async_enabled = False  # force consult path

    db_main = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=uuid.uuid4()),   # relationship check
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalar_value=session),
        DummyResult(scalar_value=tenant),
        DummyResult(scalar_value=model),          # primary LLMModel
        DummyResult(scalars_list=[]),              # ChatMessage rows (empty)
        DummyResult(scalar_value=None),            # ChatCompaction marker
    ])
    db_reply = RecordingDB(responses=[
        DummyResult(scalar_value=tgt_participant), # tgt_participant for reply-save
    ])

    captured_on_tool_call = []

    async def fake_failover(**kwargs):
        # Capture the on_tool_call callback to inspect later
        on_tool_call = kwargs.get("on_tool_call")
        if on_tool_call:
            captured_on_tool_call.append(on_tool_call)
        return "Unified loop reply"

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.llm.call_llm_with_failover", side_effect=fake_failover) as mock_failover, \
         patch("app.services.activity_logger.log_activity", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(side_effect=[db_main, db_reply])
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_name": "Bob",
            "message": "What is the status?",
            "msg_type": "consult",
        })

    assert "Bob replied" in result
    assert "Unified loop reply" in result
    mock_failover.assert_called_once()

    # Verify agent_id passed to failover is target.id (A2A id-split: run model = target)
    call_kw = mock_failover.call_args.kwargs
    assert call_kw["agent_id"] == target_agent.id
    assert call_kw["agent_name"] == "Bob"

    # Verify on_tool_call callback was passed
    assert len(captured_on_tool_call) == 1


@pytest.mark.asyncio
async def test_consult_persist_tool_call_stores_raw_connection_string():
    """The on_tool_call→persist_tool_call path must store args RAW (no sanitize).

    Simulates: call_llm_with_failover fires the on_tool_call callback with a
    sql_execute event whose args contain a real connection_string. Asserts that
    persist_tool_call is called with the original (unsanitized) args.
    """
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    model_id = uuid.uuid4()
    session_id = uuid.uuid4()

    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", primary_model_id=model_id)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    model = MagicMock()
    model.id = model_id
    model.enabled = True
    model.supports_vision = False

    tenant = MagicMock()
    tenant.a2a_async_enabled = False

    db_main = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=uuid.uuid4()),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalar_value=session),
        DummyResult(scalar_value=tenant),
        DummyResult(scalar_value=model),
        DummyResult(scalars_list=[]),
        DummyResult(scalar_value=None),
    ])
    db_reply = RecordingDB(responses=[
        DummyResult(scalar_value=tgt_participant),
    ])

    real_conn_str = "mysql://user:secret@prod-host:3306/mydb"
    tool_call_evt = {
        "name": "sql_execute",
        "call_id": "call_abc123",
        "args": {"connection_string": real_conn_str, "sql": "SELECT 1"},
        "status": "done",
        "result": "[{\"rows\": 1}]",
    }

    persist_calls = []

    async def fake_failover(**kwargs):
        # Fire the on_tool_call callback to simulate a tool being called
        on_tool_call = kwargs.get("on_tool_call")
        if on_tool_call:
            await on_tool_call(tool_call_evt)
        return "Query succeeded"

    async def fake_persist(session_factory, *, agent_id, user_id, conversation_id, evt):
        persist_calls.append({
            "agent_id": agent_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "evt": evt,
        })

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.llm.call_llm_with_failover", side_effect=fake_failover), \
         patch("app.services.chat_history.persist_tool_call", side_effect=fake_persist) as mock_persist, \
         patch("app.services.activity_logger.log_activity", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(side_effect=[db_main, db_reply])
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_name": "Bob",
            "message": "Query the DB",
            "msg_type": "consult",
        })

    assert "Bob replied" in result
    assert "Query succeeded" in result

    # persist_tool_call must have been called once (the tool call event)
    assert len(persist_calls) == 1, f"Expected 1 persist call, got {len(persist_calls)}"

    captured = persist_calls[0]
    captured_args = captured["evt"]["args"]

    # RAW: real connection_string must be present, masked form must not
    assert real_conn_str in json.dumps(captured_args), (
        f"Real connection string not found in persisted args: {captured_args}"
    )
    assert "******" not in json.dumps(captured_args), (
        f"Sanitized placeholder found in persisted args — poisoning bug reproduced: {captured_args}"
    )

    # Stored under session_agent_id (the min-id agent), not necessarily target.id
    session_agent_id = min(from_agent_id, target_id, key=str)
    assert captured["agent_id"] == session_agent_id, (
        f"Tool call stored under wrong agent_id. Expected {session_agent_id}, got {captured['agent_id']}"
    )
