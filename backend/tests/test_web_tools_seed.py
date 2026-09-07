"""Opt-in browser contracts reach the LLM through persisted tool assignments."""

from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import seed_builtin_tools
from tests.test_agent_mcp_lifecycle import _isolate, _make_agents  # noqa: F401 - autouse fixture


@pytest.mark.asyncio
async def test_seeded_browser_tools_require_assignment_and_expose_parameters():
    required = {
        "browse": ["url"], "web_open": ["url"], "web_eval": ["expression"],
        "web_cdp": ["method"], "web_screenshot": [],
    }
    await seed_builtin_tools()
    _, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        rows = (await db.scalars(select(Tool).where(Tool.name.in_(required)))).all()
        assert {row.name for row in rows} == set(required)
        for row in rows:
            assert row.is_default is False
            assert row.category == "browser"
            assert row.config["sandbox_type"] == "aio_sandbox"
            endpoint = urlsplit(row.config["api_url"])
            assert endpoint.scheme in {"http", "https"} and endpoint.hostname
        assert not (await db.scalars(
            select(AgentTool).where(AgentTool.agent_id == agent_id)
        )).all()
        visible = await get_agent_tools_for_llm(agent_id)
        assert set(required).isdisjoint(t["function"]["name"] for t in visible)
        db.add_all(AgentTool(agent_id=agent_id, tool_id=row.id, enabled=True) for row in rows)
        await db.commit()

    runtime = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in await get_agent_tools_for_llm(agent_id)
    }
    for row in rows:
        schema = runtime[row.name]
        assert schema == row.parameters_schema
        assert schema.get("required", []) == required[row.name]
        for name in required[row.name]:
            assert schema["properties"][name]["type"] == "string"
    for name in ("extract", "screenshot"):
        assert runtime["browse"]["properties"][name]["type"] == "boolean"
