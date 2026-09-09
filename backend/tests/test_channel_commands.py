"""Observable channel command responses, permissions, and session state changes."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import channel_commands


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeDB:
    """Minimal AsyncSession stub that records executes / adds / flush / commit."""

    def __init__(self, lookup_result: Any = None) -> None:
        self._lookup_result = lookup_result
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.flushes = 0

    async def execute(self, statement, _params=None):  # noqa: D401
        self.executed.append(statement)
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
    assert "下一条消息将开启新对话" in result["message"]
    assert "重新发送刚才的需求" in result["message"]
    # Nothing should be added to the DB — creation is deferred.
    assert db.added == []
    # And the response must not leak a session_id (there is no session yet).
    assert "session_id" not in result


@pytest.mark.asyncio
async def test_handle_channel_command_archives_old_session(monkeypatch):
    """When a session for the same (agent_id, external_conv_id, source_channel)
    exists, /reset must archive it by renaming its external_conv_id, so the
    next user message creates a fresh one.
    """
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # Existing session to be archived.
    old_session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        source_channel="feishu",
        external_conv_id="feishu_p2p_ou_zzz",
    )
    db = FakeDB(lookup_result=old_session)
    cancelled_keys: list[str] = []
    cancelled_turns: list[tuple[uuid.UUID, str, str]] = []
    expected_lock_key = channel_commands.chat_session_lock_key(old_session)

    async def fake_cancel(lock_key: str) -> bool:
        cancelled_keys.append(lock_key)
        return True

    async def fake_stop_tree(*, agent_id, session_id, reason, allow_legacy_recovery):
        assert allow_legacy_recovery is True
        cancelled_turns.append((agent_id, str(session_id), reason))
        return SimpleNamespace(stopped=True)

    monkeypatch.setattr(channel_commands, "cancel_running_turn", fake_cancel)
    monkeypatch.setattr(
        "app.services.turn_control.stop_session_turn_tree",
        fake_stop_tree,
    )

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/reset",
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="feishu_p2p_ou_zzz",
        source_channel="feishu",
    )

    assert result["action"] == "new_session"
    assert cancelled_keys == [expected_lock_key]
    assert cancelled_turns == [
        (agent_id, str(old_session.id), f"IM /new by user {user_id}")
    ]
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
    assert channel_commands.is_channel_command("/status") is True
    assert channel_commands.is_channel_command("/thinking on") is True
    assert channel_commands.is_channel_command("/thinking off") is True
    assert channel_commands.is_channel_command("/thinking status") is True
    assert channel_commands.is_channel_command("/think on") is True
    assert channel_commands.is_channel_command("/scene warranty") is True
    assert channel_commands.is_channel_command("/scene status") is True
    assert channel_commands.is_channel_command("/scene off") is True
    assert channel_commands.is_channel_command("/scene") is True
    assert channel_commands.is_channel_command("/scene too many args") is True
    assert channel_commands.is_channel_command("/model") is True
    assert channel_commands.is_channel_command("/model list") is True
    assert channel_commands.is_channel_command("/model qwen3.5-plus") is True
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
    assert "/scene" in result["message"]
    assert "/model" in result["message"]
    assert "/stop" in result["message"]
    assert "/status" in result["message"]
    assert "/help" in result["message"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (950, "950"),
        (1200, "1.2K"),
        (13_579, "13.6K"),
        (100_000, "100K"),
        (1_250_000, "1.2M"),
        (125_000_000, "125M"),
    ],
)
def test_format_token_count_is_human_readable(value, expected):
    assert channel_commands._format_token_count(value) == expected


@pytest.mark.asyncio
async def test_status_reports_current_agent_model_session_and_token_usage(monkeypatch):
    from app.services import chat_model_selection, session_token_usage
    from app.services.token_tracker import TokenUsage

    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        source_channel="dingtalk",
        external_conv_id="dingtalk_group_1",
        im_config={"model_id": str(uuid.uuid4())},
        is_group=True,
        context_terminated_reason=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="小智",
        status="idle",
    )

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_runtime(*_args, **_kwargs):
        return chat_model_selection.RuntimeModelResolution(
            SimpleNamespace(model="qwen3.5-plus"),
            None,
            chat_model_selection.MODEL_OVERRIDE_OK,
        )

    async def fake_count(*_args, **_kwargs):
        return 12

    async def fake_scene(_db, requested_agent_id, requested_session):
        assert requested_agent_id == agent_id and requested_session is session
        return {"scene_key": "warranty", "activation_source": "automatic"}

    async def fake_running(*_args, **_kwargs):
        return True

    async def fake_usage(*_args, **_kwargs):
        return (
            TokenUsage(
                total_tokens=1200,
                input_tokens=1000,
                output_tokens=200,
                cache_read_tokens=700,
                cache_eligible_input_tokens=1000,
            ),
            3,
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(channel_commands, "_count_session_messages", fake_count)
    monkeypatch.setattr(channel_commands, "resolve_session_scene", fake_scene)
    monkeypatch.setattr(channel_commands, "has_running_turn", fake_running)
    monkeypatch.setattr(chat_model_selection, "resolve_runtime_models", fake_runtime)
    monkeypatch.setattr(session_token_usage, "load_session_token_usage", fake_usage)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/status",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_group_1",
        source_channel="dingtalk",
        is_group=True,
    )

    assert result["action"] == "status"
    assert "数字员工：小智" in result["message"]
    assert "运行状态：处理中" in result["message"]
    assert "模型：qwen3.5-plus（会话临时模型）" in result["message"]
    assert "场景：warranty" in result["message"]
    assert "会话：群聊 · 12 条消息" in result["message"]
    assert "通道：dingtalk · 群聊" in result["message"]
    assert "输入 1K / 输出 200 / 总计 1.2K / 缓存命中率 70.0%" in result["message"]
    assert "已记录" not in result["message"]


@pytest.mark.asyncio
async def test_thinking_on_enables_agent_im_process_feedback(monkeypatch):
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
    assert "过程反馈" in result["message"]
    assert "开启" in result["message"]


@pytest.mark.asyncio
async def test_thinking_off_disables_agent_im_process_feedback(monkeypatch):
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
    user_id = uuid.uuid4()
    calls: list[str] = []
    cancelled_turns: list[tuple[uuid.UUID, str, str]] = []
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        source_channel="dingtalk",
        external_conv_id="dingtalk_p2p_staff_1",
    )

    async def fake_cancel(lock_key: str) -> bool:
        calls.append(lock_key)
        return True

    async def fake_stop_tree(*, agent_id, session_id, reason, allow_legacy_recovery):
        assert allow_legacy_recovery is True
        cancelled_turns.append((agent_id, str(session_id), reason))
        return SimpleNamespace(stopped=True)

    monkeypatch.setattr(channel_commands, "cancel_running_turn", fake_cancel)
    monkeypatch.setattr(
        "app.services.turn_control.stop_session_turn_tree",
        fake_stop_tree,
    )
    db = FakeDB(lookup_result=session)

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/stop",
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="dingtalk_p2p_staff_1",
        source_channel="dingtalk",
    )

    assert result["action"] == "stop_turn"
    assert calls == [channel_commands.chat_session_lock_key(session)]
    assert cancelled_turns == [
        (agent_id, str(session.id), f"IM /stop by user {user_id}")
    ]
    assert len(db.executed) == 1
    assert "已请求停止" in result["message"]


@pytest.mark.asyncio
async def test_stop_command_reports_when_no_turn_is_running(monkeypatch):
    calls: list[str] = []

    async def fake_cancel(lock_key: str) -> bool:
        calls.append(lock_key)
        return False

    monkeypatch.setattr(channel_commands, "cancel_running_turn", fake_cancel)
    db = FakeDB()

    agent_id = uuid.uuid4()
    result = await channel_commands.handle_channel_command(
        db=db,
        command="/stop",
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        external_conv_id="slack_D123",
        source_channel="slack",
    )

    assert result["action"] == "stop_turn"
    assert calls == [
        channel_commands.channel_session_lock_key(
            agent_id,
            "slack",
            "slack_D123",
        )
    ]
    assert "没有正在执行" in result["message"]


@pytest.mark.asyncio
async def test_stop_marks_recoverable_turn_without_local_running_task(monkeypatch):
    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        source_channel="dingtalk",
        external_conv_id="dingtalk_group_recovering",
    )
    marked: list[tuple[uuid.UUID, str, str]] = []

    async def fake_cancel(_lock_key: str) -> bool:
        return False

    async def fake_stop_tree(*, agent_id, session_id, reason, allow_legacy_recovery):
        assert allow_legacy_recovery is True
        marked.append((agent_id, str(session_id), reason))
        return SimpleNamespace(stopped=True)

    monkeypatch.setattr(channel_commands, "cancel_running_turn", fake_cancel)
    monkeypatch.setattr(
        "app.services.turn_control.stop_session_turn_tree",
        fake_stop_tree,
    )

    user_id = uuid.uuid4()

    result = await channel_commands.handle_channel_command(
        db=FakeDB(lookup_result=session),
        command="/stop",
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id=session.external_conv_id,
        source_channel="dingtalk",
    )

    assert marked == [
        (agent_id, str(session.id), f"IM /stop by user {user_id}")
    ]
    assert "已请求停止" in result["message"]


@pytest.mark.asyncio
async def test_scene_command_activates_published_scene_on_existing_session(monkeypatch):
    from app.services import scene_service

    session = SimpleNamespace(im_config={})

    async def fake_load(*_args, **_kwargs):
        return session

    async def fake_resolve(*_args, **_kwargs):
        return scene_service.SceneRuntimeResolution(
            scene_service.SCENE_STATUS_OK,
            {
                "scene_key": "warranty",
                "name": "售后咨询",
                "revision": 3,
                "enabled": True,
            },
        )

    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_load)
    monkeypatch.setattr(scene_service, "resolve_scene_for_activation", fake_resolve)
    db = FakeDB()

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/scene warranty",
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_p2p_staff_1",
        source_channel="dingtalk",
    )

    assert result["action"] == "scene_activated"
    assert "售后咨询" in result["message"]
    assert "v3" in result["message"]
    assert "下一条消息起生效" in result["message"]
    assert session.im_config == {"scene_key": "warranty"}
    assert db.flushes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "action", "message"),
    [
        ("capability_disabled", "scene_capability_disabled", "未启用场景能力"),
        ("not_found", "scene_not_found", "未找到场景"),
        ("unpublished", "scene_unpublished", "尚未发布"),
        ("disabled", "scene_disabled", "已停用"),
    ],
)
async def test_scene_command_reports_precise_activation_failure(
    monkeypatch,
    status,
    action,
    message,
):
    from app.services import scene_service

    async def fake_load(*_args, **_kwargs):
        return None

    async def fake_resolve(*_args, **_kwargs):
        return scene_service.SceneRuntimeResolution(status)

    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_load)
    monkeypatch.setattr(scene_service, "resolve_scene_for_activation", fake_resolve)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/scene warranty",
        agent_id=uuid.uuid4(),
        user_id=None,
        external_conv_id="feishu_p2p_ou_1",
        source_channel="feishu",
    )

    assert result["action"] == action
    assert message in result["message"]


@pytest.mark.asyncio
async def test_scene_invalid_syntax_never_falls_through_to_dialogue():
    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/scene too many args",
        agent_id=uuid.uuid4(),
        user_id=None,
        external_conv_id="slack_D1",
        source_channel="slack",
    )

    assert result["action"] == "scene_invalid"
    assert "用法" in result["message"]


@pytest.mark.asyncio
async def test_scene_off_clears_only_scene_session_preference(monkeypatch):
    session = SimpleNamespace(im_config={"scene_key": "warranty", "other": "keep"})

    async def fake_load(*_args, **_kwargs):
        return session

    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_load)
    db = FakeDB()
    result = await channel_commands.handle_channel_command(
        db=db,
        command="/scene off",
        agent_id=uuid.uuid4(),
        user_id=None,
        external_conv_id="wecom_p2p_1",
        source_channel="wecom",
    )

    assert result["action"] == "scene_off"
    assert "恢复默认对话模式" in result["message"]
    assert session.im_config == {"other": "keep", "scene_disabled": True}
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_first_scene_command_creates_control_session(monkeypatch):
    from app.services import channel_session, scene_service

    created_session = SimpleNamespace(im_config={})
    loads = iter([None, created_session])
    created_kwargs = {}

    async def fake_load(*_args, **_kwargs):
        return next(loads)

    async def fake_resolve(*_args, **_kwargs):
        return scene_service.SceneRuntimeResolution(
            scene_service.SCENE_STATUS_OK,
            {
                "scene_key": "default",
                "name": "默认场景",
                "revision": 1,
                "enabled": True,
            },
        )

    async def fake_find_or_create(**kwargs):
        created_kwargs.update(kwargs)
        return created_session

    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_load)
    monkeypatch.setattr(scene_service, "resolve_scene_for_activation", fake_resolve)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", fake_find_or_create)
    db = FakeDB()

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/scene default",
        agent_id=uuid.uuid4(),
        user_id=None,
        external_conv_id="feishu_group_chat_1",
        source_channel="feishu",
        is_group=True,
    )

    assert result["action"] == "scene_activated"
    assert created_kwargs["first_message_title"] == "New Session"
    assert created_kwargs["is_group"] is True
    assert created_kwargs["allow_unresolved_user"] is True
    assert created_session.im_config == {"scene_key": "default"}
