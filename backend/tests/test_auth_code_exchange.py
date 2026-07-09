"""Tests for platform-login OAuth code exchange used by H5 chat."""

import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.main import app
from app.models.identity import IdentityProvider
from app.models.tenant import Tenant
from app.models.user import User
from app.services.auth_provider import OAuth2AuthProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    async with async_session() as db:
        await db.execute(delete(IdentityProvider))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(IdentityProvider))
        await db.commit()
    await engine.dispose()


async def test_oauth2_token_exchange_includes_redirect_uri(monkeypatch):
    captured_forms: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_forms.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "AT-redirect", "token_type": "Bearer"})

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)

    provider = OAuth2AuthProvider(
        config={
            "app_id": "client-1",
            "app_secret": "secret-1",
            "token_url": "https://oauth.example.com/token",
        }
    )

    token_data = await provider.exchange_code_for_token(
        "CODE-1",
        "https://app.example.com/h5/agents/a1/chat?channel=wechat_miniprogram&provider=oauth-h5",
    )

    assert token_data["access_token"] == "AT-redirect"
    assert captured_forms == [
        {
            "grant_type": "authorization_code",
            "code": "CODE-1",
            "redirect_uri": "https://app.example.com/h5/agents/a1/chat?channel=wechat_miniprogram&provider=oauth-h5",
        }
    ]


async def test_oauth2_token_exchange_can_omit_redirect_uri(monkeypatch):
    captured_forms: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_forms.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "AT-no-redirect", "token_type": "Bearer"})

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)

    provider = OAuth2AuthProvider(
        config={
            "app_id": "client-1",
            "app_secret": "secret-1",
            "token_url": "https://oauth.example.com/token",
            "token_exchange_redirect_uri": False,
        }
    )

    token_data = await provider.exchange_code_for_token(
        "CODE-1",
        "https://app.example.com/h5/agents/a1/chat?channel=wechat_miniprogram&provider=oauth-h5",
    )

    assert token_data["access_token"] == "AT-no-redirect"
    assert captured_forms == [
        {
            "grant_type": "authorization_code",
            "code": "CODE-1",
        }
    ]


