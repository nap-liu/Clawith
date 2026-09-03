"""Observable login-cookie and workspace-download authentication tests."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.auth import router as auth_router
from app.api.files import router as files_router
from app.core.security import ACCESS_TOKEN_COOKIE_NAME, create_access_token, hash_password
from app.database import async_session, engine, get_db
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.storage import agent_storage_key, get_storage_backend
from app.services import im_markdown_media

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(files_router, prefix="/api")

    async def _db_override():
        async with async_session() as db:
            yield db

    app.dependency_overrides[get_db] = _db_override
    return app


async def _seed_user_and_agent() -> tuple[uuid.UUID, str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Cookie Auth {suffix}", slug=f"cookie-auth-{suffix}")
        identity = Identity(
            email=f"cookie-auth-{suffix}@example.com",
            username=f"cookie-auth-{suffix}",
            password_hash=hash_password("correct-password"),
            email_verified=True,
            is_active=True,
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Cookie Auth User",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Cookie Download Agent",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
            agent_type="native",
            scope="standard",
        )
        db.add(agent)
        await db.commit()
        return user.id, identity.email, agent.id


async def test_login_cookie_authenticates_api_and_logout_clears_it():
    _user_id, email, _agent_id = await _seed_user_and_agent()
    async with AsyncClient(
        transport=ASGITransport(app=_test_app()),
        base_url="http://test",
    ) as client:
        login = await client.post(
            "/auth/login",
            json={"login_identifier": email, "password": "correct-password"},
        )
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"]
        assert f"{ACCESS_TOKEN_COOKIE_NAME}=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Path=/" in set_cookie
        assert "Secure" not in set_cookie

        current = await client.get("/auth/me")
        assert current.status_code == 200
        assert current.json()["email"] == email

        invalid_header = await client.get(
            "/auth/me",
            headers={"Authorization": "Bearer invalid-header-token"},
        )
        assert invalid_header.status_code == 401

        logged_out = await client.post("/auth/logout")
        assert logged_out.status_code == 200
        assert logged_out.json() == {"ok": True}
        assert client.cookies.get(ACCESS_TOKEN_COOKIE_NAME) is None
        assert (await client.get("/auth/me")).status_code == 401


async def test_https_login_marks_same_jwt_cookie_secure():
    _user_id, email, _agent_id = await _seed_user_and_agent()
    async with AsyncClient(
        transport=ASGITransport(app=_test_app()),
        base_url="https://test",
    ) as client:
        login = await client.post(
            "/auth/login",
            json={"login_identifier": email, "password": "correct-password"},
        )
    assert login.status_code == 200
    assert "Secure" in login.headers["set-cookie"]


async def test_existing_bearer_session_is_mirrored_without_relogin():
    user_id, _email, _agent_id = await _seed_user_and_agent()
    token = create_access_token(str(user_id), "member")
    async with AsyncClient(
        transport=ASGITransport(app=_test_app()),
        base_url="http://test",
    ) as client:
        current = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert current.status_code == 200
        assert client.cookies.get(ACCESS_TOKEN_COOKIE_NAME) == token
        assert (await client.get("/auth/me")).status_code == 200


async def test_workspace_download_accepts_cookie_and_keeps_query_token_compatibility():
    user_id, _email, agent_id = await _seed_user_and_agent()
    token = create_access_token(str(user_id), "member")
    storage = get_storage_backend()
    key = agent_storage_key(agent_id, "workspace/report.png")
    payload = b"\x89PNG\r\n\x1a\nobservable-test"
    await storage.write_bytes(key, payload, content_type="image/png")
    app = _test_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/agents/{agent_id}/files/download?path=workspace/report.png&inline=true"
        assert (await client.get(url)).status_code == 401

        client.cookies.set(ACCESS_TOKEN_COOKIE_NAME, token)
        cookie_download = await client.get(url)
        assert cookie_download.status_code == 200
        assert cookie_download.content == payload
        assert cookie_download.headers["content-type"] == "image/png"
        for prefixed_path in ("/workspace/report.png", "./workspace/report.png"):
            normalized_download = await client.get(
                f"/api/agents/{agent_id}/files/download",
                params={"path": prefixed_path, "inline": "true"},
            )
            assert normalized_download.status_code == 200
            assert normalized_download.content == payload
        root_path_is_not_guessed = await client.get(
            f"/api/agents/{agent_id}/files/download",
            params={"path": "report.png", "inline": "true"},
        )
        assert root_path_is_not_guessed.status_code == 404

        rejected_header = await client.get(
            url,
            headers={"Authorization": "Bearer invalid-header-token"},
        )
        assert rejected_header.status_code == 401

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        query_download = await client.get(f"{url}&token={token}")
        assert query_download.status_code == 200
        assert query_download.content == payload

    await storage.delete(key)


async def test_im_image_ticket_serves_local_image_without_login(monkeypatch):
    _user_id, _email, agent_id = await _seed_user_and_agent()
    storage = get_storage_backend()
    key = agent_storage_key(agent_id, "workspace/ticket.png")
    payload = b"\x89PNG\r\n\x1a\nlocal-ticket-test"
    await storage.write_bytes(key, payload, content_type="image/png")
    monkeypatch.setattr(
        im_markdown_media,
        "_public_base_url",
        AsyncMock(return_value="http://test"),
    )
    projected = await im_markdown_media.project_agent_images_for_im(
        agent_id,
        "![ticket](./workspace/ticket.png)",
    )
    signed_url = projected.removeprefix("![ticket](").removesuffix(")")
    parsed = urlsplit(signed_url)

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.get(f"{parsed.path}?{parsed.query}")
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        tampered = await client.get(
            f"{parsed.path}?{parsed.query.replace('workspace%2Fticket.png', 'workspace%2Fother.png')}"
        )
        assert tampered.status_code == 404

    await storage.delete(key)


async def test_workspace_download_rejects_cookie_for_disabled_identity():
    user_id, _email, agent_id = await _seed_user_and_agent()
    async with async_session() as db:
        user = await db.get(User, user_id)
        identity = await db.get(Identity, user.identity_id)
        identity.is_active = False
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()),
        base_url="http://test",
        cookies={ACCESS_TOKEN_COOKIE_NAME: create_access_token(str(user_id), "member")},
    ) as client:
        response = await client.get(
            f"/api/agents/{agent_id}/files/download?path=workspace/report.png&inline=true"
        )
    assert response.status_code == 401
