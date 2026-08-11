"""Runtime contract for the Agent-visible ``send_media`` path guidance."""

from __future__ import annotations

import uuid

import pytest

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import seed_builtin_tools

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    yield
    await engine.dispose()


async def test_seeded_send_media_accepts_any_agent_root_relative_path():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(
            name=f"Media tool runtime {suffix}",
            slug=f"media-tool-runtime-{suffix}",
        )
        db.add(tenant)
        await db.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="Media Tool Runtime Tester",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Media Tool Runtime Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    await seed_builtin_tools()

    runtime_tools = await get_agent_tools_for_llm(agent_id)
    runtime = next(
        tool["function"]
        for tool in runtime_tools
        if tool["function"]["name"] == "send_media"
    )

    file_path = runtime["parameters"]["properties"]["file_path"]
    cover_path = runtime["parameters"]["properties"]["cover_image_path"]
    assert "Any existing file path relative to the current Agent root" in file_path["description"]
    assert "exports/briefing.mp3" in file_path["description"]
    assert "relative to the current Agent root" in cover_path["description"]
