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
    await seed_builtin_tools()
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

    async with async_session() as db:
        names = {
            "start_dingtalk_channel_provisioning",
            "get_dingtalk_channel_provisioning_status",
        }
        rows = (await db.scalars(select(Tool).where(Tool.name.in_(names)))).all()
        assert {row.name for row in rows} == names
        for row in rows:
            assert row.category == "communication"
            assert row.is_default is True
            assert "数字员工" in row.display_name + row.description
            assert "Agent" not in row.display_name + row.description
            assert "client_secret" not in row.description
            db.add(AgentTool(agent_id=agent_id, tool_id=row.id, enabled=True))
        await db.commit()

    runtime_by_name = {
        tool["function"]["name"]: tool["function"]
        for tool in await get_agent_tools_for_llm(agent_id)
    }
    for row in rows:
        assert runtime_by_name[row.name]["parameters"] == row.parameters_schema
        assert runtime_by_name[row.name]["description"] == row.description
        assert row.parameters_schema["type"] == "object"
    status = runtime_by_name["get_dingtalk_channel_provisioning_status"]["parameters"]
    assert status["required"] == ["provisioning_id"]
    assert status["properties"]["provisioning_id"]["type"] == "string"
    runtime = runtime_by_name["start_dingtalk_channel_provisioning"]

    assert "已配置时默认" in runtime["description"]
    assert "无需再次授权" in runtime["description"]
    assert "强制重配会创建新的钉钉机器人应用" in runtime["description"]
    assert not runtime["parameters"].get("required")
    force = runtime["parameters"]["properties"]["force_reconfigure"]
    assert force["type"] == "boolean"
    assert force["default"] is False
