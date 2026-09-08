"""Concurrent first-party admissions keep one newest primary without 500s."""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session, engine
from app.main import app
from app.models.chat_session import ChatSession
from app.services.chat_session_service import ensure_primary_platform_session
from tests.test_platform_session_channels import _seed_user_and_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.parametrize("channel", ["web", "miniprogram", "wechat_miniprogram"])
async def test_concurrent_creation_preserves_all_sessions_and_latest_primary(channel):
    user_id, agent_id = await _seed_user_and_agent()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(
            client.post(
                f"/api/agents/{agent_id}/sessions",
                json={"source_channel": channel},
                headers={"Authorization": f"Bearer {create_access_token(str(user_id), 'member')}"},
            )
            for _ in range(8)
        ))
    assert [response.status_code for response in responses] == [201] * 8
    assert len({response.json()["id"] for response in responses}) == 8
    async with async_session() as db:
        sessions = (await db.scalars(select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.source_channel == channel,
        ).order_by(ChatSession.created_at.desc(), ChatSession.id.desc()))).all()
    assert len(sessions) == 8
    assert [session.is_primary for session in sessions] == [True] + [False] * 7


async def test_concurrent_implicit_session_resolution_creates_one_session():
    user_id, agent_id = await _seed_user_and_agent()

    async def resolve():
        async with async_session() as db:
            session = await ensure_primary_platform_session(db, agent_id, user_id)
            await db.commit()
            return session.id

    ids = await asyncio.gather(*(resolve() for _ in range(8)))
    assert len(set(ids)) == 1
    async with async_session() as db:
        sessions = (await db.scalars(select(ChatSession).where(
            ChatSession.agent_id == agent_id,
        ))).all()
    assert len(sessions) == 1
    assert sessions[0].is_primary
