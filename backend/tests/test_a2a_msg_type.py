"""Tests for async A2A msg_type differentiation (notify/consult/task_delegate).

Validates the branching logic in _send_message_to_agent:
- notify:    fire-and-forget, returns immediately
- task_delegate: async with callback, creates focus + trigger
- consult:   synchronous request-response (original behaviour)
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import engine
from a2a_msg_type_support import DummyResult, RecordingDB


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _durable_consult_helpers(monkeypatch):
    async def transition(*_args, **_kwargs):
        return None

    async def persist_reply(db, **kwargs):
        row = MagicMock()
        row.role = "assistant"
        row.thinking = kwargs.get("thinking")
        row.content = kwargs.get("content")
        db.add(row)
        return uuid.uuid4()

    async def publish(*_args, **_kwargs):
        return None

    async def ingest(db, **kwargs):
        row = MagicMock()
        row.id = uuid.uuid4()
        row.role = "user"
        row.message_meta = dict(kwargs.get("message_meta") or {})
        db.add(row)
        result = MagicMock()
        result.message = row
        result.consumed_by_onmessage = False
        return result

    monkeypatch.setattr(
        "app.services.conversation_turn_lifecycle.transition_conversation_turn",
        transition,
    )
    monkeypatch.setattr(
        "app.services.chat_history.persist_assistant_reply_row",
        persist_reply,
    )
    monkeypatch.setattr(
        "app.services.conversation_turn_lifecycle.publish_committed_turn_terminal",
        publish,
    )
    monkeypatch.setattr(
        "app.services.chat_history.ingest_incoming_chat_message",
        ingest,
    )


@pytest.fixture(autouse=True)
def _active_a2a_relationship(monkeypatch):
    async def active(*_args, **_kwargs):
        return {
            "access_allowed": True,
            "access_status": "active",
            "access_status_reason": None,
        }

    monkeypatch.setattr(
        "app.services.recipient_resolver.evaluate_agent_relationship_status",
        active,
    )


# ── Helpers ──────────────────────────────────────────────────────────

def _make_agent(agent_id=None, name="TestAgent", tenant_id=None, agent_type="native",
                expired=False, primary_model_id=None):
    agent = MagicMock()
    agent.id = agent_id or uuid.uuid4()
    agent.name = name
    agent.tenant_id = tenant_id or uuid.uuid4()
    agent.agent_type = agent_type
    agent.is_expired = expired
    agent.expires_at = None
    agent.creator_id = uuid.uuid4()
    agent.primary_model_id = primary_model_id
    agent.fallback_model_id = None
    agent.role_description = ""
    agent.max_tool_rounds = 50
    agent.context_window_size = 20
    return agent


def _make_participant(part_id=None, ref_id=None):
    p = MagicMock()
    p.id = part_id or uuid.uuid4()
    p.type = "agent"
    p.ref_id = ref_id or uuid.uuid4()
    return p


def _make_tenant(a2a_async_enabled=True):
    t = MagicMock()
    t.a2a_async_enabled = a2a_async_enabled
    return t


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_returns_immediately():
    """notify msg_type should return immediately without calling LLM."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalars_list=[session]),
        DummyResult(scalar_value=_make_tenant()),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock) as mock_wake:

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Please review the document",
            "msg_type": "notify",
        })

    assert "Notification sent to Bob" in result
    assert "asynchronously" in result
    mock_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_delegate_creates_focus_and_trigger():
    """task_delegate should arm its durable callback before waking the target."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalars_list=[session]),
        DummyResult(scalar_value=_make_tenant()),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools._arm_a2a_delegate_callback", new_callable=AsyncMock) as mock_arm, \
         patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock) as mock_wake, \
         patch("app.services.activity_logger.log_activity", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Please prepare the Q3 report",
            "msg_type": "task_delegate",
        })

    assert "Task delegated to Bob" in result
    assert "notified when they complete" in result
    mock_arm.assert_awaited_once()
    mock_wake.assert_awaited_once()

    arm_kwargs = mock_arm.call_args.kwargs
    assert arm_kwargs["target"] is target_agent
    assert arm_kwargs["watch_session_id"] == str(session_id)
    assert arm_kwargs["outbound_message_id"] is not None


@pytest.mark.asyncio
async def test_default_msg_type_is_notify():
    """When msg_type is not specified, it should default to notify."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalars_list=[session]),
        DummyResult(scalar_value=_make_tenant()),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock) as mock_wake:

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Heads up about the meeting",
        })

    assert "Notification sent" in result
    mock_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_agent_id_returns_error():
    """Missing canonical agent_id should return an error."""
    from app.services.agent_tools import _send_message_to_agent

    result = await _send_message_to_agent(uuid.uuid4(), {
        "agent_id": "",
        "message": "Hello",
    })

    assert "❌" in result


