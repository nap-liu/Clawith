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
        self.added = []
        self.committed = False
        self.refreshed = []
        self.flush_count = 0

    async def execute(self, _statement, _params=None):
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1

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
async def test_custom_manager_can_resolve_trigger_session_only(monkeypatch):
    viewer_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=viewer_id, role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    trigger_session = _session_summary(agent_id=agent_id, owner_id=owner_id, now=now)
    trigger_session.source_channel = "trigger"
    trigger_db = RecordingDB(
        responses=[
            DummyResult([trigger_session]),
            DummyResult(scalar_value=2),
            DummyResult(scalar_value="Owner"),
        ]
    )
    detail = await chat_sessions_api.get_session(
        agent_id=agent_id,
        session_id=trigger_session.id,
        current_user=current_user,
        db=trigger_db,
    )
    assert detail.view_scope == "all"

    web_session = _session_summary(agent_id=agent_id, owner_id=owner_id, now=now)
    web_db = RecordingDB(responses=[DummyResult([web_session])])
    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.get_session(
            agent_id=agent_id,
            session_id=web_session.id,
            current_user=current_user,
            db=web_db,
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
    message_id = uuid.uuid4()
    attachment = {
        "display_name": "invoice.png",
        "path": "workspace/uploads/invoice.png",
        "kind": "image",
        "mime_type": "image/png",
    }
    message = SimpleNamespace(
        id=message_id,
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
        message_meta={"attachments": [attachment, attachment, attachment]},
        thinking=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
            DummyResult([(owner_id, "Owner", None)]),  # canonical sender profile
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
            "id": str(message_id),
            "role": "user",
            "content": "hello",
            "display_content": "hello",
            "attachments": [attachment, attachment, attachment],
            "created_at": now.isoformat(),
            "sender_user_id": str(owner_id),
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
    message_id = uuid.uuid4()
    message = SimpleNamespace(
        id=message_id,
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
        message_meta=None,
        thinking=None,
    )
    db = RecordingDB(
        responses=[
            DummyResult([session]),
            DummyResult([message]),
            DummyResult([(other_user_id, "Other user", None)]),
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
            "id": str(message_id),
            "role": "user",
            "content": "hello",
            "display_content": "hello",
            "attachments": [],
            "created_at": now.isoformat(),
            "sender_user_id": str(other_user_id),
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

    first_anchor = uuid.uuid4()
    second_anchor = uuid.uuid4()

    def message(role, content, offset_seconds, turn_anchor_id=None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            role=role,
            content=content,
            thinking=None,
            created_at=started_at + timedelta(seconds=offset_seconds),
            participant_id=None,
            user_id=user_id,
            message_meta=(
                {"turn_anchor_id": str(turn_anchor_id)}
                if turn_anchor_id is not None
                else {}
            ),
        )

    rows = [
        message("user", "移除联系人", 0),
        message("tool_call", json.dumps({
            "name": "search_contacts",
            "call_id": search_call_id,
            "args": {"query": "胡云"},
            "status": "running",
            "result": "",
        }), 1, first_anchor),
        message("tool_call", json.dumps({
            "name": "search_contacts",
            "call_id": search_call_id,
            "args": {"query": "胡云"},
            "status": "done",
            "result": "found",
        }), 2, first_anchor),
        message("tool_call", json.dumps({
            "name": "remove_contact",
            "call_id": remove_call_id,
            "args": {"target_id": "human-1"},
            "status": "running",
            "result": "",
        }), 3, first_anchor),
        message("tool_call", json.dumps({
            "name": "remove_contact",
            "call_id": remove_call_id,
            "args": {"target_id": "human-1"},
            "status": "done",
            "result": "removed",
        }), 4, first_anchor),
        message("assistant", "已完成", 5),
        message("user", "再查一次", 6),
        message("tool_call", json.dumps({
            "name": "search_contacts",
            "call_id": search_call_id,
            "args": {"query": "胡云"},
            "status": "running",
            "result": "",
        }), 7, second_anchor),
    ]
    # The SQL query returns newest-first; the handler reverses it before rendering.
    db = RecordingDB(responses=[
        DummyResult([session]),
        DummyResult(reversed(rows)),
        DummyResult([(user_id, "User", None)]),
        DummyResult([(agent_id, "Assistant", None)]),
    ])

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
    assert len(tool_messages) == 3
    assert [(item["toolName"], item["toolStatus"]) for item in tool_messages] == [
        ("search_contacts", "done"),
        ("remove_contact", "done"),
        ("search_contacts", "running"),
    ]
    assert [item["toolCallId"] for item in tool_messages] == [
        search_call_id,
        remove_call_id,
        search_call_id,
    ]
    assert [item["turnAnchorId"] for item in tool_messages] == [
        str(first_anchor),
        str(first_anchor),
        str(second_anchor),
    ]
    assert tool_messages[0]["toolResult"] == "found"
    assert all(item["content"] == "" for item in tool_messages)
    assert all(item["display_content"] == "" for item in tool_messages)
    assert tool_messages[0]["created_at"] == (started_at + timedelta(seconds=1)).isoformat()
    assert messages[-1]["role"] == "tool_call"


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
    db = RecordingDB(responses=[
        DummyResult([session]),
        DummyResult([pending_confirmation]),
        DummyResult([(agent_id, "Assistant", None)]),
    ])

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
    db = RecordingDB(responses=[DummyResult()])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    async def fake_promote_platform_session(_db, session):
        session.is_primary = True
        return session

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api, "promote_platform_session", fake_promote_platform_session)

    session = await chat_sessions_api.create_session(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert session.agent_id == str(agent_id)
    assert session.user_id == str(user_id)
    assert session.source_channel == "web"
    assert session.is_primary is True
    assert session.participant_type == "user"
    assert session.is_group is False
    assert db.committed is True
    assert db.flush_count == 1
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_get_session_execution_returns_latest_provenance(monkeypatch):
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="member")
    now = datetime.now(UTC)
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        source="on_message",
        status="failed",
        scheduled_at=now - timedelta(seconds=5),
        finished_at=now,
        last_error="origin no longer exists",
    )
    db = RecordingDB(responses=[DummyResult([execution])])

    async def fake_load(_db, _user, _agent_id, _session_id):
        return SimpleNamespace(id=agent_id), SimpleNamespace(id=session_id), "all"

    monkeypatch.setattr(chat_sessions_api, "_load_accessible_session", fake_load)

    result = await chat_sessions_api.get_session_execution(
        agent_id=agent_id,
        session_id=session_id,
        current_user=user,
        db=db,
    )

    assert result["source"] == "on_message"
    assert result["status"] == "failed"
    assert result["last_error"] == "Execution failed. Open execution details for diagnostics."
    assert result["finished_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_get_session_execution_returns_none_without_link(monkeypatch):
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="member")
    db = RecordingDB(responses=[DummyResult()])

    async def fake_load(_db, _user, _agent_id, _session_id):
        return SimpleNamespace(id=agent_id), SimpleNamespace(id=session_id), "mine"

    monkeypatch.setattr(chat_sessions_api, "_load_accessible_session", fake_load)

    result = await chat_sessions_api.get_session_execution(
        agent_id=agent_id,
        session_id=session_id,
        current_user=user,
        db=db,
    )

    assert result is None
