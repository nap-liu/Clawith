import asyncio
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

from app.api import pages as pages_api
from app.config import get_settings
from app.core.security import create_access_token
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.org import OrgDepartment, OrgMember
from app.models.published_page import (
    PublishedPage,
    PublishedPageAccess,
    PublishedPageAnonymousVisitor,
    PublishedPageVisitor,
)
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.agent_tools import (
    AGENT_TOOLS,
    _list_page_access_requests,
    _list_published_pages,
    _publish_page,
    _search_page_viewers,
    _update_published_page_access,
    get_agent_tools_for_llm,
)
from app.services.published_page_access import PAGE_SESSION_COOKIE, create_page_session
from app.services.tool_seeder import BUILTIN_TOOLS, seed_builtin_tools

pytestmark = pytest.mark.asyncio
settings = get_settings()


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_user(db, tenant_id, name, role="member", is_platform_admin=False):
    identity = Identity(
        username=f"{name}_{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:8]}@test.local",
        password_hash="x",
        is_platform_admin=is_platform_admin,
    )
    db.add(identity)
    await db.flush()
    user = User(identity_id=identity.id, tenant_id=tenant_id, display_name=name, role=role, is_active=True)
    db.add(user)
    await db.flush()
    return user


async def _make_restricted_page():
    async with async_session() as db:
        tenant = Tenant(name="Page Access Test", slug=f"page-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        owner = await _make_user(db, tenant.id, "Owner")
        viewer = await _make_user(db, tenant.id, "Viewer")
        agent = Agent(name="Publisher", role_description="", creator_id=owner.id, tenant_id=tenant.id, agent_type="native")
        db.add(agent)
        await db.flush()
        published_at = datetime.now(timezone.utc)
        page = PublishedPage(
            short_id=f"p{uuid.uuid4().hex[:7]}", agent_id=agent.id, user_id=owner.id, tenant_id=tenant.id,
            last_published_by_user_id=owner.id, last_published_at=published_at,
            source_path="out/protected.html", title="Protected", access_mode="restricted",
        )
        db.add(page)
        await db.commit()
        ids = page.short_id, page.id, agent.id, owner.id, viewer.id
    target = pathlib.Path(settings.AGENT_DATA_DIR) / str(ids[2]) / "out"
    target.mkdir(parents=True, exist_ok=True)
    (target / "protected.html").write_text("<h1>secret</h1>", encoding="utf-8")
    return ids


async def test_protected_page_uses_frontend_access_route_and_returns_after_approval():
    short_id, page_id, _agent_id, _owner_id, viewer_id = await _make_restricted_page()
    token = create_access_token(str(viewer_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert initial.status_code == 302
        assert initial.headers["location"].startswith("/published-page-access?")
        initial_query = parse_qs(urlparse(initial.headers["location"]).query)
        assert "auto_login" not in initial_query

        selected_only = await client.get(f"/p/{short_id}?sso=dingtalk", follow_redirects=False)
        selected_only_query = parse_qs(urlparse(selected_only.headers["location"]).query)
        assert "auto_login" not in selected_only_query
        assert "sso" not in selected_only_query

        automatic = await client.get(
            f"/p/{short_id}?auto_login=1&sso=dingtalk", follow_redirects=False
        )
        automatic_query = parse_qs(urlparse(automatic.headers["location"]).query)
        assert automatic_query["auto_login"] == ["1"]
        assert automatic_query["sso"] == ["dingtalk"]

        bridge = await client.post(
            "/api/pages/session", json={"short_id": short_id}, headers={"Authorization": f"Bearer {token}"},
        )
        assert bridge.status_code == 200
        assert bridge.json()["allowed"] is False

        denied = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert denied.status_code == 302
        assert "denied=1" in denied.headers["location"]

        async with async_session() as db:
            db.add(PublishedPageAccess(page_id=page_id, user_id=viewer_id, status="approved"))
            await db.commit()

        allowed = await client.get(f"/p/{short_id}")
        assert allowed.status_code == 200
        assert allowed.headers["x-accel-redirect"] == "/__published_page_viewer"
        assert allowed.headers["cache-control"] == "no-store"

        context = await client.get(f"/api/pages/{short_id}/viewer-context")
        assert context.status_code == 200
        assert context.json()["watermark_identity"]["display_name"] == "Viewer"
        assert context.json()["watermark_text"] is None
        assert context.json()["allow_top_navigation"] is True

        content = await client.get(f"/api/pages/{short_id}/content")
        assert content.status_code == 200
        assert content.headers["content-type"] == "text/html"
        assert content.headers["cache-control"] == "no-store"
        assert "content-security-policy" not in content.headers
        assert "x-frame-options" not in content.headers
        assert "x-content-type-options" not in content.headers
        assert content.text == "<h1>secret</h1>"

        cleared = await client.delete("/api/pages/session")
        assert cleared.status_code == 200
        after_logout = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert after_logout.status_code == 302
        assert after_logout.headers["location"].startswith("/published-page-access?")

    async with async_session() as db:
        visitor = await db.scalar(select(PublishedPageVisitor).where(
            PublishedPageVisitor.page_id == page_id, PublishedPageVisitor.user_id == viewer_id
        ))
        assert visitor is not None
        assert visitor.view_count == 1
        page = await db.get(PublishedPage, page_id)
        assert page.last_published_by_user_id == _owner_id
        assert page.last_published_at is not None


async def test_next_request_rechecks_access_mode_approval_user_and_session_state():
    short_id, page_id, _agent_id, _owner_id, viewer_id = await _make_restricted_page()
    transport = httpx.ASGITransport(app=app)
    async with async_session() as db:
        access = PublishedPageAccess(page_id=page_id, user_id=viewer_id, status="approved")
        db.add(access)
        await db.commit()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(PAGE_SESSION_COOKIE, create_page_session(viewer_id), path="/")
        assert (await client.get(f"/p/{short_id}")).status_code == 200
        context = await client.get(f"/api/pages/{short_id}/viewer-context")
        assert context.status_code == 200
        assert context.json()["allow_top_navigation"] is True

        async with async_session() as db:
            page = await db.get(PublishedPage, page_id)
            access = await db.scalar(select(PublishedPageAccess).where(
                PublishedPageAccess.page_id == page_id,
                PublishedPageAccess.user_id == viewer_id,
            ))
            page.access_mode = "authenticated"
            access.status = "rejected"
            await db.commit()
        assert (await client.get(f"/api/pages/{short_id}/content")).status_code == 200
        assert (await client.get(f"/api/pages/{short_id}/viewer-context")).status_code == 200

        async with async_session() as db:
            page = await db.get(PublishedPage, page_id)
            page.access_mode = "restricted"
            await db.commit()
        denied_page = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert denied_page.status_code == 302
        assert "denied=1" in denied_page.headers["location"]
        assert (await client.get(f"/api/pages/{short_id}/content")).status_code == 403
        assert (await client.get(f"/api/pages/{short_id}/viewer-context")).status_code == 403

        async with async_session() as db:
            access = await db.scalar(select(PublishedPageAccess).where(
                PublishedPageAccess.page_id == page_id,
                PublishedPageAccess.user_id == viewer_id,
            ))
            access.status = "approved"
            await db.commit()
        assert (await client.get(f"/p/{short_id}")).status_code == 200
        assert (await client.get(f"/api/pages/{short_id}/viewer-context")).status_code == 200

        async with async_session() as db:
            viewer = await db.get(User, viewer_id)
            viewer.is_active = False
            await db.commit()
        inactive_page = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert inactive_page.status_code == 302
        assert "denied=1" not in inactive_page.headers["location"]
        assert (await client.get(f"/api/pages/{short_id}/content")).status_code == 401
        assert (await client.get(f"/api/pages/{short_id}/viewer-context")).status_code == 401

    async with async_session() as db:
        viewer = await db.get(User, viewer_id)
        viewer.is_active = True
        await db.commit()

    expired_session = jwt.encode(
        {
            "sub": str(viewer_id),
            "typ": "published_page_session",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    for invalid_session in ("forged.page.session", expired_session):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as invalid_client:
            invalid_client.cookies.set(PAGE_SESSION_COOKIE, invalid_session, path="/")
            response = await invalid_client.get(f"/api/pages/{short_id}/content")
            assert response.status_code == 401
            context = await invalid_client.get(f"/api/pages/{short_id}/viewer-context")
            assert context.status_code == 401


async def test_forwarded_https_is_preserved_for_return_url_and_page_session_cookie():
    short_id, _page_id, _agent_id, _owner_id, viewer_id = await _make_restricted_page()
    token = create_access_token(str(viewer_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        redirect = await client.get(
            f"/p/{short_id}",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
        query = parse_qs(urlparse(redirect.headers["location"]).query)
        assert query["return_to"] == [f"https://test/p/{short_id}"]

        bridge = await client.post(
            "/api/pages/session",
            json={"short_id": short_id},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Forwarded-Proto": "https",
            },
        )
        assert bridge.status_code == 200
        assert "Secure" in bridge.headers["set-cookie"]

        invalid = await client.get(
            f"/p/{short_id}",
            headers={"X-Forwarded-Proto": "javascript"},
            follow_redirects=False,
        )
        invalid_query = parse_qs(urlparse(invalid.headers["location"]).query)
        assert invalid_query["return_to"] == [f"http://test/p/{short_id}"]


async def test_access_request_is_idempotent_and_notifies_publisher():
    short_id, page_id, _agent_id, owner_id, viewer_id = await _make_restricted_page()
    token = create_access_token(str(viewer_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(2):
            response = await client.post(
                f"/api/pages/{short_id}/request-access", headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "pending"

    async with async_session() as db:
        requests = (await db.scalars(select(PublishedPageAccess).where(
            PublishedPageAccess.page_id == page_id, PublishedPageAccess.user_id == viewer_id
        ))).all()
        assert len(requests) == 1
        from app.models.notification import Notification
        notifications = (await db.scalars(select(Notification).where(
            Notification.user_id == owner_id, Notification.ref_id == page_id, Notification.type == "page_access_pending"
        ))).all()
        assert len(notifications) == 1


async def test_public_and_authenticated_modes_keep_expected_access_boundaries():
    short_id, page_id, _agent_id, _owner_id, viewer_id = await _make_restricted_page()
    transport = httpx.ASGITransport(app=app)

    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        await db.commit()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        public = await client.get(f"/p/{short_id}")
        assert public.status_code == 200
        assert public.headers["x-accel-redirect"] == "/__published_page_viewer"
        public_context = await client.get(f"/api/pages/{short_id}/viewer-context")
        assert public_context.status_code == 200
        assert public_context.json()["watermark_identity"] is None
        assert public_context.json()["watermark_text"].startswith("匿名访客 ")
        assert public_context.json()["watermark_text"].endswith(" UTC")
        content = await client.get(f"/api/pages/{short_id}/content")
        assert content.status_code == 200
        assert content.text == "<h1>secret</h1>"

    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "authenticated"
        await db.commit()
    token = create_access_token(str(viewer_id), "member")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert unauthenticated.status_code == 302
        unauthenticated_embed = await client.get(
            f"/p/{short_id}?__report_embed=1",
            follow_redirects=False,
        )
        assert unauthenticated_embed.status_code == 302
        assert unauthenticated_embed.headers["location"].startswith("/published-page-access?")
        unauthenticated_content = await client.get(f"/api/pages/{short_id}/content")
        assert unauthenticated_content.status_code == 401
        bridge = await client.post(
            "/api/pages/session", json={"short_id": short_id}, headers={"Authorization": f"Bearer {token}"},
        )
        assert bridge.status_code == 200
        assert bridge.json()["allowed"] is True
        authenticated = await client.get(f"/p/{short_id}")
        assert authenticated.status_code == 200
        assert authenticated.headers["x-accel-redirect"] == "/__published_page_viewer"
        authenticated_context = await client.get(f"/api/pages/{short_id}/viewer-context")
        assert authenticated_context.status_code == 200
        assert authenticated_context.json()["watermark_identity"]["display_name"] == "Viewer"
        assert authenticated_context.json()["watermark_text"] is None
        content = await client.get(f"/api/pages/{short_id}/content")
        assert content.status_code == 200
        assert content.text == "<h1>secret</h1>"


async def test_legacy_viewer_context_is_unrestricted_and_does_not_count_a_view():
    short_id, page_id, _agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/p/{short_id}")
        context = await client.get(f"/api/pages/{short_id}/viewer-context")

    assert response.status_code == 200
    assert response.headers["x-accel-redirect"] == "/__published_page_viewer"
    assert context.status_code == 200
    assert context.headers["cache-control"] == "no-store"
    assert context.json()["title"] == "Protected"
    assert context.json()["access_mode"] == "public"
    assert context.json()["watermark_identity"] is None
    assert context.json()["watermark_text"].startswith("匿名访客 ")
    assert context.json()["watermark_text"].endswith(" UTC")
    assert context.json()["allow_top_navigation"] is True
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        assert page.view_count == 0


async def test_report_owned_meta_csp_and_dom_are_returned_unchanged():
    short_id, page_id, agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    source = pathlib.Path(settings.AGENT_DATA_DIR) / str(agent_id) / "out" / "protected.html"
    source.write_text(
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'none\'">'
        '<main id="published-page-watermark-host">report remains intact</main>'
        '<!-- legacy reference must not grant top navigation: /sdk/clawith.js -->',
        encoding="utf-8",
    )
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        viewer = await client.get(f"/p/{short_id}")
        headerless_embed = await client.get(
            f"/p/{short_id}?__report_embed=1", follow_redirects=False,
        )
        metadata_embed = await client.get(
            f"/p/{short_id}?__report_embed=1",
        )
        content = await client.get(f"/api/pages/{short_id}/content")

    assert viewer.status_code == 200
    assert viewer.headers["x-accel-redirect"] == "/__published_page_viewer"
    assert headerless_embed.status_code == 200
    assert "report remains intact" in headerless_embed.text
    assert metadata_embed.status_code == 200
    assert "report remains intact" in metadata_embed.text
    assert "/sdk/clawith.js" in metadata_embed.text
    assert content.status_code == 200
    assert "content-security-policy" not in content.headers
    assert "x-frame-options" not in content.headers
    assert "default-src 'none'" in content.text
    assert 'id="published-page-watermark-host"' in content.text
    assert "report remains intact" in content.text


async def test_missing_page_and_source_use_the_frontend_unavailable_page():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/p/missing-page", follow_redirects=False)
        assert missing.status_code == 302
        assert missing.headers["location"] == "/published-page-unavailable"

    short_id, _page_id, agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    source = pathlib.Path(settings.AGENT_DATA_DIR) / str(agent_id) / "out" / "protected.html"
    source.unlink()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unavailable = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert unavailable.status_code == 302
        assert unavailable.headers["location"] == "/published-page-unavailable"
        viewer_token = create_access_token(str(_viewer_id), "member")
        bridge = await client.post(
            "/api/pages/session",
            json={"short_id": short_id},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert bridge.status_code == 404


async def test_public_anonymous_visits_are_aggregated_by_platform_cookie():
    short_id, page_id, _agent_id, owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as first_browser:
        first = await first_browser.get(f"/p/{short_id}")
        assert first.status_code == 200
        cookie_header = first.headers["set-cookie"]
        assert "published_page_visitor=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "Path=/" in cookie_header
        first_content = await first_browser.get(f"/p/{short_id}?__report_embed=1")
        assert first_content.status_code == 200
        second = await first_browser.get(f"/p/{short_id}?__report_embed=1")
        assert second.status_code == 200
        assert "published_page_visitor=" not in second.headers.get("set-cookie", "")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as second_browser:
        shell = await second_browser.get(f"/p/{short_id}")
        assert shell.status_code == 200
        third = await second_browser.get(f"/p/{short_id}?__report_embed=1")
        assert third.status_code == 200

    async with async_session() as db:
        anonymous_visitors = (await db.scalars(
            select(PublishedPageAnonymousVisitor).where(PublishedPageAnonymousVisitor.page_id == page_id)
        )).all()
        assert len(anonymous_visitors) == 2
        assert sorted(visitor.view_count for visitor in anonymous_visitors) == [1, 2]
        assert all(visitor.first_viewed_at is not None and visitor.last_viewed_at is not None for visitor in anonymous_visitors)
        page = await db.get(PublishedPage, page_id)
        assert page.view_count == 3

    owner_token = create_access_token(str(owner_id), "member")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as owner_client:
        visitors = await owner_client.get(
            f"/api/pages/{page_id}/visitors?page=1&page_size=20",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert visitors.status_code == 200
        payload = visitors.json()
        assert payload["total"] == 2
        assert all(item["visitor_type"] == "anonymous" for item in payload["items"])
        assert sorted(item["view_count"] for item in payload["items"]) == [1, 2]


async def test_public_embed_stays_anonymous_with_existing_page_session():
    short_id, page_id, _agent_id, _owner_id, viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(PAGE_SESSION_COOKIE, create_page_session(viewer_id), path="/")
        embedded = await client.get(
            f"/p/{short_id}?__report_embed=1",
        )
        assert embedded.status_code == 200

    async with async_session() as db:
        authenticated_visit = await db.scalar(
            select(PublishedPageVisitor).where(
                PublishedPageVisitor.page_id == page_id,
                PublishedPageVisitor.user_id == viewer_id,
            )
        )
        anonymous_visits = (
            await db.scalars(
                select(PublishedPageAnonymousVisitor).where(
                    PublishedPageAnonymousVisitor.page_id == page_id
                )
            )
        ).all()
        assert authenticated_visit is None
        assert len(anonymous_visits) == 1
        assert anonymous_visits[0].view_count == 1


async def test_anonymous_visitor_key_is_isolated_per_page():
    visitor_id = uuid.uuid4()

    class CookieRequest:
        cookies = {pages_api.PUBLIC_VISITOR_COOKIE: str(visitor_id)}

    first_key, first_cookie = pages_api._anonymous_visitor(CookieRequest(), uuid.uuid4())
    second_key, second_cookie = pages_api._anonymous_visitor(CookieRequest(), uuid.uuid4())

    assert first_cookie is None
    assert second_cookie is None
    assert first_key != second_key


async def test_anonymous_visitor_rows_are_capped_and_overflow_is_aggregated(monkeypatch):
    _short_id, page_id, _agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    monkeypatch.setattr(pages_api, "MAX_ANONYMOUS_VISITORS_PER_PAGE", 1)

    async with async_session() as db:
        await pages_api._record_anonymous_view(db, page_id, "1" * 64)
        await pages_api._record_anonymous_view(db, page_id, "2" * 64)
        await pages_api._record_anonymous_view(db, page_id, "3" * 64)
        await db.commit()

        visitors = (await db.scalars(
            select(PublishedPageAnonymousVisitor).where(PublishedPageAnonymousVisitor.page_id == page_id)
        )).all()

    assert len(visitors) == 2
    assert {visitor.visitor_key for visitor in visitors} == {
        "1" * 64,
        pages_api.ANONYMOUS_VISITOR_OVERFLOW_KEY,
    }
    overflow = next(
        visitor for visitor in visitors
        if visitor.visitor_key == pages_api.ANONYMOUS_VISITOR_OVERFLOW_KEY
    )
    assert overflow.view_count == 2


async def test_public_visit_from_another_tenant_is_recorded_anonymously():
    short_id, page_id, _agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        other_tenant = Tenant(name="Public Visitor Tenant", slug=f"visitor-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(other_tenant)
        await db.flush()
        outsider = await _make_user(db, other_tenant.id, "PublicOutsider")
        await db.commit()
        outsider_id = outsider.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(PAGE_SESSION_COOKIE, create_page_session(outsider_id), path="/")
        response = await client.get(f"/p/{short_id}")
        assert response.status_code == 200
        embedded = await client.get(f"/p/{short_id}?__report_embed=1")
        assert embedded.status_code == 200

    async with async_session() as db:
        visitors = (await db.scalars(select(PublishedPageAnonymousVisitor).where(
            PublishedPageAnonymousVisitor.page_id == page_id
        ))).all()
        assert len(visitors) == 1
        assert visitors[0].view_count == 1


async def test_public_page_keeps_same_tenant_session_anonymous():
    short_id, page_id, _agent_id, owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "public"
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(PAGE_SESSION_COOKIE, create_page_session(owner_id), path="/")
        response = await client.get(f"/p/{short_id}")
        assert response.status_code == 200
        embedded = await client.get(f"/p/{short_id}?__report_embed=1")
        assert embedded.status_code == 200

    async with async_session() as db:
        anonymous = (await db.scalars(select(PublishedPageAnonymousVisitor).where(
            PublishedPageAnonymousVisitor.page_id == page_id
        ))).all()
        authenticated = (await db.scalars(select(PublishedPageVisitor).where(
            PublishedPageVisitor.page_id == page_id
        ))).all()
        assert len(anonymous) == 1
        assert authenticated == []


async def test_legacy_page_without_tenant_remains_visible_and_is_repaired_on_access_update():
    _short_id, page_id, _agent_id, owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        original_tenant_id = page.tenant_id
        page.tenant_id = None
        await db.commit()

    token = create_access_token(str(owner_id), "member")
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/pages/mine", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == str(page_id) for item in listed.json()["items"])

        updated = await client.put(
            f"/api/pages/{page_id}/access",
            json={"access_mode": "authenticated", "allowed_user_ids": []},
            headers=headers,
        )
        assert updated.status_code == 200

    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        assert page.tenant_id == original_tenant_id
        assert page.access_mode == "authenticated"


async def test_cross_tenant_user_cannot_create_page_session():
    short_id, _page_id, _agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        other_tenant = Tenant(name="Other Page Tenant", slug=f"other-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(other_tenant)
        await db.flush()
        outsider = await _make_user(db, other_tenant.id, "Outsider")
        await db.commit()
        outsider_id = outsider.id

    token = create_access_token(str(outsider_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/pages/session", json={"short_id": short_id}, headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403


async def test_restricted_page_allows_same_tenant_admins_but_rejects_cross_tenant_admin():
    short_id, page_id, _agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        org_admin = await _make_user(db, page.tenant_id, "PageOrgAdmin", role="org_admin")
        platform_admin = await _make_user(
            db,
            page.tenant_id,
            "PagePlatformAdmin",
            is_platform_admin=True,
        )
        other_tenant = Tenant(
            name="Other Admin Tenant",
            slug=f"other-admin-{uuid.uuid4().hex[:8]}",
            im_provider="web_only",
        )
        db.add(other_tenant)
        await db.flush()
        cross_tenant_admin = await _make_user(
            db,
            other_tenant.id,
            "CrossTenantPlatformAdmin",
            is_platform_admin=True,
        )
        await db.commit()
        same_tenant_admin_ids = (org_admin.id, platform_admin.id)
        cross_tenant_admin_id = cross_tenant_admin.id

    transport = httpx.ASGITransport(app=app)
    for admin_id in same_tenant_admin_ids:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.cookies.set(PAGE_SESSION_COOKIE, create_page_session(admin_id), path="/")
            assert (await client.get(f"/p/{short_id}")).status_code == 200
            assert (await client.get(f"/api/pages/{short_id}/content")).status_code == 200

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(PAGE_SESSION_COOKIE, create_page_session(cross_tenant_admin_id), path="/")
        denied_page = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert denied_page.status_code == 302
        assert "denied=1" in denied_page.headers["location"]
        assert (await client.get(f"/api/pages/{short_id}/content")).status_code == 403


async def test_only_page_managers_can_update_access_and_resolve_requests():
    short_id, page_id, _agent_id, owner_id, viewer_id = await _make_restricted_page()
    async with async_session() as db:
        original_page = await db.get(PublishedPage, page_id)
        original_last_publisher = original_page.last_published_by_user_id
        original_last_published_at = original_page.last_published_at
    owner_token = create_access_token(str(owner_id), "member")
    viewer_token = create_access_token(str(viewer_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.put(
            f"/api/pages/{page_id}/access",
            json={"access_mode": "public", "allowed_user_ids": []},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert denied.status_code == 404

        first_update = await client.put(
            f"/api/pages/{page_id}/access",
            json={"access_mode": "authenticated", "allowed_user_ids": []},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert first_update.status_code == 200
        assert first_update.json()["access_mode"] == "authenticated"

        restore_restricted = await client.put(
            f"/api/pages/{page_id}/access",
            json={"access_mode": "restricted", "allowed_user_ids": []},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert restore_restricted.status_code == 200

        requested = await client.post(
            f"/api/pages/{short_id}/request-access", headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert requested.status_code == 200
        approved = await client.put(
            f"/api/pages/{page_id}/requests/{viewer_id}",
            json={"status": "approved"}, headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert approved.status_code == 200

        bridge = await client.post(
            "/api/pages/session", json={"short_id": short_id},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert bridge.status_code == 200
        assert bridge.json()["allowed"] is True

    async with async_session() as db:
        unchanged_page = await db.get(PublishedPage, page_id)
        assert unchanged_page.last_published_by_user_id == original_last_publisher
        assert unchanged_page.last_published_at == original_last_published_at
