"""Model-selection channel command tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import channel_commands
from tests.test_channel_commands import FakeDB


@pytest.mark.asyncio
async def test_model_command_switches_by_saved_model_name(monkeypatch):
    from app.services import chat_model_selection

    model_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = SimpleNamespace(im_config={"scene_key": "warranty"})

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_resolve(*_args, **kwargs):
        assert kwargs["model_name"] == "qwen3.5-plus"
        return chat_model_selection.ModelNameResolution(
            chat_model_selection.MODEL_STATUS_OK,
            SimpleNamespace(id=model_id, model="qwen3.5-plus"),
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_tenant_model_by_name", fake_resolve)
    db = FakeDB()

    result = await channel_commands.handle_channel_command(
        db=db,
        command="/model qwen3.5-plus",
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_group_1",
        source_channel="dingtalk",
        is_group=True,
    )

    assert result["action"] == "model_switched"
    assert "qwen3.5-plus" in result["message"]
    assert str(model_id) not in result["message"]
    assert session.im_config == {
        "scene_key": "warranty",
        "model_id": str(model_id),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved_name", ["list", "status", "default"])
async def test_model_command_selects_reserved_saved_name_with_use(
    monkeypatch,
    reserved_name,
):
    from app.services import chat_model_selection

    model_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = SimpleNamespace(im_config={})

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_resolve(*_args, **kwargs):
        assert kwargs["model_name"] == reserved_name
        return chat_model_selection.ModelNameResolution(
            chat_model_selection.MODEL_STATUS_OK,
            SimpleNamespace(id=model_id, model=reserved_name),
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_tenant_model_by_name", fake_resolve)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command=f"/model use {reserved_name}",
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_group_1",
        source_channel="dingtalk",
        is_group=True,
    )

    assert result["action"] == "model_switched"
    assert session.im_config == {"model_id": str(model_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "model", "expected_action"),
    [
        ("not_found", None, "model_not_found"),
        ("disabled", SimpleNamespace(model="disabled-model"), "model_disabled"),
        ("ambiguous", None, "model_ambiguous"),
    ],
)
async def test_model_command_reports_precise_selection_failure(
    monkeypatch,
    status,
    model,
    expected_action,
):
    from app.services import chat_model_selection

    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return None

    async def fake_resolve(*_args, **_kwargs):
        return chat_model_selection.ModelNameResolution(status, model)

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_tenant_model_by_name", fake_resolve)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/model test-model",
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_p2p_1",
        source_channel="dingtalk",
    )

    assert result["action"] == expected_action
    assert result["message"].startswith("❌")


@pytest.mark.asyncio
async def test_model_status_reports_stale_session_override(monkeypatch):
    from app.services import chat_model_selection

    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = SimpleNamespace(im_config={"model_id": str(uuid.uuid4())})

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_runtime(*_args, **_kwargs):
        return chat_model_selection.RuntimeModelResolution(
            SimpleNamespace(model="qwen3.5-plus"),
            None,
            chat_model_selection.MODEL_OVERRIDE_UNAVAILABLE,
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_runtime_models", fake_runtime)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/model status",
        agent_id=agent.id,
        user_id=uuid.uuid4(),
        external_conv_id="dingtalk_p2p_1",
        source_channel="dingtalk",
    )

    assert result["action"] == "model_status_unavailable"
    assert result["message"].startswith("⚠️")


@pytest.mark.asyncio
async def test_model_list_shows_saved_model_names_without_labels_or_internal_ids(monkeypatch):
    from app.services import chat_model_selection

    model_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_list(*_args, **_kwargs):
        return [
            SimpleNamespace(
                id=model_id,
                model="qwen3.5-plus",
                label="企业 GPT 旗舰版",
            )
        ]

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(chat_model_selection, "list_enabled_tenant_models", fake_list)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/model list",
        agent_id=agent.id,
        user_id=None,
        external_conv_id="feishu_p2p_1",
        source_channel="feishu",
    )

    assert result["action"] == "model_list"
    assert "qwen3.5-plus" in result["message"]
    assert "企业 GPT 旗舰版" not in result["message"]
    assert str(model_id) not in result["message"]


@pytest.mark.asyncio
async def test_model_default_clears_only_model_preference(monkeypatch):
    from app.services import chat_model_selection

    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = SimpleNamespace(im_config={"model_id": str(uuid.uuid4()), "scene_key": "warranty"})

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_runtime(*_args, **_kwargs):
        return chat_model_selection.RuntimeModelResolution(
            SimpleNamespace(model="qwen3.5-plus"),
            None,
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_runtime_models", fake_runtime)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/model default",
        agent_id=agent.id,
        user_id=None,
        external_conv_id="wecom_p2p_1",
        source_channel="wecom",
    )

    assert result["action"] == "model_default"
    assert "qwen3.5-plus" in result["message"]
    assert session.im_config == {"scene_key": "warranty"}


@pytest.mark.asyncio
async def test_reasoning_command_sets_and_resets_only_session_reasoning(monkeypatch):
    from app.services import chat_model_selection

    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = SimpleNamespace(im_config={"model_id": str(uuid.uuid4()), "scene_key": "warranty"})

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_runtime(*_args, **_kwargs):
        return chat_model_selection.RuntimeModelResolution(
            SimpleNamespace(
                provider="qwen",
                model="qwen3.8-plus",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                reasoning_effort=None,
            ),
            None,
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_runtime_models", fake_runtime)

    updated = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/reasoning high",
        agent_id=agent.id,
        user_id=None,
        external_conv_id="feishu_p2p_1",
        source_channel="feishu",
    )
    assert updated["action"] == "reasoning_updated"
    assert "深入 (high)" in updated["message"]
    assert session.im_config["reasoning_effort"] == "high"

    reset = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/reasoning auto",
        agent_id=agent.id,
        user_id=None,
        external_conv_id="feishu_p2p_1",
        source_channel="feishu",
    )
    assert reset["action"] == "reasoning_default"
    assert "自动" in reset["message"]
    assert session.im_config == {"model_id": session.im_config["model_id"], "scene_key": "warranty"}


@pytest.mark.asyncio
async def test_reasoning_command_rejects_off_for_always_on_model(monkeypatch):
    from app.services import chat_model_selection

    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = SimpleNamespace(im_config={})

    async def fake_agent(*_args, **_kwargs):
        return agent

    async def fake_session(*_args, **_kwargs):
        return session

    async def fake_runtime(*_args, **_kwargs):
        return chat_model_selection.RuntimeModelResolution(
            SimpleNamespace(
                provider="moonshot",
                model="kimi-k3",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            None,
        )

    monkeypatch.setattr(channel_commands, "_load_agent", fake_agent)
    monkeypatch.setattr(channel_commands, "_load_channel_session", fake_session)
    monkeypatch.setattr(chat_model_selection, "resolve_runtime_models", fake_runtime)

    result = await channel_commands.handle_channel_command(
        db=FakeDB(),
        command="/reasoning off",
        agent_id=agent.id,
        user_id=None,
        external_conv_id="dingtalk_p2p_1",
        source_channel="dingtalk",
    )
    assert result["action"] == "reasoning_unsupported"
    assert session.im_config == {}
