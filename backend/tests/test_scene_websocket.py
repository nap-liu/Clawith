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
