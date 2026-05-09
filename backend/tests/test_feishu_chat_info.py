"""Unit tests for FeishuService.get_chat_info."""

from __future__ import annotations

import httpx
import pytest

from app.services.feishu_service import feishu_service

pytestmark = pytest.mark.asyncio


async def test_get_chat_info_returns_data_on_success(monkeypatch):
    """Successful 200 with code=0 returns the data dict (which contains 'name')."""

    async def fake_token(self, app_id, app_secret):
        return "fake_token_xxx"

    monkeypatch.setattr(
        "app.services.feishu_service.FeishuService.get_tenant_access_token",
        fake_token,
    )

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {"name": "产品讨论组", "description": "team chat", "chat_id": "oc_demo"},
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", _PatchedAsyncClient)

    result = await feishu_service.get_chat_info("app_id_demo", "app_secret_demo", "oc_demo")

    assert result is not None
    assert result.get("name") == "产品讨论组"
    assert "oc_demo" in (captured.get("url") or "")
    assert (captured.get("auth") or "").startswith("Bearer fake_token_xxx")


async def test_get_chat_info_returns_none_on_api_error(monkeypatch):
    """API code != 0 returns None (graceful degradation; caller falls back)."""

    async def fake_token(self, app_id, app_secret):
        return "tok"
    monkeypatch.setattr(
        "app.services.feishu_service.FeishuService.get_tenant_access_token",
        fake_token,
    )

    def handler(request):
        return httpx.Response(200, json={"code": 99991663, "msg": "permission denied"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", _PatchedAsyncClient)

    result = await feishu_service.get_chat_info("a", "b", "oc_x")
    assert result is None


async def test_get_chat_info_returns_none_on_empty_chat_id():
    """Empty chat_id short-circuits to None without any API call."""
    result = await feishu_service.get_chat_info("a", "b", "")
    assert result is None


async def test_get_chat_info_returns_none_on_token_failure(monkeypatch):
    """If tenant_access_token cannot be fetched, return None without crashing."""

    async def fake_token(self, app_id, app_secret):
        return ""
    monkeypatch.setattr(
        "app.services.feishu_service.FeishuService.get_tenant_access_token",
        fake_token,
    )
    result = await feishu_service.get_chat_info("a", "b", "oc_x")
    assert result is None


async def test_get_chat_info_returns_none_on_network_exception(monkeypatch):
    """Network error / unexpected exception returns None instead of raising."""

    async def fake_token(self, app_id, app_secret):
        return "tok"
    monkeypatch.setattr(
        "app.services.feishu_service.FeishuService.get_tenant_access_token",
        fake_token,
    )

    def handler(request):
        raise httpx.ConnectError("network down")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", _PatchedAsyncClient)

    result = await feishu_service.get_chat_info("a", "b", "oc_x")
    assert result is None
