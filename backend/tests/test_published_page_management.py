"""Published-page management, publication, and directory tests."""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.models.org import OrgDepartment, OrgMember
from app.models.tool import AgentTool, Tool
from app.services.agent_tools import (
    _list_page_access_requests,
    _list_published_pages,
    _publish_page,
    _search_page_viewers,
    _update_published_page_access,
    get_agent_tools_for_llm,
)
from app.services.tool_seeder import seed_builtin_tools
from tests.test_published_page_access import (
    Agent,
    PublishedPage,
    PublishedPageAccess,
    PublishedPageAnonymousVisitor,
    PublishedPageVisitor,
    Tenant,
    User,
    _dispose_engine,  # noqa: F401 - expose imported autouse fixture to this module
    _make_restricted_page,
    _make_user,
    app,
    async_session,
    create_access_token,
    settings,
)

pytestmark = pytest.mark.asyncio


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


async def test_admin_agent_tool_can_change_any_page_in_current_company_only():
    async with async_session() as db:
        tenant = Tenant(name="Admin Tool Tenant", slug=f"admin-tool-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        other_tenant = Tenant(name="Other Tool Tenant", slug=f"other-tool-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add_all([tenant, other_tenant])
        await db.flush()
        current_owner = await _make_user(db, tenant.id, "Current Agent Owner")
        page_owner = await _make_user(db, tenant.id, "Other Page Owner")
        org_admin = await _make_user(db, tenant.id, "Tool Company Admin", role="org_admin")
        platform_admin = await _make_user(db, tenant.id, "Tool Platform Admin", is_platform_admin=True)
        other_admin = await _make_user(db, other_tenant.id, "Other Company Admin", role="org_admin")
        current_agent = Agent(
            name="Current Conversation Agent", role_description="", creator_id=current_owner.id,
            tenant_id=tenant.id, agent_type="native",
        )
        page_agent = Agent(
            name="Different Page Agent", role_description="", creator_id=page_owner.id,
            tenant_id=tenant.id, agent_type="native",
        )
        db.add_all([current_agent, page_agent])
        await db.flush()
        page = PublishedPage(
            short_id=f"p{uuid.uuid4().hex[:7]}", agent_id=page_agent.id, user_id=page_owner.id,
            tenant_id=tenant.id, source_path="admin-tool.html", title="Admin Tool Page",
            access_mode="restricted",
        )
        db.add(page)
        await db.commit()
        current_agent_id = current_agent.id
        page_id = page.id
        short_id = page.short_id
        current_owner_id = current_owner.id
        org_admin_id = org_admin.id
        platform_admin_id = platform_admin.id
        other_admin_id = other_admin.id

    denied_member = await _update_published_page_access(
        current_agent_id,
        current_owner_id,
        {"short_id": short_id, "access_mode": "public", "allowed_user_ids": []},
    )
    assert denied_member.startswith("Published page not found")

    updated_by_company_admin = await _update_published_page_access(
        current_agent_id,
        org_admin_id,
        {"short_id": short_id, "access_mode": "authenticated", "allowed_user_ids": []},
    )
    assert updated_by_company_admin.startswith("Updated")

    updated_by_platform_admin = await _update_published_page_access(
        current_agent_id,
        platform_admin_id,
        {"short_id": short_id, "access_mode": "public", "allowed_user_ids": []},
    )
    assert updated_by_platform_admin.startswith("Updated")

    denied_cross_tenant = await _update_published_page_access(
        current_agent_id,
        other_admin_id,
        {"short_id": short_id, "access_mode": "restricted", "allowed_user_ids": []},
    )
    assert denied_cross_tenant.startswith("Permission denied")

    async with async_session() as db:
        updated_page = await db.get(PublishedPage, page_id)
        assert updated_page.access_mode == "public"


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


async def test_company_and_platform_admins_see_all_pages_in_current_company_with_pagination():
    async with async_session() as db:
        tenant = Tenant(name="Admin Page List", slug=f"admin-pages-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        other_tenant = Tenant(name="Other Admin Page List", slug=f"other-pages-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        db.add_all([tenant, other_tenant])
        await db.flush()
        first_owner = await _make_user(db, tenant.id, "First Owner")
        second_owner = await _make_user(db, tenant.id, "Second Owner")
        org_admin = await _make_user(db, tenant.id, "Company Admin", role="org_admin")
        platform_admin = await _make_user(db, tenant.id, "Platform Admin", is_platform_admin=True)
        outsider = await _make_user(db, other_tenant.id, "Other Owner")
        first_agent = Agent(
            name="First Agent", role_description="", creator_id=first_owner.id,
            tenant_id=tenant.id, agent_type="native", access_mode="company",
        )
        private_agent = Agent(
            name="Private Agent", role_description="", creator_id=second_owner.id,
            tenant_id=tenant.id, agent_type="native", access_mode="private",
        )
        outsider_agent = Agent(
            name="Other Agent", role_description="", creator_id=outsider.id,
            tenant_id=other_tenant.id, agent_type="native",
        )
        db.add_all([first_agent, private_agent, outsider_agent])
        await db.flush()
        first_page = PublishedPage(
            short_id=f"p{uuid.uuid4().hex[:7]}", agent_id=first_agent.id, user_id=first_owner.id,
            tenant_id=tenant.id, source_path="first.html", title="First", access_mode="authenticated",
        )
        private_page = PublishedPage(
            short_id=f"p{uuid.uuid4().hex[:7]}", agent_id=private_agent.id, user_id=second_owner.id,
            tenant_id=tenant.id, source_path="private.html", title="Private", access_mode="restricted",
        )
        outsider_page = PublishedPage(
            short_id=f"p{uuid.uuid4().hex[:7]}", agent_id=outsider_agent.id, user_id=outsider.id,
            tenant_id=other_tenant.id, source_path="other.html", title="Other", access_mode="public",
        )
        db.add_all([first_page, private_page, outsider_page])
        await db.commit()
        expected_page_ids = {str(first_page.id), str(private_page.id)}
        expected_agent_ids = {str(first_agent.id), str(private_agent.id)}
        first_page_id = first_page.id
        first_owner_id = first_owner.id
        org_admin_id = org_admin.id
        platform_admin_id = platform_admin.id
        private_page_id = private_page.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for admin_id in (org_admin_id, platform_admin_id):
            token = create_access_token(str(admin_id), "member")
            headers = {"Authorization": f"Bearer {token}"}
            listed_ids = set()
            for page_number in (1, 2):
                response = await client.get(
                    "/api/pages/mine",
                    params={"page": page_number, "page_size": 1},
                    headers=headers,
                )
                assert response.status_code == 200
                assert response.json()["total"] == 2
                assert response.json()["page_size"] == 1
                listed_ids.update(item["id"] for item in response.json()["items"])
            assert listed_ids == expected_page_ids

            options = await client.get("/api/pages/agent-options", headers=headers)
            assert options.status_code == 200
            assert {item["id"] for item in options.json()} == expected_agent_ids

        owner_token = create_access_token(str(first_owner_id), "member")
        owner_list = await client.get(
            "/api/pages/mine",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_list.status_code == 200
        assert {item["id"] for item in owner_list.json()["items"]} == {str(first_page_id)}

        admin_token = create_access_token(str(org_admin_id), "org_admin")
        detail = await client.get(
            f"/api/pages/{private_page_id}/detail",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert detail.status_code == 200

        bulk_update = await client.put(
            "/api/pages/batch/access",
            json={
                "page_ids": list(expected_page_ids),
                "access_mode": "public",
                "allowed_user_ids": [],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert bulk_update.status_code == 200
        assert bulk_update.json() == {"updated_count": 2}

        denied_bulk_update = await client.put(
            "/api/pages/batch/access",
            json={
                "page_ids": list(expected_page_ids),
                "access_mode": "authenticated",
                "allowed_user_ids": [],
            },
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert denied_bulk_update.status_code == 404

    async with async_session() as db:
        updated_modes = set((await db.scalars(select(PublishedPage.access_mode).where(
            PublishedPage.id.in_([first_page_id, private_page_id])
        ))).all())
        assert updated_modes == {"public"}


async def test_bulk_access_limit_and_concurrent_replacements_remain_complete_sets():
    _short_id, page_id, _agent_id, owner_id, first_viewer_id = await _make_restricted_page()
    async with async_session() as db:
        owner = await db.get(User, owner_id)
        owner.role = "org_admin"
        second_viewer = await _make_user(db, owner.tenant_id, "Second Viewer")
        await db.commit()
        second_viewer_id = second_viewer.id

    token = create_access_token(str(owner_id), "org_admin")
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        too_many = await client.put(
            "/api/pages/batch/access",
            json={
                "page_ids": [str(uuid.uuid4()) for _ in range(101)],
                "access_mode": "authenticated",
                "allowed_user_ids": [],
            },
            headers=headers,
        )
        assert too_many.status_code == 422

        first_update, second_update = await asyncio.gather(
            client.put(
                "/api/pages/batch/access",
                json={
                    "page_ids": [str(page_id)],
                    "access_mode": "restricted",
                    "allowed_user_ids": [str(first_viewer_id)],
                },
                headers=headers,
            ),
            client.put(
                "/api/pages/batch/access",
                json={
                    "page_ids": [str(page_id)],
                    "access_mode": "restricted",
                    "allowed_user_ids": [str(second_viewer_id)],
                },
                headers=headers,
            ),
        )
        assert first_update.status_code == 200
        assert second_update.status_code == 200

    async with async_session() as db:
        approved_ids = set((await db.scalars(select(PublishedPageAccess.user_id).where(
            PublishedPageAccess.page_id == page_id,
            PublishedPageAccess.status == "approved",
        ))).all())
    assert approved_ids in ({first_viewer_id}, {second_viewer_id})


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


async def test_page_list_filters_agents_access_mode_and_fuzzy_search_fields():
    short_id, page_id, agent_id, owner_id, viewer_id = await _make_restricted_page()
    marker = uuid.uuid4().hex[:10]
    agent_term = f"agent-{marker}"
    title_term = f"title-{marker}"
    path_term = f"path-{marker}"
    publisher_term = f"publisher-{marker}"
    modifier_term = f"modifier-{marker}"
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        page = await db.get(PublishedPage, page_id)
        owner = await db.get(User, owner_id)
        modifier = await db.get(User, viewer_id)
        agent.name = f"Search {agent_term}"
        page.title = f"Search {title_term}"
        page.source_path = f"reports/{path_term}.html"
        owner.display_name = f"Search {publisher_term}"
        modifier.display_name = f"Search {modifier_term}"
        page.last_published_by_user_id = modifier.id
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
        for term in (
            title_term.upper(),
            path_term,
            f"https://example.test/p/{short_id}",
            publisher_term,
            modifier_term.upper(),
        ):
            response = await client.get(
                "/api/pages/mine",
                params={"q": term},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert any(item["id"] == str(page_id) for item in response.json()["items"])

        for access_mode, expected_id in (
            ("restricted", page_id),
            ("authenticated", second_page_id),
        ):
            response = await client.get(
                "/api/pages/mine",
                params={"access_mode": access_mode},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert {item["id"] for item in response.json()["items"]} == {str(expected_id)}

        invalid_mode = await client.get(
            "/api/pages/mine",
            params={"access_mode": "company"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert invalid_mode.status_code == 422

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
    assert "Platform watermark: enabled automatically (signed-in user identity)." in first
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
    assert "Platform watermark: enabled automatically (anonymous visitor ID and access time)." in second
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


async def test_seeded_publish_page_guidance_reaches_actual_llm_tool_output():
    _short_id, _page_id, agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    await seed_builtin_tools()

    # The helper creates an Agent directly rather than through provisioning,
    # so give it the same default tool binding that provisioning would create.
    async with async_session() as db:
        publish_tool_row = await db.scalar(select(Tool).where(Tool.name == "publish_page"))
        assignment = await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id == publish_tool_row.id,
        ))
        if assignment is None:
            db.add(AgentTool(agent_id=agent_id, tool_id=publish_tool_row.id, enabled=True))
            await db.commit()

    visible_tools = await get_agent_tools_for_llm(agent_id)
    publish_tool = next(tool for tool in visible_tools if tool["function"]["name"] == "publish_page")
    description = publish_tool["function"]["description"]
    assert "Non-public pages receive the platform watermark automatically" in description


async def test_agent_page_list_includes_management_links():
    _short_id, page_id, agent_id, _owner_id, _viewer_id = await _make_restricted_page()
    result = await _list_published_pages(agent_id)
    assert f"/published-pages?agent_id={agent_id}" in result
    assert f"/published-pages?page={page_id}" in result


async def test_page_manager_can_use_member_picker_directory():
    _short_id, page_id, _agent_id, owner_id, viewer_id = await _make_restricted_page()
    async with async_session() as db:
        published_page = await db.get(PublishedPage, page_id)
        department = OrgDepartment(
            name="研发部", path="研发部", tenant_id=published_page.tenant_id, member_count=1,
        )
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
