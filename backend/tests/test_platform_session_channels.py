"""Tests for first-party chat sessions across web and H5 channels."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.api.websocket import WebSocketChatHandler
from app.core.security import create_access_token
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.audit import ChatMessage
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
    assert body["is_primary"] is True

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sessions_resp = await client.get(
            f"/api/agents/{agent_id}/sessions?scope=mine&source_channel=wechat_miniprogram",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert sessions_resp.status_code == 200, sessions_resp.text
    assert [row["id"] for row in sessions_resp.json()] == [body["id"]]


async def test_primary_session_is_not_lost_after_first_page_of_active_history():
    user_id, agent_id = await _seed_user_and_agent()
    token = create_access_token(str(user_id), "member")
    now = datetime.now(UTC)
    primary_id = uuid.uuid4()

    async with async_session() as db:
        db.add(ChatSession(
            id=primary_id,
            agent_id=agent_id,
            user_id=user_id,
            source_channel="web",
            title="Newest empty primary",
            is_primary=True,
            created_at=now,
        ))
        for index in range(50):
            session_id = uuid.uuid4()
            active_at = now + timedelta(minutes=index + 1)
            db.add(ChatSession(
                id=session_id,
                agent_id=agent_id,
                user_id=user_id,
                source_channel="web",
                title=f"Older active session {index}",
                is_primary=False,
                created_at=now - timedelta(days=index + 1),
                last_message_at=active_at,
            ))
            db.add(ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="user",
                content=f"message {index}",
                conversation_id=str(session_id),
                created_at=active_at,
            ))
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/agents/{agent_id}/sessions?scope=mine&limit=50",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 50
    assert rows[0]["id"] == str(primary_id)
    assert rows[0]["is_primary"] is True


async def test_websocket_default_session_uses_channel_when_no_session_id():
    user_id, agent_id = await _seed_user_and_agent()
    now = datetime.now(UTC)
    async with async_session() as db:
        web_session = await ensure_primary_platform_session(db, agent_id, user_id, source_channel="web")
        older_h5_session = await ensure_primary_platform_session(
            db,
            agent_id,
            user_id,
            source_channel="wechat_miniprogram",
        )
        older_h5_session.created_at = now
        older_h5_session.last_message_at = now + timedelta(hours=2)
        latest_h5_session = ChatSession(
            id=uuid.uuid4(),
            agent_id=agent_id,
            user_id=user_id,
            source_channel="wechat_miniprogram",
            title="Newest H5 session",
            is_primary=False,
            created_at=now + timedelta(minutes=1),
            last_message_at=now,
        )
        db.add(latest_h5_session)
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
    assert conv_id == str(latest_h5_session.id)
    assert h5_session.source_channel == "wechat_miniprogram"

    async with async_session() as db:
        repaired = (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == user_id,
                    ChatSession.source_channel == "wechat_miniprogram",
                )
                .order_by(ChatSession.created_at)
            )
        ).scalars().all()

    assert [session.is_primary for session in repaired] == [False, True]


async def test_deleting_primary_promotes_latest_remaining_session():
    user_id, agent_id = await _seed_user_and_agent()
    token = create_access_token(str(user_id), "member")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/agents/{agent_id}/sessions",
            json={"source_channel": "wechat_miniprogram"},
            headers={"Authorization": f"Bearer {token}"},
        )
        second = await client.post(
            f"/api/agents/{agent_id}/sessions",
            json={"source_channel": "wechat_miniprogram"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    async with async_session() as db:
        first_before_delete = await db.get(ChatSession, uuid.UUID(first.json()["id"]))
        second_before_delete = await db.get(ChatSession, uuid.UUID(second.json()["id"]))

    assert first_before_delete is not None
    assert second_before_delete is not None
    assert first_before_delete.is_primary is False
    assert second_before_delete.is_primary is True

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        deleted = await client.delete(
            f"/api/agents/{agent_id}/sessions/{second.json()['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert deleted.status_code == 204, deleted.text

    async with async_session() as db:
        remaining = await db.get(ChatSession, uuid.UUID(first.json()["id"]))
        removed = await db.get(ChatSession, uuid.UUID(second.json()["id"]))

    assert remaining is not None
    assert remaining.is_primary is True
    assert removed is None


async def test_admin_deleting_another_users_primary_promotes_that_users_session():
    admin_user_id, agent_id = await _seed_user_and_agent()
    now = datetime.now(UTC)
    other_user_id = uuid.uuid4()

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        identity = Identity(
            email=f"h5-other-{uuid.uuid4().hex[:8]}@example.com",
            username=f"h5-other-{uuid.uuid4().hex[:8]}",
        )
        other_user = User(
            id=other_user_id,
            identity=identity,
            display_name="Other H5 User",
            tenant_id=agent.tenant_id,
            is_active=True,
        )
        db.add_all([identity, other_user])
        await db.flush()
        older = ChatSession(
            id=uuid.uuid4(),
            agent_id=agent_id,
            user_id=other_user_id,
            source_channel="web",
            title="Other user older session",
            is_primary=False,
            created_at=now,
        )
        latest = ChatSession(
            id=uuid.uuid4(),
            agent_id=agent_id,
            user_id=other_user_id,
            source_channel="web",
            title="Other user latest session",
            is_primary=True,
            created_at=now + timedelta(minutes=1),
        )
        db.add_all([older, latest])
        await db.commit()

    token = create_access_token(str(admin_user_id), "member")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        deleted = await client.delete(
            f"/api/agents/{agent_id}/sessions/{latest.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert deleted.status_code == 204, deleted.text

    async with async_session() as db:
        promoted = await db.get(ChatSession, older.id)

    assert promoted is not None
    assert promoted.user_id == other_user_id
    assert promoted.is_primary is True
