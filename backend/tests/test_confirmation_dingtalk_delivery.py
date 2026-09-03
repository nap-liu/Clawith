"""Observable initial DingTalk delivery for a suspended confirmation."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import async_session, engine
from app.models.channel_config import ChannelConfig
from tests.test_confirmation_toolcall import _make_agent, _make_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    yield
    await engine.dispose()


async def test_dingtalk_suspension_delivers_card_to_exact_session_channel(monkeypatch):
    """The facade must connect suspension to the DingTalk delivery module."""
    from app.services import (
        agent_tools,
        confirmation_core,
        confirmation_service,
        dingtalk_card,
    )

    agent_id, user_id = await _make_agent()
    app_id = f"exact-dingtalk-app-{uuid.uuid4().hex}"
    external_conv_id = f"dingtalk_group_open-conversation-{uuid.uuid4().hex}"
    session = await _make_session(
        agent_id,
        user_id,
        source_channel="dingtalk",
        external_conv_id=external_conv_id,
        is_group=True,
    )
    async with async_session() as db:
        db.add(
            ChannelConfig(
                agent_id=agent_id,
                channel_type="dingtalk",
                app_id=app_id,
                app_secret="test-secret",
                is_configured=True,
                is_connected=True,
            )
        )
        await db.commit()

    captured: dict = {}

    async def fake_load_runtime(**_kwargs):
        return SimpleNamespace()

    async def fake_get_tool_config(target_agent_id, tool_name):
        assert target_agent_id == agent_id
        assert tool_name == "request_confirmation"
        return {"card_template_id": "confirmation-template.schema"}

    async def fake_send_confirmation_card(**kwargs):
        captured.update(kwargs)
        return kwargs["out_track_id"]

    monkeypatch.setattr(confirmation_service, "_broadcast", AsyncMock())
    monkeypatch.setattr(confirmation_core, "load_turn_runtime", fake_load_runtime)
    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        dingtalk_card,
        "send_confirmation_card",
        fake_send_confirmation_card,
    )

    row_id = await confirmation_service.suspend_for_confirmation(
        agent_id=agent_id,
        conversation_id=str(session.id),
        chat_session_id=session.id,
        source_channel="web",
        user_id=user_id,
        intro_text=None,
        title="发布确认",
        summary="即将发布变更",
        action={"tool": "deploy", "args": {"target": "test"}},
        risk_level="high",
        buttons=[{"text": "确认", "value": "confirm"}],
    )

    assert captured["app_id"] == app_id
    assert captured["card_template_id"] == "confirmation-template.schema"
    assert captured["out_track_id"] == str(row_id)
    assert captured["external_conv_id"] == external_conv_id
    assert captured["is_group"] is True
    assert captured["card_data"]["title"] == "发布确认"
    assert captured["card_data"]["summary"] == "即将发布变更"
