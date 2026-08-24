"""Unit tests for FeishuService.send_message reply_to_message_id behavior."""

from __future__ import annotations

import httpx
import pytest

from app.services.feishu_service import FeishuAPIError, feishu_service
from app.api.feishu import _build_card

pytestmark = pytest.mark.asyncio


async def test_streaming_card_sanitizes_every_user_visible_field():
    forbidden = "cla" + "with"
    card = _build_card(
        f"answer {forbidden}",
        thinking_text=f"thinking {forbidden.upper()}",
        tool_status_lines=[f"tool {forbidden}"],
        agent_name=f"agent {forbidden}",
    )

    assert forbidden.lower() not in str(card).lower()


async def test_file_send_records_only_business_success_before_later_failure(
    tmp_path,
    monkeypatch,
):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        url = str(request.url)
        if url.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "token"})
        if url.endswith("/im/v1/files"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"file_key": "file-key"}},
            )
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "caption-id"}},
            )
        return httpx.Response(200, json={"code": 230001, "msg": "file rejected"})

    _make_patched_client(monkeypatch, handler)
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 test")
    recorded = []

    async def on_result(role, result):
        recorded.append((role, result["data"]["message_id"]))

    with pytest.raises(FeishuAPIError, match="file_message"):
        await feishu_service.upload_and_send_file(
            "app",
            "secret",
            "recipient",
            report,
            accompany_msg="caption",
            on_result=on_result,
        )

    assert recorded == [("file_caption", "caption-id")]


def _make_patched_client(monkeypatch, handler):
    """Patch httpx.AsyncClient inside feishu_service to use a MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr(
        "app.services.feishu_service.httpx.AsyncClient", _PatchedAsyncClient
    )


async def test_send_message_uses_reply_endpoint_when_parent_id_provided(monkeypatch):
    """reply_to_message_id triggers POST /messages/<id>/reply, NOT plain send."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        captured.append({"url": url, "method": request.method})
        # Token endpoint
        if url.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(200, json={"app_access_token": "tok_xxx", "code": 0})
        # Reply endpoint
        if "/im/v1/messages/" in url and url.endswith("/reply"):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "data": {"message_id": "om_replied"}},
            )
        # Plain send fallback (should NOT be reached in this test)
        return httpx.Response(500, json={"code": 999, "msg": "unexpected plain send"})

    _make_patched_client(monkeypatch, handler)

    out = await feishu_service.send_message(
        "app_id_x", "app_secret_x", "oc_chat_id", "text",
        '{"text":"hi"}', receive_id_type="chat_id",
        reply_to_message_id="om_user_msg_id",
    )

    reply_calls = [c for c in captured if "/reply" in c["url"]]
    assert len(reply_calls) == 1, f"expected 1 reply call, got: {captured}"
    assert "om_user_msg_id/reply" in reply_calls[0]["url"]
    assert out.get("code") == 0
    assert out.get("data", {}).get("message_id") == "om_replied"


async def test_send_message_falls_back_to_plain_when_reply_fails(monkeypatch):
    """When reply endpoint returns non-zero code, plain send is invoked instead."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        captured.append(url)
        if url.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(200, json={"app_access_token": "tok", "code": 0})
        if "/reply" in url:
            return httpx.Response(200, json={"code": 230020, "msg": "permission denied"})
        # Plain send must be reached on fallback
        return httpx.Response(
            200,
            json={"code": 0, "msg": "ok", "data": {"message_id": "om_plain"}},
        )

    _make_patched_client(monkeypatch, handler)

    out = await feishu_service.send_message(
        "a", "b", "open_id_xxx", "text",
        '{"text":"hi"}',
        reply_to_message_id="om_dead_msg",
    )

    assert any("/reply" in u for u in captured), "reply must be attempted first"
    assert any(u.endswith("?receive_id_type=open_id") for u in captured), (
        "plain send fallback should fire after reply failed"
    )
    assert out.get("code") == 0
    assert out.get("data", {}).get("message_id") == "om_plain"


async def test_send_message_skips_reply_when_parent_id_is_empty(monkeypatch):
    """No reply_to_message_id (or empty/None) → never hits the reply endpoint."""
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        captured_urls.append(url)
        if url.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(200, json={"app_access_token": "tok", "code": 0})
        return httpx.Response(
            200,
            json={"code": 0, "msg": "ok", "data": {"message_id": "om_plain_only"}},
        )

    _make_patched_client(monkeypatch, handler)

    out = await feishu_service.send_message(
        "a", "b", "ou_xxx", "text", '{"text":"hi"}',
    )

    assert not any("/reply" in u for u in captured_urls), (
        f"reply endpoint must not be called when no parent id; got urls: {captured_urls}"
    )
    assert out.get("code") == 0
    assert out.get("data", {}).get("message_id") == "om_plain_only"
