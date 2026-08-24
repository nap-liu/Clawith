import uuid

import pytest

from app.api import dingtalk as dingtalk_api
from app.services import agent_tools, dingtalk_stream, im_delivery
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


@pytest.mark.asyncio
async def test_command_webhook_business_error_never_finalizes_sent(monkeypatch):
    calls = []
    finalized = []
    response = _Response({"errcode": 40035, "errmsg": "invalid webhook"})
    monkeypatch.setattr(
        dingtalk_api.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, calls),
    )

    async def register(_message_id, result):
        finalized.append(result)
        return True

    monkeypatch.setattr(im_delivery, "register_delivery", register)

    result = await dingtalk_api._deliver_dingtalk_command_reply(
        message_id=uuid.uuid4(),
        session_webhook="https://example.invalid/session-webhook",
        conversation_ref="conversation-1",
        message="command reply",
    )

    assert len(calls) == 1
    assert result.status == "failed"
    assert finalized == [result]
    assert all(item.status != "sent" for item in finalized)


@pytest.mark.asyncio
async def test_session_webhook_business_error_never_records_file_part(monkeypatch):
    calls = []
    recorded = []
    response = _Response({"errcode": 310000, "errmsg": "invalid session"})
    monkeypatch.setattr(
        dingtalk_api.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, calls),
    )

    async def record(part):
        recorded.append(part)

    monkeypatch.setattr(agent_tools, "record_channel_file_part", record)
    part = IMDeliveryPart(
        transport="dingtalk_session_webhook",
        conversation_ref="conversation-1",
        artifact_role="file_fallback",
        recallable=False,
    )

    with pytest.raises(RuntimeError, match="dingtalk_session_webhook_error"):
        await dingtalk_api._deliver_dingtalk_session_webhook_part(
            session_webhook="https://example.invalid/session-webhook",
            payload={"msgtype": "text", "text": {"content": "fallback"}},
            part=part,
        )

    assert len(calls) == 1
    assert recorded == []


def test_legacy_markdown_title_is_sanitized():
    forbidden = "cla" + "with"
    payload = dingtalk_api._dingtalk_markdown_payload(
        f"[Agent] {forbidden.upper()} Helper",
        "safe reply",
    )

    assert forbidden not in payload["markdown"]["title"].lower()
    assert payload["markdown"]["text"] == "safe reply"


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
        return False

    async def register(message_id, result):
        assert message_id == receipt_id
        finalized.append(result)
        return True

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
