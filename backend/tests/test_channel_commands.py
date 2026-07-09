"""Unit tests for app.services.channel_commands.

Covers:
1. `handle_channel_command()` scopes its archive lookup by source_channel
   (no cross-channel collision on shared external_conv_id).
2. It archives the matching old session by renaming its external_conv_id.
3. It defers new-session creation to the next user message so the session
   title auto-names from the first message — rather than being locked to
   a hard-coded 'New Session' placeholder.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import channel_commands


class _ExecutedQuery:
    """Captures WHERE-clause state for an executed SQLAlchemy select()."""

    def __init__(self, statement: Any) -> None:
        self.statement = statement
        # Extract column names referenced by equality comparisons in the WHERE
        # clause. This lets tests assert that source_channel is part of the
        # filter without depending on clause order.
        self.filter_columns: set[str] = set()
        self.filter_values: dict[str, Any] = {}
        whereclause = getattr(statement, "whereclause", None)
        self._collect(whereclause)

    def _collect(self, clause: Any) -> None:
        if clause is None:
            return
        # BooleanClauseList (AND/OR) has .clauses
        sub_clauses = getattr(clause, "clauses", None)
        if sub_clauses:
            for c in sub_clauses:
                self._collect(c)
            return
        left = getattr(clause, "left", None)
        right = getattr(clause, "right", None)
        if left is not None:
            name = getattr(left, "key", None) or getattr(left, "name", None)
            if name:
                self.filter_columns.add(name)
                if right is not None:
                    # BindParameter exposes .value
                    val = getattr(right, "value", None)
                    if val is not None:
                        self.filter_values[name] = val


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeDB:
    """Minimal AsyncSession stub that records executes / adds / flush / commit."""

    def __init__(self, lookup_result: Any = None) -> None:
        self._lookup_result = lookup_result
        self.executed: list[_ExecutedQuery] = []
        self.added: list[Any] = []
        self.flushes = 0

    async def execute(self, statement, _params=None):  # noqa: D401
        self.executed.append(_ExecutedQuery(statement))
        return _FakeResult(self._lookup_result)

    def add(self, obj) -> None:
        # Assign an id so handle_channel_command can stringify it.
        if getattr(obj, "id", None) is None:
            try:
                obj.id = uuid.uuid4()
            except Exception:
                pass
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


@pytest.mark.asyncio
async def test_handle_channel_command_scopes_lookup_by_source_channel():
    """Regression test for review concern #2.

    The session-archive lookup must include `source_channel` in its WHERE
    clause so a /new command on one channel never archives a same-external-id
    session on another channel.
    """
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    db = FakeDB(lookup_result=None)  # no pre-existing session

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/new",
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="feishu_p2p_ou_xxx",
        source_channel="feishu",
    )

    assert result["action"] == "new_session"
    # Exactly one SELECT for the old-session lookup.
    assert len(db.executed) == 1
    q = db.executed[0]
    # The WHERE clause must filter on all three columns.
    assert "agent_id" in q.filter_columns
    assert "external_conv_id" in q.filter_columns
    assert "source_channel" in q.filter_columns, (
        "handle_channel_command() must scope the archive lookup by source_channel "
        "so it never archives a cross-channel session with a colliding external_conv_id"
    )
    assert q.filter_values.get("source_channel") == "feishu"


@pytest.mark.asyncio
async def test_handle_channel_command_does_not_preempt_session_creation():
    """New sessions must not be pre-created by /new — they're built by the
    next user message via find_or_create_channel_session, so the first real
    message content becomes the session title instead of "New Session".
    """
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # Simulate no pre-existing session (lookup miss).
    db = FakeDB(lookup_result=None)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/new",
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="shared_conv_id_xxx",
        source_channel="feishu",
    )

    assert result["action"] == "new_session"
    # Nothing should be added to the DB — creation is deferred.
    assert db.added == []
    # And the response must not leak a session_id (there is no session yet).
    assert "session_id" not in result


@pytest.mark.asyncio
async def test_handle_channel_command_archives_old_session():
    """When a session for the same (agent_id, external_conv_id, source_channel)
    exists, /reset must archive it by renaming its external_conv_id, so the
    next user message creates a fresh one.
    """
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # Existing session to be archived.
    old_session = SimpleNamespace(external_conv_id="feishu_p2p_ou_zzz")
    db = FakeDB(lookup_result=old_session)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/reset",
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="feishu_p2p_ou_zzz",
        source_channel="feishu",
    )

    assert result["action"] == "new_session"
    # Old session got its external_conv_id renamed to the archived form.
    assert old_session.external_conv_id.startswith("feishu_p2p_ou_zzz__archived_")
    # No new session pre-created (deferred to next user message).
    assert db.added == []


@pytest.mark.asyncio
async def test_is_channel_command_recognises_slash_commands():
    assert channel_commands.is_channel_command("/new") is True
    assert channel_commands.is_channel_command("/reset") is True
    assert channel_commands.is_channel_command("/help") is True
    assert channel_commands.is_channel_command("/stop") is True
    assert channel_commands.is_channel_command("/thinking on") is True
    assert channel_commands.is_channel_command("/thinking off") is True
    assert channel_commands.is_channel_command("/thinking status") is True
    assert channel_commands.is_channel_command("/think on") is True
    assert channel_commands.is_channel_command("  /NEW  ") is True
    assert channel_commands.is_channel_command("/RESET") is True
    # Non-commands
    assert channel_commands.is_channel_command("hello") is False
    assert channel_commands.is_channel_command("/newish") is False
    assert channel_commands.is_channel_command("/thinking maybe") is False
    assert channel_commands.is_channel_command("") is False


@pytest.mark.asyncio
async def test_help_command_lists_available_im_commands():
    agent_id = uuid.uuid4()
    db = FakeDB()

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/help",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_p2p_staff_1",
        source_channel="dingtalk",
    )

    assert result["action"] == "help"
    assert "/new" in result["message"]
    assert "/thinking on" in result["message"]
    assert "/thinking off" in result["message"]
    assert "/thinking status" in result["message"]
    assert "/stop" in result["message"]
    assert "/help" in result["message"]


@pytest.mark.asyncio
async def test_thinking_on_updates_agent_im_config(monkeypatch):
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, im_thinking_output_enabled=False)
    db = FakeDB(lookup_result=agent)

    async def fake_can_manage(_db, _user_id, _agent):
        return True

    monkeypatch.setattr(channel_commands, "user_can_manage_agent_id", fake_can_manage)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/thinking on",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_p2p_staff_1",
        source_channel="dingtalk",
    )

    assert result["action"] == "thinking_output"
    assert agent.im_thinking_output_enabled is True
    assert db.flushes == 1
    assert "数字员工" in result["message"]
    assert "开启" in result["message"]


@pytest.mark.asyncio
async def test_thinking_off_updates_agent_im_config(monkeypatch):
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, im_thinking_output_enabled=True)
    db = FakeDB(lookup_result=agent)

    async def fake_can_manage(_db, _user_id, _agent):
        return True

    monkeypatch.setattr(channel_commands, "user_can_manage_agent_id", fake_can_manage)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/think off",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="feishu_p2p_ou_1",
        source_channel="feishu",
    )

    assert result["action"] == "thinking_output"
    assert agent.im_thinking_output_enabled is False
    assert db.flushes == 1
    assert "数字员工" in result["message"]
    assert "关闭" in result["message"]


@pytest.mark.asyncio
async def test_thinking_status_reports_agent_config():
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, im_thinking_output_enabled=False)
    db = FakeDB(lookup_result=agent)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/thinking status",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="wecom_p2p_1",
        source_channel="wecom",
    )

    assert result["action"] == "thinking_output_status"
    assert "数字员工" in result["message"]
    assert "关闭" in result["message"]


@pytest.mark.asyncio
async def test_thinking_toggle_requires_agent_manage_permission(monkeypatch):
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, im_thinking_output_enabled=False)
    db = FakeDB(lookup_result=agent)

    async def fake_can_manage(_db, _user_id, _agent):
        return False

    monkeypatch.setattr(channel_commands, "user_can_manage_agent_id", fake_can_manage)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/thinking on",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="wecom_p2p_1",
        source_channel="wecom",
    )

    assert result["action"] == "thinking_output_denied"
    assert agent.im_thinking_output_enabled is False
    assert db.flushes == 0
    assert "没有权限" in result["message"]


@pytest.mark.asyncio
async def test_stop_command_cancels_running_turn_without_deleting_history(monkeypatch):
    agent_id = uuid.uuid4()
    calls: list[str] = []

    async def fake_cancel(lock_key: str) -> bool:
        calls.append(lock_key)
        return True

    monkeypatch.setattr(channel_commands, "cancel_running_turn", fake_cancel)
    db = FakeDB()

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/stop",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_p2p_staff_1",
        source_channel="dingtalk",
    )

    assert result["action"] == "stop_turn"
    assert calls == ["dingtalk:dingtalk_p2p_staff_1"]
    assert db.executed == []
    assert "已请求停止" in result["message"]


@pytest.mark.asyncio
async def test_stop_command_reports_when_no_turn_is_running(monkeypatch):
    async def fake_cancel(lock_key: str) -> bool:
        return False

    monkeypatch.setattr(channel_commands, "cancel_running_turn", fake_cancel)
    db = FakeDB()

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/stop",
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        external_conv_id="slack_D123",
        source_channel="slack",
    )

    assert result["action"] == "stop_turn"
    assert "没有正在执行" in result["message"]
