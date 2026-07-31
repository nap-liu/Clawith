import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.websocket import WebSocketChatHandler


def _handler(scene_manifest):
    handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    handler.scene_manifest = scene_manifest
    return handler


def test_published_scene_welcome_takes_priority_over_generated_onboarding():
    handler = _handler({"welcome_message": "欢迎使用报修服务"})

    assert handler._resolve_onboarding_required(True) is False
    assert handler._resolve_onboarding_required(False) is False


def test_empty_scene_welcome_keeps_existing_onboarding_fallback():
    for scene_manifest in (None, {}, {"welcome_message": ""}, {"welcome_message": "   "}):
        handler = _handler(scene_manifest)

        assert handler._resolve_onboarding_required(True) is True
        assert handler._resolve_onboarding_required(False) is False


def test_current_scene_quick_actions_are_part_of_channel_context():
    handler = _handler(
        {
            "scene_key": "warranty",
            "revision": 3,
            "welcome_message": "",
            "system_prompts": [],
            "quick_actions": [
                {
                    "id": "repair",
                    "label": "我要报修",
                    "type": "send_message",
                    "enabled": True,
                    "message": "我要申请设备保修",
                },
                {
                    "id": "paused",
                    "label": "暂停入口",
                    "type": "send_message",
                    "enabled": False,
                    "message": "不应注入",
                },
            ],
        }
    )
    handler.source_channel = "web"

    context = handler._channel_context()

    assert context["scene_key"] == "warranty"
    assert context["scene_revision"] == 3
    assert context["scene_quick_actions"] == [
        {
            "id": "repair",
            "label": "我要报修",
            "type": "send_message",
            "enabled": True,
            "message": "我要申请设备保修",
        }
    ]


@pytest.mark.asyncio
async def test_initial_assistant_message_has_stable_session_identity():
    class WelcomeSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_json(self):
            raise WebSocketDisconnect()

    session_id = str(uuid.uuid4())
    handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    handler.conv_id = session_id
    handler.pending_initial_assistant = {"content": "欢迎使用报修服务"}
    handler.welcome_message = ""
    handler.history_messages = []
    handler.onboarding_required = False
    handler.websocket = WelcomeSocket()

    with pytest.raises(WebSocketDisconnect):
        await handler.message_loop()

    assert handler.websocket.sent == [
        {
            "type": "done",
            "role": "assistant",
            "content": "欢迎使用报修服务",
            "message_id": f"initial-assistant:{session_id}",
        }
    ]


@pytest.mark.asyncio
async def test_quota_check_uses_stable_authenticated_user_id(monkeypatch):
    check_conversation_quota = AsyncMock()
    check_agent_expired = AsyncMock()
    monkeypatch.setattr(
        "app.api.websocket.check_conversation_quota",
        check_conversation_quota,
    )
    monkeypatch.setattr(
        "app.api.websocket.check_agent_expired",
        check_agent_expired,
    )

    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    handler = WebSocketChatHandler.__new__(WebSocketChatHandler)
    handler.user_id = user_id
    handler.agent_id = agent_id
    handler.websocket = SimpleNamespace()

    assert await handler._check_quotas() is True
    check_conversation_quota.assert_awaited_once_with(user_id)
    check_agent_expired.assert_awaited_once_with(agent_id)


def test_websocket_handler_does_not_retain_user_or_agent_orm_entities():
    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=uuid.uuid4(),
        token="unused",
    )

    assert not hasattr(handler, "user")
    assert not hasattr(handler, "agent")
