"""Runtime contract for storage-backed file tool guidance."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import BUILTIN_TOOLS, seed_builtin_tools


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    yield
    await engine.dispose()


async def test_seeded_file_tool_guidance_reaches_llm_runtime():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"File tool runtime {suffix}", slug=f"file-tool-runtime-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"file_tool_runtime_{suffix}",
            email=f"file-tool-runtime-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="File Tool Runtime Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"File Tool Runtime Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    await seed_builtin_tools()

    expected_names = {"list_files", "read_file", "read_document"}
    async with async_session() as db:
        tool_rows = (
            await db.execute(select(Tool).where(Tool.name.in_(expected_names)))
        ).scalars().all()
        assert {tool.name for tool in tool_rows} == expected_names
        existing_assignments = {
            assignment.tool_id: assignment
            for assignment in (
                await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
            ).scalars().all()
        }
        for tool in tool_rows:
            assignment = existing_assignments.get(tool.id)
            if assignment is None:
                db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True))
            else:
                assignment.enabled = True
        await db.commit()

    runtime_tools = await get_agent_tools_for_llm(agent_id)
    runtime_by_name = {
        tool["function"]["name"]: tool["function"]
        for tool in runtime_tools
        if tool["function"]["name"] in expected_names
    }
    seeded_by_name = {
        tool["name"]: tool
        for tool in BUILTIN_TOOLS
        if tool["name"] in expected_names
    }

    assert set(runtime_by_name) == expected_names
    for name in expected_names:
        assert runtime_by_name[name]["description"] == seeded_by_name[name]["description"]
        assert runtime_by_name[name]["parameters"] == seeded_by_name[name]["parameters_schema"]

    assert "copy each displayed path exactly" in runtime_by_name["list_files"]["description"].lower()
    assert "accepts any file type" in runtime_by_name["read_file"]["description"]
    assert "matched exactly" in runtime_by_name["read_document"]["description"]
