"""Rollback disables only the new catalog entry, preserving assignments."""

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.scripts.agent_login_tool_state import tool_state
from app.services.agent_login_contract import AGENT_LOGIN_SEED
from app.services.tool_seeder import seed_builtin_tools
from tests.test_published_page_access import _dispose_engine, _make_restricted_page  # noqa: F401


@pytest.mark.asyncio
async def test_disable_is_idempotent_and_preserves_bindings():
    _, _, agent_id, _, _ = await _make_restricted_page()
    await seed_builtin_tools()
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == AGENT_LOGIN_SEED["name"]))
        tool.enabled = True
        binding = AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True)
        db.add(binding)
        await db.commit()
    assert (await tool_state(disable=True))["enabled"] is False
    assert (await tool_state(disable=True))["enabled"] is False
    async with async_session() as db:
        assert (await db.get(AgentTool, binding.id)).enabled is True
        assert (await db.get(Tool, tool.id)).enabled is False
        assert await db.scalar(select(Tool.id).where(Tool.name == "publish_page", Tool.enabled.is_(True)))
