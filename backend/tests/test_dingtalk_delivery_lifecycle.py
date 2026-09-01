import json
import uuid

import httpx
import pytest

from app.api import dingtalk as dingtalk_api
from app.services import agent_tools, dingtalk_service, dingtalk_stream, im_delivery, turn_runtime
from app.services.im_delivery import (
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _UnreadableResponse(_Response):
    def json(self):
        raise ValueError("invalid provider JSON")


class _Client:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self._calls.append((url, kwargs))
        return self._response


class _TimeoutClient:
    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self._calls.append((url, kwargs))
        raise httpx.ReadTimeout("provider response timed out")


@pytest.mark.asyncio
async def test_command_reply_uses_persisted_delivery_and_exact_session_lock_key(monkeypatch):
    deliveries = []

    async def deliver(**kwargs):
        deliveries.append(kwargs)
        return IMDeliveryResult.failed("dingtalk", "provider_rejected")

    monkeypatch.setattr(im_delivery, "deliver_persisted_message", deliver)

    message_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())
    result = await dingtalk_api._deliver_dingtalk_command_reply(
        message_id=message_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        external_conv_id="dingtalk_group_conversation-1",
        is_group=True,
        message="command reply",
    )

    assert len(deliveries) == 1
    assert deliveries[0]["message_id"] == message_id
    assert deliveries[0]["agent_id"] == agent_id
    runtime = deliveries[0]["runtime"]
    assert runtime.source_channel == "dingtalk"
    assert runtime.conversation_id == conversation_id
    assert runtime.external_conv_id == "dingtalk_group_conversation-1"
    assert runtime.is_group is True
    assert deliveries[0]["message"] == "command reply"
    assert deliveries[0]["content_format"] == "plain_text"
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_dingtalk_markdown_transports_use_plain_text_summary_and_keep_receipt(monkeypatch):
    calls = []
    response = _Response({"processQueryKey": "provider-message-1", "errcode": 0})

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
        lambda **_kwargs: _Client(response, calls),
    )
    monkeypatch.setattr(
        turn_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, calls),
    )

    person = await dingtalk_service.send_dingtalk_v1_robot_oto_message(
        "app",
        "secret",
        ["staff"],
        "## 发布结果\n\n**服务已更新**",
        msg_type="markdown",
    )
    group = await turn_runtime._send_dingtalk_group_markdown(
        app_id="app",
        app_secret="secret",
        open_conversation_id="conversation",
        message="## 发布结果\n\n**服务已更新**",
    )

    assert person["processQueryKey"] == "provider-message-1"
    assert group["processQueryKey"] == "provider-message-1"
    assert len(calls) == 2
    for _url, kwargs in calls:
        payload = kwargs["json"]
        message = json.loads(payload["msgParam"])
        assert message == {
            "title": "发布结果 服务已更新",
            "text": "## 发布结果\n\n**服务已更新**",
        }


@pytest.mark.asyncio
async def test_dingtalk_plain_text_transports_keep_command_newlines(monkeypatch):
    calls = []
    response = _Response({"processQueryKey": "provider-command-1", "errcode": 0})

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
        lambda **_kwargs: _Client(response, calls),
    )
    monkeypatch.setattr(
        turn_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, calls),
    )
    command = "可用模型：\nQwen3.7 Flash\nGLM 5.3"

    for is_group, target_id in ((False, "staff"), (True, "conversation")):
        result = await turn_runtime.send_dingtalk_proactive_text(
            app_id="app",
            app_secret="secret",
            target_id=target_id,
            is_group=is_group,
            message=command,
        )
        assert result["processQueryKey"] == "provider-command-1"

    assert len(calls) == 2
    for _url, kwargs in calls:
        payload = kwargs["json"]
        assert payload["msgKey"] == "sampleText"
        assert json.loads(payload["msgParam"]) == {"content": command}


@pytest.mark.asyncio
@pytest.mark.parametrize("is_group", [False, True])
async def test_dingtalk_proactive_transport_timeout_propagates_without_retry(
    monkeypatch,
    is_group,
):
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
        lambda **_kwargs: _TimeoutClient(calls),
    )
    monkeypatch.setattr(
        turn_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: _TimeoutClient(calls),
    )

    with pytest.raises(httpx.ReadTimeout):
        await turn_runtime.send_dingtalk_proactive_markdown(
            app_id="app",
            app_secret="secret",
            target_id="conversation" if is_group else "staff",
            is_group=is_group,
            message="timeout test",
        )

    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("is_group", [False, True])
