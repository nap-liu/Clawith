"""Tests for first-party chat sessions across web and H5 channels."""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.api.websocket import WebSocketChatHandler
from app.core.security import create_access_token
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.chat_session_service import ensure_primary_platform_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_user_and_agent() -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session() as db:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        agent_id = uuid.uuid4()
        tenant = Tenant(id=tenant_id, name="H5 Tenant", slug=f"h5-{uuid.uuid4().hex[:8]}", im_provider="web_only")
        identity = Identity(email=f"h5-{uuid.uuid4().hex[:8]}@example.com", username=f"h5-{uuid.uuid4().hex[:8]}")
        user = User(id=user_id, identity=identity, display_name="H5 Tester", tenant_id=tenant_id, is_active=True)
        agent = Agent(
            id=agent_id,
            name="H5 Agent",
            creator_id=user_id,
            tenant_id=tenant_id,
            role_description="Helps mobile users",
            status="idle",
        )
        db.add_all([tenant, identity, user, agent])
        await db.commit()
        return user_id, agent_id


async def test_primary_platform_sessions_are_scoped_by_source_channel():
    user_id, agent_id = await _seed_user_and_agent()

    async with async_session() as db:
        web_session = await ensure_primary_platform_session(db, agent_id, user_id, source_channel="web")
        h5_session = await ensure_primary_platform_session(
            db,
            agent_id,
            user_id,
            source_channel="wechat_miniprogram",
        )
        second_h5_session = await ensure_primary_platform_session(
            db,
            agent_id,
            user_id,
            source_channel="wechat_miniprogram",
        )

    assert web_session.id != h5_session.id
    assert web_session.source_channel == "web"
    assert h5_session.source_channel == "wechat_miniprogram"
    assert second_h5_session.id == h5_session.id


async def test_create_session_accepts_h5_source_channel():
    user_id, agent_id = await _seed_user_and_agent()
    token = create_access_token(str(user_id), "member")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/agents/{agent_id}/sessions",
            json={"source_channel": "wechat_miniprogram"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_channel"] == "wechat_miniprogram"


async def test_websocket_default_session_uses_channel_when_no_session_id():
    user_id, agent_id = await _seed_user_and_agent()
    async with async_session() as db:
        web_session = await ensure_primary_platform_session(db, agent_id, user_id, source_channel="web")
        await db.commit()

    handler = WebSocketChatHandler(
        websocket=None,
        agent_id=agent_id,
        token="unused",
        session_id=None,
        lang="zh",
        channel="wechat_miniprogram",
    )

    async with async_session() as db:
        conv_id = await handler._resolve_chat_session(db, user_id)
        h5_session = (
            await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(conv_id)))
        ).scalar_one()

    assert conv_id != str(web_session.id)
    assert h5_session.source_channel == "wechat_miniprogram"
