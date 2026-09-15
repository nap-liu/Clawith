"""Internal browser login: identity, independent tickets, replay and expiry."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

from app.config import get_settings
from app.core.security import create_access_token, decode_access_token
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.openapi_application import OpenAPICredential
from app.models.tool import AgentTool, Tool
from app.services import agent_login
from app.services.agent_login_contract import AGENT_LOGIN_SEED
from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
from app.services.openapi_applications import digest
from app.services.published_page_access import PAGE_SESSION_COOKIE
from app.services.tool_result_display import tool_event_for_display
from app.services.tool_results import tool_result_text
from app.services.tool_seeder import seed_builtin_tools
from app.services.user_output import UserOutputStreamSanitizer, sanitize_user_visible_text
from app.utils.sanitize import sanitize_tool_args
from tests.test_published_page_access import (
    _dispose_engine,  # noqa: F401
    _make_restricted_page,
    app,
    async_session,
)

pytestmark = pytest.mark.asyncio
EXCHANGE = "/api/openapi/v1/auth/link-exchange"


async def context(monkeypatch):
    short_id, _, agent_id, owner_id, viewer_id = await _make_restricted_page()

    monkeypatch.setenv("PUBLIC_BASE_URL", "http://test")
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=viewer_id, source_channel="web")
        db.add(session)
        await db.flush()
        anchor = ChatMessage(agent_id=agent_id, user_id=viewer_id, sender_user_id=viewer_id,
                             conversation_id=str(session.id), role="user", content="Read the report")
        db.add(anchor)
        await db.commit()
    return short_id, agent_id, owner_id, viewer_id, session.id, anchor.id


async def issue(ctx):
    short_id, agent_id, _, viewer_id, session_id, anchor_id = ctx
    result = await agent_login.create_agent_login_link(
        agent_id, viewer_id, str(session_id), anchor_id, {"page_url": short_id},
    )
    assert not result.get("isError"), result
    output = json.loads(tool_result_text(result))
    assert output["expires_in"] == 300 and output["token_expires_in"] == 3600
    assert "agent_code" not in json.dumps(tool_event_for_display({"tool_result": result}))
    code = parse_qs(urlsplit(output["login_url"]).fragment)["agent_code"][0]
    return code, output["login_url"]


def advance(monkeypatch, seconds):
    instant = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(jwt, "datetime", Clock)
    monkeypatch.setattr(agent_login, "now", lambda: instant)


async def test_independent_links_same_report_and_same_ticket_reentry(monkeypatch):
    ctx = await context(monkeypatch)
    code_a, _ = await issue(ctx)
    code_b, _ = await issue(ctx)
    assert code_a != code_b
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as a, \
            httpx.AsyncClient(transport=transport, base_url="http://test") as b:
        ra = await a.post(EXCHANGE, json={"code": code_a})
        rb = await b.post(EXCHANGE, json={"code": code_b})
        assert ra.status_code == rb.status_code == 200
        token_a = ra.json()["access_token"]
        token_b = rb.json()["access_token"]
        pa = decode_access_token(token_a)
        assert pa["login_code_hash"] == digest(code_a)
        assert decode_access_token(token_b)["login_code_hash"] == digest(code_b)
        assert 3595 <= pa["exp"] - datetime.now(timezone.utc).timestamp() <= 3600
        # Issuance deliberately does not check restricted-report access.
        denied = await a.get(f"/p/{ctx[0]}")
        assert denied.status_code == 302 and "denied=1" in denied.headers["location"]
        advance(monkeypatch, 301)
        again = await a.post(EXCHANGE, json={"code": code_a})
        assert again.status_code == 200
        assert again.json()["access_token"] == token_a
        assert (await a.post(EXCHANGE, json={"code": code_b})).status_code == 401
        assert (await b.post(EXCHANGE, json={"code": code_b})).json()["access_token"] == token_b
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as fresh:
            assert (await fresh.post(EXCHANGE, json={"code": code_a})).status_code == 401


async def test_atomic_redemption_and_expired_unused_link(monkeypatch):
    ctx = await context(monkeypatch)
    code, _ = await issue(ctx)
    unused, _ = await issue(ctx)

    async def redeem():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return (await client.post(EXCHANGE, json={"code": code})).status_code

    assert sorted(await asyncio.gather(redeem(), redeem())) == [200, 401]
    normal = create_access_token(str(ctx[3]), "member")
    advance(monkeypatch, 301)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(EXCHANGE, json={"code": unused})).status_code == 401
        assert (await client.post(EXCHANGE, json={"code": code},
                                 headers={"Authorization": f"Bearer {normal}"})).status_code == 401


async def test_one_hour_report_session_does_not_extend_or_revoke_normal_login(monkeypatch):
    ctx = list(await context(monkeypatch))
    # Give this human report access using the ordinary page owner identity.
    async with async_session() as db:
        anchor = await db.get(ChatMessage, ctx[5])
        anchor.sender_user_id = ctx[2]
        anchor.user_id = ctx[2]
        session = await db.get(ChatSession, ctx[4])
        session.user_id = ctx[2]
        await db.commit()
    ctx[3] = ctx[2]
    code, _ = await issue(ctx)
    normal = create_access_token(str(ctx[3]), "member", timedelta(hours=8))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(EXCHANGE, json={"code": code})
        assert response.status_code == 200
        token = response.json()["access_token"]
        expiry = decode_access_token(token)["exp"]
        for path in [f"/p/{ctx[0]}", f"/p/{ctx[0]}?__report_embed=1", f"/p/{ctx[0]}"]:
            assert (await client.get(path)).status_code == 200
            page_token = client.cookies.get(PAGE_SESSION_COOKIE)
            claims = jwt.decode(page_token, get_settings().SECRET_KEY, algorithms=["HS256"])
            assert claims["exp"] == claims["absolute_exp"] == expiry
        advance(monkeypatch, 3601)
        assert (await client.post(EXCHANGE, json={"code": code})).status_code == 401
        assert (await client.get(f"/p/{ctx[0]}")).status_code == 302
        assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
        assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {normal}"})).status_code == 200
        assert decode_access_token(normal)["sub"] == str(ctx[3])


async def test_no_background_or_foreign_actor_and_no_signature_disclosure(monkeypatch):
    ctx = await context(monkeypatch)
    code, url = await issue(ctx)
    for actor, session_id, anchor_id in [(ctx[2], ctx[4], ctx[5]), (ctx[3], "", None)]:
        result = await agent_login.create_agent_login_link(ctx[1], actor, str(session_id), anchor_id,
                                                         {"page_url": ctx[0]})
        assert result["isError"]
    async with async_session() as db:
        anchor = await db.get(ChatMessage, ctx[5])
        anchor.message_meta = {"kind": "on_message_event"}
        await db.commit()
    result = await agent_login.create_agent_login_link(ctx[1], ctx[3], str(ctx[4]), ctx[5], {"page_url": ctx[0]})
    assert result["isError"]
    assert code not in sanitize_user_visible_text(url)
    assert code not in sanitize_user_visible_text(code)
    assert code not in json.dumps(sanitize_tool_args({"url": url}))
    event = tool_event_for_display({"toolThinking": url, "tool_result": {
        "structuredContent": {"url": url}, "content": [{"type": "resource_link", "uri": url}],
    }})
    assert code not in json.dumps(event)
    for text in [url, f"[report]({url})", code]:
        stream = UserOutputStreamSanitizer()
        output = "".join(stream.feed(char) for char in text) + stream.flush()
        assert code not in output and code[-40:] not in output


async def test_membership_token_derivation_keeps_temporary_deadline(monkeypatch):
    from app.models.user import User

    ctx = await context(monkeypatch)
    code, _ = await issue(ctx)
    async with async_session() as db:
        tenant_id = (await db.get(User, ctx[3])).tenant_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(EXCHANGE, json={"code": code})
        original = decode_access_token(login.json()["access_token"])
        switched = await client.post("/api/auth/switch-tenant", json={"tenant_id": str(tenant_id)})
        assert switched.status_code == 200, switched.text
        derived = decode_access_token(switched.json()["access_token"])
        assert derived["exp"] == original["exp"]
        assert derived["login_code_hash"] == original["login_code_hash"]
        normal = create_access_token(str(ctx[3]), "member")
        switched = await client.post("/api/auth/switch-tenant", json={"tenant_id": str(tenant_id)},
                                    headers={"Authorization": f"Bearer {normal}"})
        assert switched.status_code == 200, switched.text
        assert "login_code_hash" not in decode_access_token(switched.json()["access_token"])


async def test_seeded_tool_schema_and_dispatch(monkeypatch):
    ctx = await context(monkeypatch)
    await seed_builtin_tools()
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == AGENT_LOGIN_SEED["name"]))
        assert tool.parameters_schema == AGENT_LOGIN_SEED["parameters_schema"]
        db.add(AgentTool(agent_id=ctx[1], tool_id=tool.id, enabled=True))
        await db.commit()
    schemas = await get_agent_tools_for_llm(ctx[1])
    schema = next(item["function"] for item in schemas if item["function"]["name"] == tool.name)
    assert schema["parameters"] == AGENT_LOGIN_SEED["parameters_schema"]
    result = await execute_tool(tool.name, {"page_url": ctx[0]}, ctx[1], ctx[3],
                                session_id=str(ctx[4]), turn_anchor_id=ctx[5], skip_autonomy=True)
    assert "login_url" in tool_result_text(result)
    assert "login_url" not in tool_result_text(result, audience="user")
    async with async_session() as db:
        assert await db.scalar(select(OpenAPICredential).where(
            OpenAPICredential.user_id == ctx[3], OpenAPICredential.kind == "agent_login",
        )) is not None
