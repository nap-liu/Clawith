"""Tests for SDK OAuth identity endpoints (sdk_auth.py)."""

import pytest
from urllib.parse import urlparse, parse_qs

import httpx
from sqlalchemy import func, select

from app.api.sdk_auth import sign_sdk_state, verify_sdk_state
from app.database import async_session, engine
from app.main import app
from app.models.identity import IdentityProvider
from app.models.user import User

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Sub-task A — signed state helpers
# ---------------------------------------------------------------------------

def test_state_roundtrip_returns_return_to():
    st = sign_sdk_state("https://ai.yeyecha.com/p/abc123", ttl_seconds=600)
    data = verify_sdk_state(st)
    assert data is not None
    assert data["return_to"] == "https://ai.yeyecha.com/p/abc123"


def test_state_tampered_rejected():
    st = sign_sdk_state("https://ai.yeyecha.com/p/abc123", ttl_seconds=600)
    tampered = st[:-2] + ("aa" if st[-2:] != "aa" else "bb")
    assert verify_sdk_state(tampered) is None


def test_state_expired_rejected():
    st = sign_sdk_state("https://ai.yeyecha.com/p/abc123", ttl_seconds=-1)
    assert verify_sdk_state(st) is None


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_oauth2_provider():
    async with async_session() as db:
        db.add(IdentityProvider(
            provider_type="oauth2",
            name="测试SSO",
            is_active=True,
            sso_login_enabled=True,
            config={
                "app_id": "yyc-claw",
                "app_secret": "secret-xyz",
                "authorize_url": "https://sso.example.com/oauth2/authorize",
                "scope": "openid",
                "field_mapping": {"user_id": "userId", "name": "userName", "mobile": "mobile"},
            },
        ))
        await db.commit()


# ---------------------------------------------------------------------------
# Sub-task B — /api/sdk/auth/start
# ---------------------------------------------------------------------------

async def test_start_redirects_to_authorize_with_return_to():
    await _seed_oauth2_provider()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/sdk/auth/start",
            params={"return_to": "https://ai.yeyecha.com/p/abc123"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://sso.example.com/oauth2/authorize?")
    q = parse_qs(urlparse(loc).query)
    assert q["client_id"] == ["yyc-claw"]
    assert q["redirect_uri"] == ["https://ai.yeyecha.com/p/abc123"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["openid"]
    assert "state" in q


async def test_start_rejects_non_p_path():
    await _seed_oauth2_provider()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/sdk/auth/start",
            params={"return_to": "https://evil.com/phish"},
            follow_redirects=False,
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Sub-task C — /api/sdk/auth/exchange
# ---------------------------------------------------------------------------

def _mock_oauth_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "AT-123", "token_type": "Bearer"})
        if url.endswith("/oauth2/userinfo"):
            return httpx.Response(200, json={"userId": "u-1001", "userName": "张三", "mobile": "13800000000"})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


async def test_exchange_returns_user_info_and_creates_no_user(monkeypatch):
    await _seed_oauth2_provider()
    async with async_session() as db:
        before = (await db.execute(select(func.count(User.id)))).scalar()

    mock_transport = _mock_oauth_transport()
    _RealAsyncClient = httpx.AsyncClient

    class _Patched(_RealAsyncClient):
        def __init__(self, *a, **kw):
            kw.pop("timeout", None)
            super().__init__(*a, transport=mock_transport, **kw)

    monkeypatch.setattr("app.api.sdk_auth.httpx.AsyncClient", _Patched)
    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _Patched)

    state = sign_sdk_state("https://ai.yeyecha.com/p/abc123")
    asgi = httpx.ASGITransport(app=app)
    async with _RealAsyncClient(transport=asgi, base_url="http://test") as client:
        resp = await client.post("/api/sdk/auth/exchange", json={"code": "CODE-1", "state": state})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"userId": "u-1001", "userName": "张三", "mobile": "13800000000"}

    async with async_session() as db:
        after = (await db.execute(select(func.count(User.id)))).scalar()
    assert after == before, "exchange 不得创建 User"


async def test_exchange_rejects_bad_state():
    await _seed_oauth2_provider()
    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi, base_url="http://test") as client:
        resp = await client.post("/api/sdk/auth/exchange", json={"code": "X", "state": "garbage.sig"})
    assert resp.status_code == 400
