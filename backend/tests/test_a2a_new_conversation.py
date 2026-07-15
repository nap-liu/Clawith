"""Tests for the new_conversation parameter in _send_message_to_agent.

Validates three behaviours:
(a) new_conversation=True with no prior session → ChatSession created with
    non-null external_conv_id (a2a-<hex> format).
(b) new_conversation=True even when a prior session exists → force-creates
    a new ChatSession (find query is skipped).
(c) new_conversation omitted (default) → most-recent existing session reused,
    no new ChatSession added.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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


# ── Re-use the same helpers from test_a2a_msg_type ────────────────────

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
        if self._scalars_list is not None:
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
            raise AssertionError(
                f"unexpected execute() call — no more responses queued. "
                f"Already consumed all responses; added so far: {self.added}"
            )
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        self.flushed = True


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


def _make_participant(ref_id=None):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.type = "agent"
    p.ref_id = ref_id or uuid.uuid4()
    return p


def _make_tenant(a2a_async_enabled=True):
    t = MagicMock()
    t.a2a_async_enabled = a2a_async_enabled
    return t


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_conversation_creates_session_with_external_conv_id():
    """(a) new_conversation=True, no prior session → ChatSession with non-null
    external_conv_id in a2a-<hex> format."""
    from app.services.agent_tools import _send_message_to_agent
    from app.models.chat_session import ChatSession

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")

    # Query sequence when new_conversation=True and async A2A is enabled (notify path):
    # 1. source agent lookup
    # 2. target agent exact-match
    # 3. relationship check
    # 4. src_participant
    # 5. tgt_participant
    # (NO session lookup — skipped because new_conversation=True)
    # 6. count query for #N suffix
    # 7. tenant feature-flag
    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),          # 1. source agent
        DummyResult(scalars_list=[target_agent]),         # 2. target exact match
        DummyResult(scalar_value=rel_id),                 # 3. rel check
        DummyResult(scalar_value=src_participant),        # 4. src_participant
        DummyResult(scalar_value=tgt_participant),        # 5. tgt_participant
        DummyResult(scalar_value=0),                      # 6. count for #N suffix
        DummyResult(scalar_value=_make_tenant()),         # 7. tenant flag
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Starting fresh",
            "msg_type": "notify",
            "new_conversation": True,
        })

    # A ChatSession should have been added to the DB
    chat_sessions = [obj for obj in db.added if isinstance(obj, ChatSession)]
    assert len(chat_sessions) == 1, f"Expected 1 ChatSession added, got {len(chat_sessions)}: {db.added}"

    new_sess = chat_sessions[0]
    assert new_sess.external_conv_id is not None, "external_conv_id must be set"
    assert new_sess.external_conv_id.startswith("a2a-"), (
        f"expected 'a2a-' prefix, got {new_sess.external_conv_id!r}"
    )
    assert len(new_sess.external_conv_id) == len("a2a-") + 8, (
        f"expected 'a2a-<8hex>', got {new_sess.external_conv_id!r}"
    )
    assert "#1" in new_sess.title, f"expected '#1' suffix in title, got {new_sess.title!r}"
    assert "Notification sent" in result


@pytest.mark.asyncio
async def test_new_conversation_force_creates_even_when_prior_session_exists():
    """(b) new_conversation=True skips the find query even when a prior session
    exists — a new ChatSession is created, distinguished by external_conv_id."""
    from app.services.agent_tools import _send_message_to_agent
    from app.models.chat_session import ChatSession

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")

    # The existing session that WOULD be reused if new_conversation were False
    existing_session = MagicMock()
    existing_session.id = uuid.uuid4()
    existing_session.last_message_at = None

    # With new_conversation=True the find-existing query is skipped entirely.
    # If the find query were accidentally issued, RecordingDB would raise on the
    # next (misaligned) response — that's how we detect the skip.
    # Sequence: source / target / rel / src_part / tgt_part / count / tenant
    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalar_value=1),                      # count → existing pair had 1 session
        DummyResult(scalar_value=_make_tenant()),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Reset please",
            "msg_type": "notify",
            "new_conversation": True,
        })

    chat_sessions = [obj for obj in db.added if isinstance(obj, ChatSession)]
    assert len(chat_sessions) == 1

    new_sess = chat_sessions[0]
    # Count was 1, so suffix should be #2
    assert "#2" in new_sess.title, f"expected '#2' suffix, got {new_sess.title!r}"
    assert new_sess.external_conv_id is not None
    assert new_sess.external_conv_id.startswith("a2a-")
    assert "Notification sent" in result


@pytest.mark.asyncio
async def test_default_reuses_existing_session_no_new_session_added():
    """(c) Without new_conversation, the most-recent existing session is reused
    and no new ChatSession is added to the DB."""
    from app.services.agent_tools import _send_message_to_agent
    from app.models.chat_session import ChatSession

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob")

    existing_session = MagicMock()
    existing_session.id = session_id
    existing_session.last_message_at = None

    # Sequence: source / target / rel / src_part / tgt_part / session-find / tenant
    db = RecordingDB(responses=[
        DummyResult(scalar_value=source_agent),
        DummyResult(scalars_list=[target_agent]),
        DummyResult(scalar_value=rel_id),
        DummyResult(scalar_value=src_participant),
        DummyResult(scalar_value=tgt_participant),
        DummyResult(scalars_list=[existing_session]),     # session lookup returns existing one
        DummyResult(scalar_value=_make_tenant()),
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _send_message_to_agent(from_agent_id, {
            "agent_id": str(target_id),
            "message": "Hello again",
            "msg_type": "notify",
            # new_conversation omitted → default False
        })

    # No new ChatSession should have been created
    chat_sessions = [obj for obj in db.added if isinstance(obj, ChatSession)]
    assert len(chat_sessions) == 0, (
        f"Expected 0 new ChatSessions (reuse path), got {len(chat_sessions)}: {db.added}"
    )
    assert "Notification sent" in result
