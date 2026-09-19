"""Tests for platform-login OAuth code exchange used by H5 chat."""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.main import app
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider, SSOScanSession
from app.models.org import ChannelUserBinding, OrgMember
from app.models.system_settings import SystemSetting
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.auth_code_exchange import validate_platform_login_channel
from app.services.auth_provider import OAuth2AuthProvider
from app.services.auth_registry import auth_provider_registry
from app.services.oauth_identity import oauth_authority_scope, sign_oauth2_sso_state
from app.services.sso_login_state import (
    create_sso_browser_binding,
    sso_browser_cookie_name,
)

pytestmark = pytest.mark.asyncio


async def test_neutral_miniprogram_channel_is_supported_for_platform_login():
    assert validate_platform_login_channel("miniprogram") == "miniprogram"
    assert validate_platform_login_channel("wechat_miniprogram") == "wechat_miniprogram"


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    auth_provider_registry.clear_all_cache()
    async with async_session() as db:
        await db.execute(delete(IdentityProvider))
        await db.execute(
            delete(SystemSetting).where(
                SystemSetting.key == "account_registration_enabled"
            )
        )
        await db.commit()
    yield
    auth_provider_registry.clear_all_cache()
    async with async_session() as db:
        await db.execute(delete(IdentityProvider))
        await db.execute(
            delete(SystemSetting).where(
                SystemSetting.key == "account_registration_enabled"
            )
        )
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