@pytest.mark.asyncio
async def test_no_relationship_returns_error():
    """No relationship between agents should return an error."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)

    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=None),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx:
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Hello",
            "msg_type": "notify",
        })

    assert '"code": "recipient_not_related"' in result


@pytest.mark.asyncio
async def test_create_on_message_trigger():
    """_create_on_message_trigger should create a trigger in DB."""
    from app.services.agent_tools import _create_on_message_trigger

    agent_id = uuid.uuid4()
    from_agent_id = uuid.uuid4()

    snap_db = RecordingDB(responses=[
        DummyResult(scalar_value=None),
    ])
    trigger_db = RecordingDB(responses=[
        DummyResult(scalar_value=None),
    ])

    enter_count = 0
    dbs = [snap_db, trigger_db]

    async def _enter():
        nonlocal enter_count
        db = dbs[min(enter_count, len(dbs) - 1)]
        enter_count += 1
        return db

    # v1.9.3 introduced DB-backed focus items; ensure_focus_item now opens its own
    # async_session via app.database, bypassing the agent_tools patch. Stub it out
    # so the test stays at the trigger-DB level instead of needing a real agent row.
    async def _stub_ensure_focus_item(agent_id, focus_ref=None, description=None):
        return focus_ref

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools.ensure_focus_item", side_effect=_stub_ensure_focus_item):
        mock_session_ctx.return_value.__aenter__ = AsyncMock(side_effect=_enter)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await _create_on_message_trigger(
            agent_id=agent_id,
            trigger_name="test_trigger",
            from_agent_id_value=str(from_agent_id),
            reason="Test reason",
            focus_ref="test_focus",
        )

    assert trigger_db.committed
    assert len(trigger_db.added) == 1

    trigger = trigger_db.added[0]
    assert trigger.name == "test_trigger"
    assert trigger.type == "on_message"
    assert trigger.config["from_agent_id"] == str(from_agent_id)
    assert trigger.reason == "Test reason"
    assert trigger.focus_ref == "test_focus"


@pytest.mark.asyncio
async def test_create_on_message_trigger_resets_fire_count():
    """_create_on_message_trigger should reset fire_count to 0 for an existing trigger."""
    from app.services.agent_tools import _create_on_message_trigger
    from app.models.trigger import AgentTrigger

    agent_id = uuid.uuid4()
    from_agent_id = uuid.uuid4()

    existing_trigger = AgentTrigger(
        agent_id=agent_id,
        name="test_trigger",
        type="on_message",
        config={"from_agent_id": str(from_agent_id)},
        reason="Old reason",
        focus_ref="old_focus",
        is_enabled=False,
        fire_count=1,
        max_fires=1,
    )

    snap_db = RecordingDB(responses=[
        DummyResult(scalar_value=None),
    ])
    trigger_db = RecordingDB(responses=[
        DummyResult(scalar_value=existing_trigger),
    ])

    enter_count = 0
    dbs = [snap_db, trigger_db]

    async def _enter():
        nonlocal enter_count
        db = dbs[min(enter_count, len(dbs) - 1)]
        enter_count += 1
        return db

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools.ensure_focus_item", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = "new_focus"
        mock_session_ctx.return_value.__aenter__ = AsyncMock(side_effect=_enter)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await _create_on_message_trigger(
            agent_id=agent_id,
            trigger_name="test_trigger",
            from_agent_id_value=str(from_agent_id),
            reason="New reason",
            focus_ref="new_focus",
        )

    assert trigger_db.committed
    assert existing_trigger.is_enabled is True
    assert existing_trigger.fire_count == 0
    assert existing_trigger.reason == "New reason"
    assert existing_trigger.focus_ref == "new_focus"


@pytest.mark.asyncio
async def test_openclaw_target_still_queues():
    """OpenClaw targets should still use the gateway queue regardless of msg_type."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="OpenClawBot", agent_type="openclaw")
    target_agent.openclaw_last_seen = datetime.now(UTC)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalars_list=[session]),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.activity_logger.log_activity", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Hello",
            "msg_type": "notify",
        })

    assert "OpenClaw agent" in result
    assert "queued" in result


