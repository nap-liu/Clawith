import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import chat_sessions as chat_sessions_api


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._values[0] if self._values else None


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.statements = []
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, _statement, _params=None):
        self.statements.append(_statement)
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)


def _session_summary(*, agent_id, owner_id, now, session_id=None):
    return SimpleNamespace(
        id=session_id or uuid.uuid4(),
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="URL-restored session",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
        is_primary=False,
    )


@pytest.mark.asyncio
async def test_owner_can_get_session_detail_for_url_restore(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=user_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    session = _session_summary(agent_id=agent_id, owner_id=user_id, now=now)
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult(scalar_value=7),
            DummyResult(scalar_value="Alice"),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    detail = await chat_sessions_api.get_session(
        agent_id=agent_id,
        session_id=session.id,
        current_user=current_user,
        db=db,
    )

    assert detail.id == str(session.id)
    assert detail.message_count == 7
    assert detail.username == "Alice"
    assert detail.view_scope == "mine"


@pytest.mark.asyncio
async def test_admin_gets_other_users_session_in_all_scope(monkeypatch):
    admin_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=admin_id, role="org_admin")
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = _session_summary(agent_id=agent_id, owner_id=owner_id, now=now)
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult(scalar_value=3),
            DummyResult(scalar_value="Bob"),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    detail = await chat_sessions_api.get_session(
        agent_id=agent_id,
        session_id=session.id,
        current_user=current_user,
        db=db,
    )

    assert detail.id == str(session.id)
    assert detail.view_scope == "all"


