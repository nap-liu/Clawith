from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession  # noqa: F401 - register message FK target
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.mcp_server import MCPServer  # noqa: F401 - register tool FK target
from app.models.participant import Participant  # noqa: F401 - register message FK target
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool  # noqa: F401 - register MCP FK edges
from app.models.user import Identity, User
from app.services import agent_tools_message_transports
from app.services.feishu_service import feishu_service

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    yield
    await engine.dispose()


async def test_legacy_feishu_outlet_persists_relative_markdown_before_projection(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"IM projection {suffix}", slug=f"im-projection-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"im_projection_{suffix}",
            email=f"im-projection-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="IM recipient",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Projection Agent",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="feishu",
                app_id="app-id",
                app_secret="app-secret",
                is_configured=True,
                extra_config={},
            )
        )
        await db.commit()
        agent_id = agent.id
        user_id = user.id

    original = "结果：![图](workspace/reports/chart.png)"
    projected = "结果：![图](https://storage.example/chart.png?signed=yes)"
    projection_observed_persisted_original = False

    async def resolve_recipient(*_args, **_kwargs):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="IM recipient"),
            member=SimpleNamespace(external_id="feishu-user", open_id=None),
        )

    async def project(actual_agent_id, text):
        nonlocal projection_observed_persisted_original
        assert actual_agent_id == agent_id
        assert text == original
        async with async_session() as db:
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == original,
                    )
                )
            ).scalar_one()
        projection_observed_persisted_original = True
        return projected

    provider_payloads: list[dict] = []

    async def send_message(*_args, **kwargs):
        provider_payloads.append(kwargs)
        return {"code": 0, "data": {"message_id": "provider-message"}}

    monkeypatch.setattr(
        agent_tools_message_transports,
        "resolve_human_channel_recipient",
        resolve_recipient,
    )
    monkeypatch.setattr(
        agent_tools_message_transports,
        "project_agent_images_for_im",
        project,
    )
    monkeypatch.setattr(feishu_service, "send_message", send_message)

    result = await agent_tools_message_transports._send_feishu_message(
        agent_id,
        {"user_id": str(user_id), "message": original},
        tool_call_id=f"tool-{suffix}",
    )

    assert projection_observed_persisted_original is True
    assert "Message sent" in result
    assert json.loads(provider_payloads[0]["content"])["text"] == projected
    async with async_session() as db:
        stored = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.role == "assistant",
                )
            )
        ).scalar_one()
    assert stored.content == original