async def test_h5_and_regular_sso_share_code_only_token_exchange(monkeypatch):
    async with async_session() as db:
        tenant = Tenant(name="H5 OAuth Tenant", slug=f"h5-oauth-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.flush()
        provider = IdentityProvider(
            provider_type="oauth2",
            name="H5 OAuth",
            is_active=True,
            sso_login_enabled=True,
            tenant_id=tenant.id,
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
        scan_session = SSOScanSession(
            status="pending",
            tenant_id=tenant.id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        registration_setting = SystemSetting(
            key="account_registration_enabled",
            value={"enabled": False},
        )
        db.add_all([provider, scan_session, registration_setting])
        await db.commit()
        tenant_id = tenant.id
        scan_session_id = scan_session.id
        sso_state = sign_oauth2_sso_state(scan_session.id, provider.id)

    captured_token_forms: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://oauth.example.com/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            captured_token_forms.append(form)
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
        client.cookies.set(
            sso_browser_cookie_name(scan_session_id),
            create_sso_browser_binding(scan_session_id),
            path="/",
        )
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
        sso_resp = await client.get(
            "/api/auth/oauth2/callback",
            params={"code": "CODE-SSO", "state": sso_state},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["display_name"] == "H5 User"
    assert body["needs_company_setup"] is False
    assert sso_resp.status_code == 302
    assert sso_resp.headers["location"] == f"/sso/entry?sid={scan_session_id}&complete=1"
    assert captured_token_forms == [
        {"grant_type": "authorization_code", "code": "CODE-H5"},
        {"grant_type": "authorization_code", "code": "CODE-SSO"},
    ]

    async with async_session() as db:
        user = (
            await db.execute(
                select(User)
                .join(User.identity)
                .where(User.tenant_id == tenant_id, User.display_name == "H5 User")
            )
        ).scalar_one()
        assert user.registration_source == "oauth2"


async def test_qr_oauth_failure_rolls_back_authoritative_email_and_audit(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    old_email = f"qr-old-{suffix}@example.com"
    new_email = f"qr-new-{suffix}@example.com"
    subject = f"qr-subject-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name="QR Rollback", slug=f"qr-rollback-{suffix}")
        db.add(tenant)
        await db.flush()
        provider = IdentityProvider(
            provider_type="oauth2",
            name="QR OAuth",
            is_active=True,
            sso_login_enabled=True,
            tenant_id=tenant.id,
            config={
                "app_id": "qr-client",
                "app_secret": "qr-secret",
                "token_url": "https://qr.example.com/token",
                "user_info_url": "https://qr.example.com/userinfo",
                "field_mapping": {
                    "user_id": "userId",
                    "name": "userName",
                    "email": "email",
                    "mobile": "mobile",
                },
            },
        )
        identity = Identity(
            username=f"qr-user-{suffix}",
            email=old_email,
            phone=f"138{uuid.uuid4().int % 10**8:08d}",
            email_verified=True,
        )
        conflicting_identity = Identity(
            username=f"qr-conflict-{suffix}",
            email=f"qr-conflict-{suffix}@example.com",
            phone=f"139{uuid.uuid4().int % 10**8:08d}",
            email_verified=True,
        )
        db.add_all([provider, identity, conflicting_identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="QR User",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=subject,
            name="QR User",
            email=old_email,
            phone=identity.phone,
            user_id=user.id,
            status="active",
        )
        binding = ChannelUserBinding(
            tenant_id=tenant.id,
            provider_id=provider.id,
            installation_scope=oauth_authority_scope(provider),
            channel_type="oauth2",
            id_type="subject",
            subject=subject,
            user_id=user.id,
        )
        scan_session = SSOScanSession(
            status="pending",
            tenant_id=tenant.id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db.add_all([member, binding, scan_session])
        await db.commit()
        identity_id = identity.id
        user_id = user.id
        member_id = member.id
        scan_session_id = scan_session.id
        state = sign_oauth2_sso_state(scan_session.id, provider.id)
        conflicting_phone = conflicting_identity.phone

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://qr.example.com/token":
            return httpx.Response(200, json={"access_token": "AT-QR"})
        if str(request.url) == "https://qr.example.com/userinfo":
            return httpx.Response(
                200,
                json={
                    "userId": subject,
                    "userName": "QR User",
                    "email": new_email,
                    "mobile": conflicting_phone,
                },
            )
        return httpx.Response(404)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)
    transport = httpx.ASGITransport(app=app)
    async with real_async_client(transport=transport, base_url="http://test") as client:
        client.cookies.set(
            sso_browser_cookie_name(scan_session_id),
            create_sso_browser_binding(scan_session_id),
            path="/",
        )
        response = await client.get(
            "/api/auth/oauth2/callback",
            params={"code": "CODE-QR", "state": state},
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith("/sso/entry?error=authentication_failed")
    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == old_email
        assert (await db.get(OrgMember, member_id)).email == old_email
        assert (await db.get(SSOScanSession, scan_session_id)).status == "pending"
        audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.user_id == user_id,
                    AuditLog.action == "oauth_identity_email_refreshed",
                )
            )
        ).scalars().all()
        assert audits == []


async def test_auth_code_exchange_returns_safe_provider_rejection(monkeypatch):
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
                    "allowed_purposes": ["h5_agent_chat"],
                    "allowed_redirect_hosts": ["app.example.com"],
                    "allowed_redirect_paths": ["/h5/agents/*/chat"],
                },
            )
        )
        await db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth.example.com/token"
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form == {"grant_type": "authorization_code", "code": "SECRET-CODE"}
        return httpx.Response(200, json={"status": -1024, "msg": "invalid code: SECRET-CODE"})

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
                "code": "SECRET-CODE",
                "purpose": "h5_agent_chat",
                "channel": "wechat_miniprogram",
                "redirect_uri": "https://app.example.com/h5/agents/a1/chat",
            },
        )

    assert resp.status_code == 400
    assert resp.json() == {"detail": "OAuth provider rejected the authorization code"}
    assert "SECRET-CODE" not in resp.text


async def test_auth_code_exchange_validates_redirect_locally_before_token_exchange(monkeypatch):
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
                    "allowed_purposes": ["h5_agent_chat"],
                    "allowed_redirect_hosts": ["app.example.com"],
                    "allowed_redirect_paths": ["/h5/agents/*/chat"],
                },
            )
        )
        await db.commit()

    async def fail_if_exchanged(*args, **kwargs):
        pytest.fail("redirect_uri must be rejected before the provider token request")

    monkeypatch.setattr(OAuth2AuthProvider, "exchange_code_for_token", fail_if_exchanged)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/code/exchange",
            json={
                "provider": "oauth-h5",
                "code": "CODE-H5",
                "purpose": "h5_agent_chat",
                "channel": "wechat_miniprogram",
                "redirect_uri": "https://evil.example.com/h5/agents/a1/chat",
            },
        )

    assert resp.status_code == 400
    assert resp.json() == {"detail": "redirect_uri host not allowed"}


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
    user_suffix = uuid.uuid4().hex[:8]
    provider_user_id = f"tenant-user-{user_suffix}"
    display_name = f"Tenant User {user_suffix}"
    email = f"tenant-user-{user_suffix}@example.com"
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
                json={"userId": provider_user_id, "userName": display_name, "email": email},
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
                select(User).join(User.identity).where(User.display_name == display_name)
            )
        ).scalar_one()
        assert user.tenant_id == tenant_id


