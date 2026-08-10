import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from jose import jwt
from starlette.requests import Request
from starlette.responses import Response

from app.api.sso import SSO_SESSION_HOURS, create_sso_session
from app.config import get_settings
from app.database import async_session, engine
from app.main import app
from app.models.identity import IdentityProvider, SSOScanSession
from app.models.tenant import Tenant
from app.services.sso_login_state import (
    SSO_STATE_HOURS,
    create_sso_browser_binding,
    create_sso_login_state,
    get_enabled_sso_provider,
    parse_sso_login_state,
    sso_browser_cookie_name,
    sso_completion_url,
    verify_sso_browser_binding,
)


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


def test_sso_state_lasts_twelve_hours_and_carries_complete_query():
    sid = uuid.uuid4()
    provider_id = uuid.uuid4()
    query = "return_to=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1&sso=dingtalk&tenant_id=abc"
    token = create_sso_login_state(sid, provider_id, query)

    assert SSO_STATE_HOURS == 12
    assert parse_sso_login_state(token) == (sid, provider_id, query)
    payload = jwt.decode(token, get_settings().SECRET_KEY, algorithms=["HS256"])
    remaining = datetime.fromtimestamp(payload["exp"], timezone.utc) - datetime.now(timezone.utc)
    assert 11.9 * 3600 < remaining.total_seconds() <= 12 * 3600 + 1
    assert sso_completion_url(sid, query) == f"/sso/entry?sid={sid}&complete=1&{query}"


def test_sso_browser_binding_is_signed_and_session_specific():
    sid = uuid.uuid4()
    token = create_sso_browser_binding(sid)
    assert verify_sso_browser_binding(sid, token) is True
    assert verify_sso_browser_binding(uuid.uuid4(), token) is False
    assert verify_sso_browser_binding(sid, token + "broken") is False
    assert sso_browser_cookie_name(sid).endswith(sid.hex)


@pytest.mark.asyncio
async def test_legacy_sso_session_also_lasts_twelve_hours():
    class RecordingSession:
        def __init__(self):
            self.added = None

        def add(self, value):
            self.added = value

        async def commit(self):
            return None

    db = RecordingSession()
    before = datetime.now(timezone.utc)
    response = Response()
    request = Request({
        "type": "http", "method": "POST", "scheme": "http", "path": "/api/sso/session",
        "raw_path": b"/api/sso/session", "query_string": b"", "headers": [],
        "client": ("test", 123), "server": ("test", 80),
    })
    result = await create_sso_session(response=response, request=request, tenant_id=None, db=db)
    remaining = db.added.expires_at - before
    assert SSO_SESSION_HOURS == 12
    assert 11.9 * 3600 < remaining.total_seconds() <= 12 * 3600 + 1
    assert result["session_id"] == str(db.added.id)
    assert sso_browser_cookie_name(db.added.id) in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_starting_a_new_sso_session_removes_stale_browser_bindings():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/sso/session")
        first_cookie = sso_browser_cookie_name(uuid.UUID(first.json()["session_id"]))
        assert client.cookies.get(first_cookie)

        second = await client.post("/api/sso/session")
        second_cookie = sso_browser_cookie_name(uuid.UUID(second.json()["session_id"]))

        binding_cookies = [cookie.name for cookie in client.cookies.jar if cookie.name.startswith("sso_browser_binding_")]
        assert binding_cookies == [second_cookie]
        assert client.cookies.get(first_cookie) is None


@pytest.mark.asyncio
async def test_sso_status_cannot_be_claimed_from_another_browser():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as owner:
        created = await owner.post("/api/sso/session")
        assert created.status_code == 200
        sid = created.json()["session_id"]

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as attacker:
            denied = await attacker.get(f"/api/sso/session/{sid}/status")
            assert denied.status_code == 403

        allowed = await owner.get(f"/api/sso/session/{sid}/status")
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_authorized_sso_status_returns_token_once_and_clears_browser_binding():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as owner:
        created = await owner.post("/api/sso/session")
        assert created.status_code == 200
        sid = uuid.UUID(created.json()["session_id"])
        cookie_name = sso_browser_cookie_name(sid)
        assert owner.cookies.get(cookie_name)

        async with async_session() as db:
            session = await db.get(SSOScanSession, sid)
            session.status = "authorized"
            session.access_token = "one-time-token"
            await db.commit()

        completed = await owner.get(f"/api/sso/session/{sid}/status")
        assert completed.status_code == 200
        assert completed.json()["status"] == "authorized"
        assert completed.json()["access_token"] == "one-time-token"
        assert owner.cookies.get(cookie_name) is None

        async with async_session() as db:
            session = await db.get(SSOScanSession, sid)
            assert session.status == "completed"


@pytest.mark.asyncio
async def test_sso_provider_order_uses_enable_time_only():
    async with async_session() as db:
        tenant = Tenant(name="SSO Order Test", slug=f"sso-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        older = IdentityProvider(
            provider_type="dingtalk", name="Older Enabled", tenant_id=tenant.id,
            is_active=True, sso_login_enabled=True,
            sso_enabled_at=datetime.now(timezone.utc) - timedelta(days=1), config={},
        )
        newer = IdentityProvider(
            provider_type="wecom", name="Newer Enabled", tenant_id=tenant.id,
            is_active=True, sso_login_enabled=True,
            sso_enabled_at=datetime.now(timezone.utc), config={},
        )
        db.add_all([older, newer])
        await db.commit()
        tenant_id = tenant.id
        newer_id = newer.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/sso/providers?tenant_id={tenant_id}")
    assert response.status_code == 200
    assert [item["name"] for item in response.json()[:2]] == ["Newer Enabled", "Older Enabled"]

    async with async_session() as db:
        provider = await db.get(IdentityProvider, newer_id)
        provider.sso_login_enabled = False
        await db.commit()
        assert await get_enabled_sso_provider(db, newer_id, "wecom", tenant_id) is None


@pytest.mark.asyncio
async def test_sso_start_binds_browser_and_exact_provider():
    async with async_session() as db:
        tenant = Tenant(name="SSO Start Test", slug=f"start-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        provider = IdentityProvider(
            provider_type="oauth2", name="Exact OAuth", tenant_id=tenant.id,
            is_active=True, sso_login_enabled=True, sso_enabled_at=datetime.now(timezone.utc),
            config={
                "app_id": "client-id", "app_secret": "client-secret",
                "authorize_url": "https://identity.example/authorize", "scope": "openid profile",
            },
        )
        db.add(provider)
        await db.commit()
        tenant_id, provider_id = tenant.id, provider.id

    transport = httpx.ASGITransport(app=app)
    query = "return_to=https%3A%2F%2Felsewhere.example%2Fdone&sso=oauth2"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as owner:
        started = await owner.post("/api/sso/start", json={
            "tenant_id": str(tenant_id), "provider_type": "oauth2", "login_query": query,
        })
        assert started.status_code == 200
        payload = started.json()
        sid = uuid.UUID(payload["session_id"])
        state = parse_qs(urlparse(payload["authorization_url"]).query)["state"][0]
        assert parse_sso_login_state(state) == (sid, provider_id, query)
        assert sso_browser_cookie_name(sid) in started.headers["set-cookie"]

        owner_status = await owner.get(f"/api/sso/session/{sid}/status")
        assert owner_status.status_code == 200

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as attacker:
        attacker_status = await attacker.get(f"/api/sso/session/{sid}/status")
        assert attacker_status.status_code == 403