async def test_dingtalk_proactive_unreadable_response_is_uncertain_without_retry(
    monkeypatch,
    is_group,
):
    calls = []

    async def get_token(_key, _secret):
        return "token"

    async def get_access_token(_key, _secret):
        return {"access_token": "token", "expires_in": 7200}

    client_factory = lambda **_kwargs: _Client(_UnreadableResponse({}), calls)
    monkeypatch.setattr(dingtalk_service, "get_dingtalk_access_token", get_access_token)
    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", get_token)
    monkeypatch.setattr(dingtalk_service.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(turn_runtime.httpx, "AsyncClient", client_factory)

    with pytest.raises(im_delivery.ProviderResponseUncertainError) as caught:
        await turn_runtime.send_dingtalk_proactive_markdown(
            app_id="app",
            app_secret="secret",
            target_id="conversation" if is_group else "staff",
            is_group=is_group,
            message="parse test",
        )

    assert IMDeliveryResult.from_exception("dingtalk", caught.value).status == "unknown"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dingtalk_media_unreadable_response_is_uncertain_without_retry(monkeypatch):
    calls = []

    async def get_token(_key, _secret):
        return "token"

    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", get_token)
    monkeypatch.setattr(
        dingtalk_stream.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(_UnreadableResponse({}), calls),
    )

    with pytest.raises(im_delivery.ProviderResponseUncertainError) as caught:
        await dingtalk_stream._send_dingtalk_media_message(
            "app",
            "secret",
            "conversation",
            "media-id",
            "file",
            "2",
            filename="report.pdf",
            raise_on_transport_error=True,
        )

    assert IMDeliveryResult.from_exception("dingtalk", caught.value).status == "unknown"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dingtalk_work_notification_uses_plain_text_summary(monkeypatch):
    captured = []

    async def send(_app_id, _app_secret, _user_id, msg_body, _agent_id):
        captured.append(msg_body)
        return {"errcode": 0}

    monkeypatch.setattr(dingtalk_service, "send_dingtalk_corp_conversation", send)

    result = await dingtalk_service.send_dingtalk_message(
        app_id="app",
        app_secret="secret",
        user_id="staff",
        message="## 发布结果\n\n**服务已更新**",
        agent_id="agent",
        use_robot=False,
        msg_type="markdown",
    )

    assert result == {"errcode": 0}
    assert captured == [
        {
            "msgtype": "markdown",
            "markdown": {
                "title": "发布结果 服务已更新",
                "text": "## 发布结果\n\n**服务已更新**",
            },
        }
    ]


def test_dingtalk_markdown_preserves_nonempty_format_only_content():
    assert dingtalk_service.build_dingtalk_markdown_content("---") == {
        "title": "非文本消息",
        "text": "---",
    }


def test_dingtalk_markdown_rejects_only_empty_content():
    with pytest.raises(ValueError, match="must not be empty"):
        dingtalk_service.build_dingtalk_markdown_content("  \n  ")


@pytest.mark.asyncio
async def test_provider_success_observer_failure_propagates_without_resend(monkeypatch):
    calls = []
    response = _Response({"processQueryKey": "provider-message-1", "errcode": 0})

    async def get_token(_key, _secret):
        return "token"

    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", get_token)
    monkeypatch.setattr(
        dingtalk_stream.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, calls),
    )

    async def observer(_result):
        raise DeliveryReceiptPersistenceError("database append unavailable")

    with pytest.raises(DeliveryReceiptPersistenceError):
        await dingtalk_stream._send_dingtalk_media_message(
            "app",
            "secret",
            "staff",
            "@file",
            "file",
            "1",
            filename="report.pdf",
            on_result=observer,
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_success_receipt_failure_is_unknown_without_fallback_io(
    tmp_path,
    monkeypatch,
):
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    report = workspace / "workspace" / "report.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF-1.4 test")
    receipt_id = uuid.uuid4()
    provider_calls = []
    fallback_calls = []
    finalized = []
    response = _Response({"processQueryKey": "provider-message-1", "errcode": 0})

    async def get_token(_key, _secret):
        return "token"

    async def claim(**_kwargs):
        return receipt_id

    async def append(_message_id, _part):
        raise DeliveryReceiptPersistenceError("receipt unavailable")

    async def register(message_id, result):
        assert message_id == receipt_id
        finalized.append(result)
        return True

    async def no_exact_session_route(_agent_id, _session_id):
        return False

    async def sender(_path, _message):
        parts = []

        async def observe(item):
            part = IMDeliveryPart(
                transport="dingtalk_openapi_oto",
                provider_message_id=item["processQueryKey"],
                conversation_ref="staff",
                artifact_role="channel_file",
            )
            await agent_tools.record_channel_file_part(part)
            parts.append(part)

        sent = await dingtalk_stream._send_dingtalk_media_message(
            "app",
            "secret",
            "staff",
            "@file",
            "file",
            "1",
            filename="report.pdf",
            on_result=observe,
        )
        if not sent:
            fallback_calls.append(True)
        return IMDeliveryResult.sent("dingtalk", *parts)

    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "_claim_channel_file_receipt", claim)
    monkeypatch.setattr(agent_tools, "append_delivery_part", append)
    monkeypatch.setattr(agent_tools, "register_delivery", register)
    monkeypatch.setattr(
        agent_tools,
        "_supports_exact_file_session_route",
        no_exact_session_route,
    )
    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", get_token)
    monkeypatch.setattr(
        dingtalk_stream.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, provider_calls),
    )
    token = agent_tools.channel_file_sender.set(sender)
    try:
        result = await agent_tools._send_channel_file(
            agent_id,
            workspace,
            {"file_path": "workspace/report.pdf"},
            tool_call_id="dingtalk-file-receipt-failure",
            origin_session_id=str(uuid.uuid4()),
        )
    finally:
        agent_tools.channel_file_sender.reset(token)

    assert "Failed to send file" in result
    assert len(provider_calls) == 1
    assert fallback_calls == []
    assert finalized[-1].status == "unknown"