@pytest.mark.parametrize(
    ("stale_provider_type", "stale_status"),
    [
        ("dingtalk", "deleted"),
        ("feishu", "inactive"),
        ("wecom", "deleted"),
        ("scim", "inactive"),
    ],
)
async def test_h5_fresh_source_reactivates_user_without_restoring_stale_source(
    monkeypatch,
    stale_provider_type,
    stale_status,
):
    suffix = uuid.uuid4().hex[:8]
    phone = f"137{uuid.uuid4().int % 10**8:08d}"
    subject = f"reactivate-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name="H5 Reactivation", slug=f"h5-reactivate-{suffix}")
        db.add(tenant)
        await db.flush()
        oauth_provider = IdentityProvider(
            provider_type="oauth2",
            name="H5 Login",
            tenant_id=tenant.id,
            is_active=True,
            sso_login_enabled=True,
            config={
                "provider_key": f"h5-reactivate-{suffix}",
                "app_id": "client",
                "app_secret": "secret",
                "token_url": "https://reactivate.example.com/token",
                "user_info_url": "https://reactivate.example.com/userinfo",
                "field_mapping": {"user_id": "id", "name": "name", "mobile": "phone"},
                "allowed_purposes": ["h5_agent_chat"],
                "allowed_redirect_hosts": ["app.example.com"],
                "allowed_redirect_paths": ["/h5/agents/*/chat"],
            },
        )
        stale_provider = IdentityProvider(
            provider_type=stale_provider_type,
            name=f"Stale {stale_provider_type}",
            tenant_id=tenant.id,
            is_active=True,
            sso_login_enabled=False,
            config={},
        )
        identity = Identity(phone=phone, username=f"reactivate-{suffix}", email_verified=True)
        db.add_all([oauth_provider, stale_provider, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Reactivated User",
            role="member",
            is_active=False,
        )
        db.add(user)
        await db.flush()
        stale_member = OrgMember(
            tenant_id=tenant.id,
            provider_id=stale_provider.id,
            external_id=f"staff-{suffix}",
            name="Reactivated User",
            phone=phone,
            user_id=user.id,
            status=stale_status,
        )
        previous_oauth_member = OrgMember(
            tenant_id=tenant.id,
            provider_id=oauth_provider.id,
            external_id=f"previous-{subject}",
            name="Previous OAuth Subject",
            user_id=user.id,
            status="deleted",
        )
        db.add_all([stale_member, previous_oauth_member])
        await db.commit()
        user_id = user.id
        stale_member_id = stale_member.id
        previous_oauth_member_id = previous_oauth_member.id
        provider_key = oauth_provider.config["provider_key"]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://reactivate.example.com/token":
            return httpx.Response(200, json={"access_token": "AT-REACTIVATE"})
        if str(request.url) == "https://reactivate.example.com/userinfo":
            return httpx.Response(200, json={"id": subject, "name": "Reactivated User", "phone": phone})
        return httpx.Response(404)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.auth_provider.httpx.AsyncClient", _PatchedAsyncClient)
    transport = httpx.ASGITransport(app=app)
    async with real_client(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/auth/code/exchange",
            json={
                "provider": provider_key,
                "code": "CODE-REACTIVATE",
                "purpose": "h5_agent_chat",
                "channel": "miniprogram",
                "redirect_uri": "https://app.example.com/h5/agents/a1/chat",
            },
        )

    assert response.status_code == 200, response.text
    async with async_session() as db:
        user = await db.get(User, user_id)
        stale_member = await db.get(OrgMember, stale_member_id)
        previous_oauth_member = await db.get(OrgMember, previous_oauth_member_id)
        oauth_member = await db.scalar(
            select(OrgMember).where(
                OrgMember.user_id == user_id,
                OrgMember.external_id == subject,
            )
        )
        assert user is not None and user.is_active is True
        assert stale_member is not None and stale_member.status == stale_status
        assert previous_oauth_member is not None and previous_oauth_member.status == "deleted"
        assert oauth_member is not None and oauth_member.status == "active"
