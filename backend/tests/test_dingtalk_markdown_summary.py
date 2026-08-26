import json

import pytest

from app.services import dingtalk_service, turn_runtime


class _Response:
    status_code = 200

    def json(self):
        return {"errcode": 0, "processQueryKey": "provider-message-1"}


class _Client:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


@pytest.mark.asyncio
async def test_dingtalk_markdown_transports_use_visible_message_summary(monkeypatch):
    calls = []

    async def get_token(_key, _secret):
        return "token"

    async def get_access_token(_key, _secret):
        return {"access_token": "token", "expires_in": 7200}

    monkeypatch.setattr(dingtalk_service, "get_dingtalk_access_token", get_access_token)
    monkeypatch.setattr(
        "app.services.dingtalk_token.dingtalk_token_manager.get_token",
        get_token,
    )
    monkeypatch.setattr(
        dingtalk_service.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(calls),
    )
    monkeypatch.setattr(
        turn_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(calls),
    )

    message = "## 发布结果\n\n**服务已更新**"
    await dingtalk_service.send_dingtalk_v1_robot_oto_message(
        "app",
        "secret",
        ["staff"],
        message,
        msg_type="markdown",
    )
    await turn_runtime._send_dingtalk_group_markdown(
        app_id="app",
        app_secret="secret",
        open_conversation_id="conversation",
        message=message,
    )

    assert len(calls) == 2
    for _url, kwargs in calls:
        assert json.loads(kwargs["json"]["msgParam"]) == {
            "title": "发布结果 服务已更新",
            "text": message,
        }


def test_dingtalk_markdown_never_uses_notification_placeholder():
    payload = dingtalk_service.build_dingtalk_markdown_content("![执行结果](https://example.com/result.png)")

    assert payload == {
        "title": "执行结果",
        "text": "![执行结果](https://example.com/result.png)",
    }