@pytest.mark.asyncio
async def test_member_cannot_resolve_other_users_session(monkeypatch):
    viewer_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=viewer_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = _session_summary(agent_id=agent_id, owner_id=owner_id, now=now)
    db = RecordingDB(responses=[DummyResult([session])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.get_session(
            agent_id=agent_id,
            session_id=session.id,
            current_user=current_user,
            db=db,
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_group_member_resolves_link_in_mine_scope(monkeypatch):
    member_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=member_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = _session_summary(agent_id=agent_id, owner_id=owner_id, now=now)
    session.is_group = True
    session.group_name = "Project room"
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([SimpleNamespace(id=uuid.uuid4())]),
            DummyResult(scalar_value=5),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    detail = await chat_sessions_api.get_session(
        agent_id=agent_id,
        session_id=session.id,
        current_user=current_user,
        db=db,
    )

    assert detail.view_scope == "mine"
    assert detail.participant_type == "group"
    assert detail.username == "Project room"


@pytest.mark.asyncio
async def test_org_admin_can_list_all_sessions(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=viewer_id, role="org_admin")
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
        is_primary=False,
    )
    db = RecordingDB(
        responses=[
            DummyResult([agent]),
            DummyResult([session]),
            DummyResult([(str(session.id), 3)]),  # message_counts
            DummyResult([]),  # unread_counts (empty)
            DummyResult([(owner_id, "Alice")]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
        db=db,
    )

    assert len(sessions) == 1
    assert sessions[0].id == str(session.id)
    assert sessions[0].user_id == str(owner_id)
    assert sessions[0].username == "Alice"


@pytest.mark.asyncio
async def test_creator_can_list_all_sessions(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=creator_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=other_user_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
        is_primary=False,
    )
    db = RecordingDB(
        responses=[
            DummyResult([agent]),
            DummyResult([session]),
            DummyResult([(str(session.id), 2)]),  # message_counts
            DummyResult([]),  # unread_counts
            DummyResult([(other_user_id, "Bob")]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
        db=db,
    )

    assert len(sessions) == 1
    assert sessions[0].user_id == str(other_user_id)
    assert sessions[0].username == "Bob"


@pytest.mark.asyncio
async def test_mine_session_list_applies_channel_filter_and_pagination(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=user_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        source_channel="wechat_miniprogram",
        title="H5 history",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
        is_primary=False,
    )
    db = RecordingDB(
        responses=[
            DummyResult([agent]),
            DummyResult([session]),
            DummyResult([(str(session.id), 1)]),
            DummyResult([]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        source_channel="wechat_miniprogram",
        limit=10,
        offset=5,
        current_user=current_user,
        db=db,
    )

    rendered = str(db.statements[1])
    assert "chat_sessions.source_channel =" in rendered
    assert "LIMIT" in rendered.upper()
    assert "OFFSET" in rendered.upper()
    assert len(sessions) == 1
    assert sessions[0].source_channel == "wechat_miniprogram"


@pytest.mark.asyncio
async def test_org_admin_can_view_other_users_session_messages(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=viewer_id, role="org_admin")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    message = SimpleNamespace(
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        # Calling the handler directly bypasses FastAPI's dependency resolution,
        # so the `limit`/`before` Query(...) defaults are NOT coerced to plain
        # values. Pass the resolved defaults explicitly to mirror a real request.
        limit=20,
        before=None,
        current_user=current_user,
        db=db,
    )

    assert messages == [
        {
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
        }
    ]


@pytest.mark.asyncio
async def test_creator_can_view_other_users_session_messages(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=creator_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=other_user_id,
        source_channel="web",
    )
    message = SimpleNamespace(
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
        ]
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        # See note above: direct handler calls must supply the Query defaults.
        limit=20,
        before=None,
        current_user=current_user,
        db=db,
    )

    assert messages == [
        {
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
        }
    ]


@pytest.mark.asyncio
async def test_session_messages_fold_append_only_tool_events(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    started_at = datetime.now(UTC)
    search_call_id = "call_search"
    remove_call_id = "call_remove"

    current_user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=user_id,
        source_channel="wechat_miniprogram",
        is_group=False,
    )

    def message(role, content, offset_seconds):
        return SimpleNamespace(
            id=uuid.uuid4(),
            role=role,
            content=content,
            thinking=None,
            created_at=started_at + timedelta(seconds=offset_seconds),
            participant_id=None,
            user_id=user_id,
        )

    rows = [
        message("user", "移除联系人", 0),
        message("tool_call", json.dumps({
            "name": "search_contacts",
            "call_id": search_call_id,
            "args": {"query": "胡云"},
            "status": "running",
            "result": "",
        }), 1),
        message("tool_call", json.dumps({
            "name": "search_contacts",
            "call_id": search_call_id,
            "args": {"query": "胡云"},
            "status": "done",
            "result": "found",
        }), 2),
        message("tool_call", json.dumps({
            "name": "remove_contact",
            "call_id": remove_call_id,
            "args": {"target_id": "human-1"},
            "status": "running",
            "result": "",
        }), 3),
        message("tool_call", json.dumps({
            "name": "remove_contact",
            "call_id": remove_call_id,
            "args": {"target_id": "human-1"},
            "status": "done",
            "result": "removed",
        }), 4),
        message("assistant", "已完成", 5),
    ]
    # The SQL query returns newest-first; the handler reverses it before rendering.
    db = RecordingDB(responses=[DummyResult([session]), DummyResult(reversed(rows))])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=uuid.uuid4()), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        limit=200,
        before=None,
        current_user=current_user,
        db=db,
    )

    tool_messages = [item for item in messages if item["role"] == "tool_call"]
    assert len(tool_messages) == 2
    assert [(item["toolName"], item["toolStatus"]) for item in tool_messages] == [
        ("search_contacts", "done"),
        ("remove_contact", "done"),
    ]
    assert [item["toolCallId"] for item in tool_messages] == [search_call_id, remove_call_id]
    assert tool_messages[0]["toolResult"] == "found"
    assert tool_messages[0]["created_at"] == (started_at + timedelta(seconds=1)).isoformat()
    assert messages[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_session_messages_keep_row_id_as_confirmation_handle(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    row_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=user_id, role="member")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=user_id,
        source_channel="wechat_miniprogram",
        is_group=False,
    )
    pending_confirmation = SimpleNamespace(
        id=row_id,
        role="tool_call",
        content=json.dumps({
            "name": "request_confirmation",
            "call_id": "model-confirmation-id",
            "args": {"title": "确认移除"},
            "status": "pending",
            "result": "",
        }),
        created_at=now,
        participant_id=None,
        user_id=user_id,
    )
    db = RecordingDB(responses=[DummyResult([session]), DummyResult([pending_confirmation])])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=uuid.uuid4()), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        limit=200,
        before=None,
        current_user=current_user,
        db=db,
    )

    assert len(messages) == 1
    assert messages[0]["toolName"] == "request_confirmation"
    assert messages[0]["toolCallId"] == str(row_id)
    assert messages[0]["toolStatus"] == "pending"


@pytest.mark.asyncio
async def test_create_session_returns_web_session_shape(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    current_user = SimpleNamespace(id=user_id, role="member")
    db = RecordingDB()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    session = await chat_sessions_api.create_session(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert session.agent_id == str(agent_id)
    assert session.user_id == str(user_id)
    assert session.source_channel == "web"
    assert session.participant_type == "user"
    assert session.is_group is False
    assert db.committed is True
    assert len(db.added) == 1