@pytest.mark.asyncio
async def test_feature_flag_off_falls_back_to_consult():
    """When tenant a2a_async_enabled=False, notify and task_delegate fall back to consult."""
    from app.services.agent_tools import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    model_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    source_agent.tenant_id = uuid.uuid4()
    target_agent = _make_agent(target_id, name="Bob", primary_model_id=model_id)

    tenant = MagicMock()
    tenant.a2a_async_enabled = False

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    model = MagicMock()
    model.id = model_id
    model.enabled = True
    model.supports_vision = False

    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalars_list=[session]),
        DummyResult(scalar_value=tenant),
        DummyResult(scalar_value=model),
        DummyResult(scalars_list=[]),   # ChatMessage rows (empty history)
        DummyResult(scalar_value=None), # ChatCompaction marker (none)
    ])

    db2 = RecordingDB(responses=[
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalar_value=MagicMock(message_meta={})),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.llm.call_llm_with_failover",
               new_callable=AsyncMock, return_value="Got it") as mock_failover, \
         patch("app.services.activity_logger.log_activity", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(side_effect=[db, db2])
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Hello",
            "msg_type": "notify",
        })

    assert "Bob replied" in result
    assert "Got it" in result
    mock_failover.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_set_trigger_resets_fire_count():
    """_handle_set_trigger should reset fire_count to 0 if it has reached max_fires when re-enabling."""
    from app.services.agent_tools import _handle_set_trigger
    from app.models.trigger import AgentTrigger

    agent_id = uuid.uuid4()
    agent_mock = MagicMock()
    agent_mock.max_triggers = 10

    existing_trigger = AgentTrigger(
        agent_id=agent_id,
        name="test_trigger",
        type="once",
        config={"at": "2026-03-10T09:00:00+08:00"},
        reason="Old reason",
        focus_ref="old_focus",
        is_enabled=False,
        fire_count=1,
        max_fires=1,
    )

    db = RecordingDB(responses=[
        DummyResult(scalar_value=agent_mock),  # Load agent to get per-agent trigger limit
        DummyResult(scalar_value=0),           # Check max triggers (count)
        DummyResult(scalar_value=existing_trigger), # Check for duplicate name
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools.ensure_focus_item", new_callable=AsyncMock) as mock_ensure:
        mock_ensure.return_value = "new_focus"
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        arguments = {
            "name": "test_trigger",
            "type": "once",
            "config": {"at": "2026-03-10T09:00:00+08:00"},
            "reason": "New reason",
            "focus_ref": "new_focus",
        }

        result = await _handle_set_trigger(agent_id, arguments)

    assert "re-enabled" in result
    assert existing_trigger.is_enabled is True
    assert existing_trigger.fire_count == 0
    assert existing_trigger.reason == "New reason"
