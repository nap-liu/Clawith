import pathlib
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
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
from app.models.user import Identity, User
from app.services.agent_tools import (
    AGENT_TOOLS,
    _list_page_access_requests,
    _list_published_pages,
    _publish_page,
    _search_page_viewers,
    _update_published_page_access,
)
from app.services.published_page_access import PAGE_SESSION_COOKIE, create_page_session
from app.services.tool_seeder import BUILTIN_TOOLS


pytestmark = pytest.mark.asyncio
settings = get_settings()


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_user(db, tenant_id, name):
    identity = Identity(username=f"{name}_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
    db.add(identity)
    await db.flush()
    user = User(identity_id=identity.id, tenant_id=tenant_id, display_name=name, role="member", is_active=True)
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
        assert "secret" not in allowed.text

        context = await client.get(f"/api/pages/{short_id}/viewer-context")
        assert context.status_code == 200
        assert context.json()["watermark_identity"]["display_name"] == "Viewer"
        assert context.json()["allow_top_navigation"] is False
        content = await client.get(f"/api/pages/{short_id}/content")
        assert content.status_code == 200
        assert content.headers["content-type"].startswith("text/html")
        assert content.headers["cache-control"] == "no-store"
        assert content.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert "secret" in content.text

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
        assert public.text == "<h1>secret</h1>"
        assert "x-accel-redirect" not in public.headers

    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.access_mode = "authenticated"
        await db.commit()
    token = create_access_token(str(viewer_id), "member")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert unauthenticated.status_code == 302
        bridge = await client.post(
            "/api/pages/session", json={"short_id": short_id}, headers={"Authorization": f"Bearer {token}"},
        )
        assert bridge.status_code == 200
        assert bridge.json()["allowed"] is True
        authenticated = await client.get(f"/p/{short_id}")
        assert authenticated.status_code == 200
        assert authenticated.headers["x-accel-redirect"] == "/__published_page_viewer"
        content = await client.get(f"/api/pages/{short_id}/content")
        assert content.status_code == 200
        assert content.text == "<h1>secret</h1>"


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
        assert first.text == "<h1>secret</h1>"
        cookie_header = first.headers["set-cookie"]
        assert "published_page_visitor=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "Path=/p/" in cookie_header
        second = await first_browser.get(f"/p/{short_id}")
        assert second.status_code == 200
        assert "published_page_visitor=" not in second.headers.get("set-cookie", "")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as second_browser:
        third = await second_browser.get(f"/p/{short_id}")
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

    async with async_session() as db:
        visitors = (await db.scalars(select(PublishedPageAnonymousVisitor).where(
            PublishedPageAnonymousVisitor.page_id == page_id
        ))).all()
        assert len(visitors) == 1
        assert visitors[0].view_count == 1


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


async def test_page_manager_can_delete_published_address_without_deleting_source_file():
    short_id, page_id, agent_id, owner_id, viewer_id = await _make_restricted_page()
    source = pathlib.Path(settings.AGENT_DATA_DIR) / str(agent_id) / "out/protected.html"
    async with async_session() as db:
        db.add(PublishedPageAccess(page_id=page_id, user_id=viewer_id, status="approved"))
        db.add(PublishedPageVisitor(page_id=page_id, user_id=viewer_id, view_count=2))
        db.add(PublishedPageAnonymousVisitor(page_id=page_id, visitor_key="a" * 64, view_count=3))
        await db.commit()

    owner_token = create_access_token(str(owner_id), "member")
    viewer_token = create_access_token(str(viewer_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.delete(
            f"/api/pages/{page_id}", headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert denied.status_code == 404

        deleted = await client.delete(
            f"/api/pages/{page_id}", headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True}

        unavailable = await client.get(f"/p/{short_id}", follow_redirects=False)
        assert unavailable.status_code == 302
        assert unavailable.headers["location"] == "/published-page-unavailable"

    async with async_session() as db:
        assert await db.get(PublishedPage, page_id) is None
        assert await db.scalar(select(PublishedPageAccess).where(PublishedPageAccess.page_id == page_id)) is None
        assert await db.scalar(select(PublishedPageVisitor).where(PublishedPageVisitor.page_id == page_id)) is None
        assert await db.scalar(select(PublishedPageAnonymousVisitor).where(PublishedPageAnonymousVisitor.page_id == page_id)) is None
    assert source.exists()
    assert source.read_text(encoding="utf-8") == "<h1>secret</h1>"


async def test_shared_agent_user_cannot_change_page_access_through_tools():
    short_id, page_id, agent_id, owner_id, viewer_id = await _make_restricted_page()
    denied = await _update_published_page_access(
        agent_id, viewer_id,
        {"short_id": short_id, "access_mode": "public", "allowed_user_ids": []},
    )
    assert denied.startswith("Permission denied")
    search_denied = await _search_page_viewers(agent_id, viewer_id, {"query": "Owner"})
    assert search_denied.startswith("Permission denied")

    allowed = await _update_published_page_access(
        agent_id, owner_id,
        {"short_id": short_id, "access_mode": "authenticated", "allowed_user_ids": []},
    )
    assert allowed.startswith("Updated")
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        assert page.access_mode == "authenticated"


async def test_page_list_returns_summary_and_detail_is_loaded_separately():
    _short_id, page_id, _agent_id, owner_id, viewer_id = await _make_restricted_page()
    async with async_session() as db:
        db.add(PublishedPageVisitor(page_id=page_id, user_id=viewer_id, view_count=3))
        await db.commit()
    token = create_access_token(str(owner_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pages = await client.get("/api/pages/mine", headers={"Authorization": f"Bearer {token}"})
        assert pages.status_code == 200
        payload = pages.json()
        assert payload["page"] == 1
        assert payload["page_size"] == 20
        assert payload["total"] >= 1
        summary = next(item for item in payload["items"] if item["id"] == str(page_id))
        assert "access_users" not in summary
        assert "visitor_count" in summary
        assert summary["pending_request_count"] == 0
        assert summary["created_by"]["display_name"] == "Owner"
        assert summary["last_published_by"]["display_name"] == "Owner"
        assert summary["last_published_at"] is not None

        detail = await client.get(
            f"/api/pages/{page_id}/detail", headers={"Authorization": f"Bearer {token}"},
        )
        assert detail.status_code == 200
        assert "access_users" in detail.json()
        assert "visitors" not in detail.json()
        assert detail.json()["visitor_count"] == 1
        assert detail.json()["created_by"]["id"] == str(owner_id)
        assert detail.json()["last_published_by"]["id"] == str(owner_id)
        assert detail.json()["last_published_at"] is not None

        visitors = await client.get(
            f"/api/pages/{page_id}/visitors?page=1&page_size=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert visitors.status_code == 200
        assert visitors.json()["total"] == 1
        assert visitors.json()["items"][0]["id"] == str(viewer_id)
        assert visitors.json()["items"][0]["view_count"] == 3


async def test_historical_page_reports_unrecorded_last_publication():
    _short_id, page_id, agent_id, owner_id, _viewer_id = await _make_restricted_page()
    async with async_session() as db:
        page = await db.get(PublishedPage, page_id)
        page.last_published_by_user_id = None
        page.last_published_at = None
        await db.commit()

    token = create_access_token(str(owner_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pages = await client.get("/api/pages/mine", headers={"Authorization": f"Bearer {token}"})
        summary = next(item for item in pages.json()["items"] if item["id"] == str(page_id))
        assert summary["created_by"]["id"] == str(owner_id)
        assert summary["last_published_by"] is None
        assert summary["last_published_at"] is None

    agent_view = await _list_published_pages(agent_id)
    assert "Last published by: historical data not recorded" in agent_view
    assert "Last published at: historical data not recorded" in agent_view


async def test_page_list_filters_multiple_agents_and_fuzzy_searches_title_or_path():
    _short_id, page_id, agent_id, owner_id, _viewer_id = await _make_restricted_page()
    marker = uuid.uuid4().hex[:10]
    agent_term = f"agent-{marker}"
    title_term = f"title-{marker}"
    path_term = f"path-{marker}"
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        page = await db.get(PublishedPage, page_id)
        agent.name = f"Search {agent_term}"
        page.title = f"Search {title_term}"
        page.source_path = f"reports/{path_term}.html"
        second_agent = Agent(
            name=f"Second {marker}", role_description="", creator_id=owner_id,
            tenant_id=page.tenant_id, agent_type="native",
        )
        db.add(second_agent)
        await db.flush()
        second_page = PublishedPage(
            short_id=f"p{uuid.uuid4().hex[:7]}", agent_id=second_agent.id, user_id=owner_id,
            tenant_id=page.tenant_id, source_path="reports/second.html", title="Second page",
            access_mode="authenticated",
        )
        db.add(second_page)
        await db.commit()
        second_agent_id, second_page_id = second_agent.id, second_page.id

    token = create_access_token(str(owner_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for term in (title_term.upper(), path_term):
            response = await client.get(
                "/api/pages/mine",
                params={"q": term},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert any(item["id"] == str(page_id) for item in response.json()["items"])

        agent_name_is_not_part_of_fuzzy_search = await client.get(
            "/api/pages/mine",
            params={"q": agent_term},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert agent_name_is_not_part_of_fuzzy_search.status_code == 200
        assert agent_name_is_not_part_of_fuzzy_search.json()["total"] == 0

        single_agent = await client.get(
            "/api/pages/mine",
            params={"agent_ids": str(agent_id)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert single_agent.status_code == 200
        assert {item["id"] for item in single_agent.json()["items"]} == {str(page_id)}

        agent_options = await client.get(
            "/api/pages/agent-options",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert agent_options.status_code == 200
        assert {option["id"] for option in agent_options.json()} == {
            str(agent_id), str(second_agent_id),
        }

        legacy_single_agent = await client.get(
            "/api/pages/mine",
            params={"agent_id": str(agent_id)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert legacy_single_agent.status_code == 200
        assert {item["id"] for item in legacy_single_agent.json()["items"]} == {str(page_id)}

        multiple_agents = await client.get(
            "/api/pages/mine",
            params=[("agent_ids", str(agent_id)), ("agent_ids", str(second_agent_id))],
            headers={"Authorization": f"Bearer {token}"},
        )
        assert multiple_agents.status_code == 200
        assert {item["id"] for item in multiple_agents.json()["items"]} == {
            str(page_id), str(second_page_id),
        }


async def test_publish_tool_defaults_new_pages_to_authenticated_and_preserves_existing_access():
    async with async_session() as db:
        tenant = Tenant(name="Publish Default Test", slug=f"publish-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        owner = await _make_user(db, tenant.id, "Default Owner")
        agent = Agent(name="Default Publisher", role_description="", creator_id=owner.id, tenant_id=tenant.id, agent_type="native")
        db.add(agent)
        await db.commit()
        agent_id, owner_id = agent.id, owner.id

    source_path = f"workspace/default-{uuid.uuid4().hex[:8]}.html"
    source = pathlib.Path(settings.AGENT_DATA_DIR) / str(agent_id) / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<title>Default access</title><p>hello</p>", encoding="utf-8")

    first = await _publish_page(agent_id, owner_id, source.parent, {"path": source_path})
    assert "Access: authenticated." in first
    assert "Platform watermark: enabled automatically." in first
    assert "Page URL:" in first and "Management URL:" in first
    assert "Published by: Default Owner" in first
    assert "Published at:" in first
    async with async_session() as db:
        published = await db.scalar(select(PublishedPage).where(
            PublishedPage.agent_id == agent_id,
            PublishedPage.source_path == source_path,
        ))
        assert published is not None
        assert published.access_mode == "authenticated"
        assert published.user_id == owner_id
        assert published.last_published_by_user_id == owner_id
        assert published.last_published_at is not None
        published.access_mode = "public"
        await db.commit()

    second = await _publish_page(agent_id, owner_id, source.parent, {"path": source_path})
    assert "Access: public." in second
    async with async_session() as db:
        published = await db.scalar(select(PublishedPage).where(
            PublishedPage.agent_id == agent_id,
            PublishedPage.source_path == source_path,
        ))
        assert published.access_mode == "public"


async def test_republish_preserves_creator_and_records_latest_user():
    async with async_session() as db:
        tenant = Tenant(name="Publish Actor Test", slug=f"actor-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        creator = await _make_user(db, tenant.id, "Creator")
        updater = await _make_user(db, tenant.id, "Updater")
        agent = Agent(
            name="Actor Publisher", role_description="", creator_id=creator.id,
            tenant_id=tenant.id, agent_type="native",
        )
        db.add(agent)
        await db.commit()
        agent_id, creator_id, updater_id = agent.id, creator.id, updater.id

    source_path = f"workspace/actor-{uuid.uuid4().hex[:8]}.html"
    source = pathlib.Path(settings.AGENT_DATA_DIR) / str(agent_id) / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<title>Actor attribution</title><p>first</p>", encoding="utf-8")

    await _publish_page(agent_id, creator_id, source.parent, {"path": source_path})
    async with async_session() as db:
        first_publication = await db.scalar(select(PublishedPage).where(
            PublishedPage.agent_id == agent_id,
            PublishedPage.source_path == source_path,
        ))
        first_published_at = first_publication.last_published_at
        assert first_published_at is not None
    source.write_text("<title>Actor attribution</title><p>second</p>", encoding="utf-8")
    result = await _publish_page(agent_id, updater_id, source.parent, {"path": source_path})
    assert result.startswith("Updated in place")

    async with async_session() as db:
        page = await db.scalar(select(PublishedPage).where(
            PublishedPage.agent_id == agent_id,
            PublishedPage.source_path == source_path,
        ))
        assert page is not None
        assert page.user_id == creator_id
        assert page.last_published_by_user_id == updater_id
        assert page.last_published_at is not None
        assert page.last_published_at > first_published_at
        page_id = page.id

    token = create_access_token(str(creator_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pages = await client.get("/api/pages/mine", headers={"Authorization": f"Bearer {token}"})
        summary = next(item for item in pages.json()["items"] if item["id"] == str(page_id))
        assert summary["created_by"]["id"] == str(creator_id)
        assert summary["last_published_by"]["id"] == str(updater_id)
        assert summary["last_published_at"] is not None

        detail = await client.get(
            f"/api/pages/{page_id}/detail",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert detail.json()["created_by"]["id"] == str(creator_id)
        assert detail.json()["last_published_by"]["id"] == str(updater_id)
        assert detail.json()["last_published_at"] == summary["last_published_at"]

    agent_view = await _list_published_pages(agent_id)
    assert "Created by: Creator" in agent_view
    assert "Last published by: Updater" in agent_view
    assert f"Last published at: {summary['last_published_at']}" in agent_view


async def test_agent_can_list_real_access_request_statuses_with_pagination():
    short_id, page_id, agent_id, owner_id, viewer_id = await _make_restricted_page()
    async with async_session() as db:
        request_row = PublishedPageAccess(
            page_id=page_id,
            user_id=viewer_id,
            status="pending",
            requested_at=datetime.now(timezone.utc),
        )
        db.add(request_row)
        direct_user = await _make_user(db, (await db.get(PublishedPage, page_id)).tenant_id, "Direct grant")
        db.add(PublishedPageAccess(page_id=page_id, user_id=direct_user.id, status="approved"))
        await db.commit()

    output = await _list_page_access_requests(agent_id, owner_id, {
        "short_id": short_id,
        "status": "all",
        "page": 1,
        "page_size": 1,
    })
    assert "showing 1 of 1" in output
    assert "Pending requests: 1" in output
    assert "Status: pending" in output
    assert "Direct grant" not in output
    assert "Management URL:" in output

    denied = await _list_page_access_requests(agent_id, viewer_id, {"short_id": short_id})
    assert denied.startswith("Permission denied")
    page_list = await _list_published_pages(agent_id)
    assert "Pending access requests: 1" in page_list


async def test_publish_tool_contract_is_self_contained_and_consistent():
    runtime_publish = next(item["function"] for item in AGENT_TOOLS if item["function"]["name"] == "publish_page")
    seeded_publish = next(item for item in BUILTIN_TOOLS if item["name"] == "publish_page")
    assert runtime_publish["parameters"]["properties"]["access_mode"]["default"] == "authenticated"
    assert seeded_publish["parameters_schema"]["properties"]["access_mode"]["default"] == "authenticated"
    for required_guidance in (
        "omit access_mode for authenticated access",
        "use public only when the user explicitly wants",
        "search_page_viewers",
        "preserves its current permissions",
        "publication actor and exact publication time",
        "Page URL and Management URL",
    ):
        assert required_guidance in runtime_publish["description"]
        assert required_guidance in seeded_publish["description"]
    assert any(item["function"]["name"] == "list_page_access_requests" for item in AGENT_TOOLS)
    assert any(item["name"] == "list_page_access_requests" for item in BUILTIN_TOOLS)
    runtime_list = next(item["function"] for item in AGENT_TOOLS if item["function"]["name"] == "list_published_pages")
    for expected_metadata in ("creator", "creation time", "most recent publisher", "publication time"):
        assert expected_metadata in runtime_list["description"]
    runtime_page_tools = {
        item["function"]["name"]: item["function"]
        for item in AGENT_TOOLS
        if item["function"]["name"] in {
            "publish_page",
            "search_page_viewers",
            "update_published_page_access",
            "list_published_pages",
            "list_page_access_requests",
        }
    }
    seeded_page_tools = {
        item["name"]: item
        for item in BUILTIN_TOOLS
        if item["name"] in runtime_page_tools
    }
    assert seeded_page_tools.keys() == runtime_page_tools.keys()
    for tool_name, runtime_tool in runtime_page_tools.items():
        assert seeded_page_tools[tool_name]["description"] == runtime_tool["description"]
        assert seeded_page_tools[tool_name]["parameters_schema"] == runtime_tool["parameters"]

async def test_agent_page_list_includes_management_links():
    _short_id, page_id, agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    result = await _list_published_pages(agent_id)
    assert f"/published-pages?agent_id={agent_id}" in result
    assert f"/published-pages?page={page_id}" in result


async def test_page_manager_can_use_member_picker_directory():
    _short_id, page_id, _agent_id, owner_id, viewer_id = await _make_restricted_page()
    async with async_session() as db:
        published_page = await db.get(PublishedPage, page_id)
        department = OrgDepartment(name="研发部", path="研发部", tenant_id=published_page.tenant_id)
        db.add(department)
        await db.flush()
        owner = await db.get(User, owner_id)
        db.add(OrgMember(
            name=owner.display_name,
            email="owner@test.local",
            department_id=department.id,
            department_path="研发部",
            tenant_id=published_page.tenant_id,
            user_id=owner_id,
        ))
        await db.commit()
    owner_token = create_access_token(str(owner_id), "member")
    viewer_token = create_access_token(str(viewer_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        departments = await client.get(
            f"/api/pages/{page_id}/directory/departments",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert departments.status_code == 200
        assert departments.json()["items"][0]["name"] == "研发部"
        members = await client.get(
            f"/api/pages/{page_id}/directory/members?department_id={department.id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert members.status_code == 200
        assert members.json()["items"][0]["id"] == str(owner_id)
        denied = await client.get(
            f"/api/pages/{page_id}/directory/departments",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert denied.status_code == 404
