"""Visitor search preserves pagination, literal matching and page authorization."""

import httpx
import pytest

from tests.test_published_page_access import (
    Identity,
    PublishedPageAnonymousVisitor,
    PublishedPageVisitor,
    User,
    _dispose_engine,  # noqa: F401
    _make_restricted_page,
    app,
    async_session,
    create_access_token,
)

pytestmark = pytest.mark.asyncio


async def test_visitor_search_filters_before_pagination_and_preserves_access():
    _, page_id, _, owner_id, viewer_id = await _make_restricted_page()
    _, other_page_id, _, other_owner_id, _ = await _make_restricted_page()
    async with async_session() as db:
        viewer = await db.get(User, viewer_id)
        viewer.display_name = "一二三四五六七八九十测试姓名"
        identity = await db.get(Identity, viewer.identity_id)
        identity.email = f"visitor_{viewer_id.hex}%@example.test"
        db.add_all([
            PublishedPageVisitor(page_id=page_id, user_id=viewer_id, view_count=7),
            PublishedPageAnonymousVisitor(page_id=page_id, visitor_key="abcdef123456" + "a" * 52),
            PublishedPageAnonymousVisitor(page_id=page_id, visitor_key="abcdef654321" + "b" * 52),
            PublishedPageAnonymousVisitor(page_id=other_page_id, visitor_key="abcdef123456" + "a" * 52),
        ])
        await db.commit()

    headers = {"Authorization": f"Bearer {create_access_token(str(owner_id), 'member')}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/pages/{page_id}/visitors"
        for query in [" 五六七八九十 ", "VISITOR_", "%@", viewer_id.hex]:
            response = await client.get(url, headers=headers, params={"q": query, "page_size": 1})
            assert response.status_code == 200
            result = response.json()
            assert result["total"] == 1
            assert result["items"][0]["id"] == str(viewer_id)
            assert result["items"][0]["display_name"] == "一二三四五六七八九十测试姓名"
            assert result["items"][0]["view_count"] == 7

        first = (await client.get(url, headers=headers, params={"q": "ABCDEF", "page_size": 1})).json()
        second = (await client.get(url, headers=headers, params={"q": "abcdef", "page_size": 1, "page": 2})).json()
        assert first["total"] == second["total"] == 2
        assert len(first["items"]) == len(second["items"]) == 1
        assert first["items"][0]["id"] != second["items"][0]["id"]
        exact = (await client.get(url, headers=headers, params={"q": "匿名访客 ABCDEF123456"})).json()
        assert exact["total"] == 1
        assert exact["items"][0]["display_name"] == "匿名访客 ABCDEF123456"
        for query, total in [(" ", 3), ("", 3), ("not-present", 0), ("__", 0), ("%%", 0)]:
            result = (await client.get(url, headers=headers, params={"q": query})).json()
            assert result["total"] == total
        for user_id in [viewer_id, other_owner_id]:
            denied = await client.get(url, params={"q": "ABCDEF"}, headers={
                "Authorization": f"Bearer {create_access_token(str(user_id), 'member')}",
            })
            assert denied.status_code == 404
