"""Runtime contract for the Agent-visible DingTalk provisioning guidance."""

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import seed_builtin_tools


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    yield
    await engine.dispose()


async def test_seeded_dingtalk_guidance_reaches_actual_llm_tool_output():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(
            name=f"DingTalk tool runtime {suffix}",
            slug=f"dingtalk-tool-runtime-{suffix}",
        )
        db.add(tenant)
        await db.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="DingTalk Tool Runtime Tester",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"DingTalk Tool Runtime Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    await seed_builtin_tools()

    async with async_session() as db:
        tool = (
            await db.execute(
                select(Tool).where(Tool.name == "start_dingtalk_channel_provisioning")
            )
        ).scalar_one()
        assignment = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool.id,
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True))
        else:
            assignment.enabled = True
        await db.commit()

    runtime_tools = await get_agent_tools_for_llm(agent_id)
    runtime = next(
        tool["function"]
        for tool in runtime_tools
        if tool["function"]["name"] == "start_dingtalk_channel_provisioning"
    )

    assert "已配置时默认" in runtime["description"]
    assert "无需再次授权" in runtime["description"]
    assert "强制重配会创建新的钉钉机器人应用" in runtime["description"]
    assert not runtime["parameters"].get("required")
    force = runtime["parameters"]["properties"]["force_reconfigure"]
    assert force["type"] == "boolean"
    assert force["default"] is False