async def test_auth_code_exchange_uses_provider_key_and_returns_platform_token(monkeypatch):
    async with async_session() as db:
        db.add(
            IdentityProvider(
                provider_type="oauth2",
                name="H5 OAuth",
                is_active=True,
                sso_login_enabled=True,
                config={
                    "provider_key": "oauth-h5",
                    "app_id": "h5-client",
                    "app_secret": "h5-secret",
                    "token_url": "https://oauth.example.com/token",
                    "user_info_url": "https://oauth.example.com/userinfo",
                    "field_mapping": {
                        "user_id": "userId",
                        "name": "userName",
                        "email": "email",
                        "mobile": "mobile",
                    },
                    "allowed_purposes": ["h5_agent_chat"],
                    "allowed_redirect_hosts": ["app.example.com"],
                    "allowed_redirect_paths": ["/h5/agents/*/chat"],
                },
            )
        )
        await db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://oauth.example.com/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["code"] == "CODE-H5"
            assert form["redirect_uri"] == (
                "https://app.example.com/h5/agents/a1/chat"
                "?channel=wechat_miniprogram&provider=oauth-h5"
            )
            return httpx.Response(200, json={"access_token": "AT-H5", "token_type": "Bearer"})
        if url == "https://oauth.example.com/userinfo":
            return httpx.Response(
                200,
                json={
                    "userId": "h5-user-1",
                    "userName": "H5 User",
                    "email": "h5-user-1@example.com",
                    "mobile": "13800000001",
                },
            )
        return httpx.Response(404, json={})

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    _RealAsyncClient = httpx.AsyncClient
    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)

    transport = httpx.ASGITransport(app=app)
    async with _RealAsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/code/exchange",
            json={
                "provider": "oauth-h5",
                "code": "CODE-H5",
                "state": "STATE-H5",
                "purpose": "h5_agent_chat",
                "channel": "wechat_miniprogram",
                "redirect_uri": (
                    "https://app.example.com/h5/agents/a1/chat"
                    "?channel=wechat_miniprogram&provider=oauth-h5"
                ),
                "context": {"agent_id": "a1"},
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["display_name"] == "H5 User"
    assert body["needs_company_setup"] is True

    async with async_session() as db:
        user = (
            await db.execute(
                select(User).join(User.identity).where(User.display_name == "H5 User")
            )
        ).scalar_one()
        assert user.registration_source == "oauth2"


async def test_auth_code_exchange_filters_provider_type_by_purpose(monkeypatch):
    async with async_session() as db:
        db.add(
            IdentityProvider(
                provider_type="oauth2",
                name="SDK OAuth",
                is_active=True,
                sso_login_enabled=True,
                config={
                    "app_id": "sdk-client",
                    "app_secret": "sdk-secret",
                    "authorize_url": "https://sso.example.com/oauth2/authorize",
                    "token_url": "https://sso.example.com/oauth2/token",
                    "user_info_url": "https://sso.example.com/oauth2/userinfo",
                },
            )
        )
        db.add(
            IdentityProvider(
                provider_type="oauth2",
                name="H5 OAuth",
                is_active=True,
                sso_login_enabled=True,
                config={
                    "provider_key": "oauth-h5",
                    "app_id": "h5-client",
                    "app_secret": "h5-secret",
                    "token_url": "https://oauth.example.com/token",
                    "user_info_url": "https://oauth.example.com/userinfo",
                    "field_mapping": {"user_id": "userId", "name": "userName", "email": "email"},
                    "allowed_purposes": ["h5_agent_chat"],
                    "allowed_redirect_hosts": ["app.example.com"],
                    "allowed_redirect_paths": ["/h5/agents/*/chat"],
                },
            )
        )
        await db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://oauth.example.com/")
        if str(request.url) == "https://oauth.example.com/token":
            return httpx.Response(200, json={"access_token": "AT-H5", "token_type": "Bearer"})
        if str(request.url) == "https://oauth.example.com/userinfo":
            return httpx.Response(
                200,
                json={"userId": "h5-user-type", "userName": "H5 Type User", "email": "h5-type@example.com"},
            )
        return httpx.Response(404, json={})

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    _RealAsyncClient = httpx.AsyncClient
    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)

    transport = httpx.ASGITransport(app=app)
    async with _RealAsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/code/exchange",
            json={
                "provider": "oauth2",
                "code": "CODE-H5",
                "purpose": "h5_agent_chat",
                "channel": "wechat_miniprogram",
                "redirect_uri": (
                    "https://app.example.com/h5/agents/a1/chat"
                    "?channel=wechat_miniprogram&provider=oauth2"
                ),
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["display_name"] == "H5 Type User"


async def test_auth_code_exchange_binds_user_to_provider_tenant(monkeypatch):
    tenant_id = uuid.uuid4()
    async with async_session() as db:
        db.add(Tenant(id=tenant_id, name="H5 Company", slug=f"h5-{uuid.uuid4().hex[:8]}"))
        db.add(
            IdentityProvider(
                provider_type="oauth2",
                name="Tenant H5 OAuth",
                is_active=True,
                sso_login_enabled=True,
                tenant_id=tenant_id,
                config={
                    "provider_key": "oauth-h5-tenant",
                    "app_id": "h5-client",
                    "app_secret": "h5-secret",
                    "token_url": "https://oauth.example.com/token",
                    "user_info_url": "https://oauth.example.com/userinfo",
                    "field_mapping": {"user_id": "userId", "name": "userName", "email": "email"},
                    "allowed_purposes": ["h5_agent_chat"],
                    "allowed_redirect_hosts": ["app.example.com"],
                    "allowed_redirect_paths": ["/h5/agents/*/chat"],
                },
            )
        )
        await db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://oauth.example.com/token":
            return httpx.Response(200, json={"access_token": "AT-H5", "token_type": "Bearer"})
        if str(request.url) == "https://oauth.example.com/userinfo":
            return httpx.Response(
                200,
                json={"userId": "tenant-user-1", "userName": "Tenant User", "email": "tenant-user@example.com"},
            )
        return httpx.Response(404, json={})

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    _RealAsyncClient = httpx.AsyncClient
    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)

    transport = httpx.ASGITransport(app=app)
    async with _RealAsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/code/exchange",
            json={
                "provider": "oauth-h5-tenant",
                "code": "CODE-H5",
                "purpose": "h5_agent_chat",
                "channel": "wechat_miniprogram",
                "redirect_uri": (
                    "https://app.example.com/h5/agents/a1/chat"
                    "?channel=wechat_miniprogram&provider=oauth-h5-tenant"
                ),
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["needs_company_setup"] is False

    async with async_session() as db:
        user = (
            await db.execute(
                select(User).join(User.identity).where(User.display_name == "Tenant User")
            )
        ).scalar_one()
        assert user.tenant_id == tenant_id
