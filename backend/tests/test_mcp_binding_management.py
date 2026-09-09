"""Agent binding deletion enforces management authority and preserves peers."""

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session
from app.main import app
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.models.user import User
from mcp_tool_refresh_support import _isolate, _make_agent  # noqa: F401 -- autouse fixture
from test_mcp_refresh_revocation import _installation

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("actor", ["foreign", "use_only", "owner", "org_admin", "platform_admin"])
async def test_binding_delete_requires_manage_and_preserves_other_agents(actor):
    owner, agent, _server, tool_id = await _installation()
    caller, _, _ = await _make_agent()
    async with async_session() as db:
        if actor == "owner":
            caller = owner
        else:
            caller = await db.get(User, caller.id)
            if actor in {"use_only", "org_admin"}:
                caller.tenant_id = agent.tenant_id
            if actor in {"org_admin", "platform_admin"}:
                caller.role = actor
        peer = Agent(name="Other installed Agent", creator_id=owner.id, tenant_id=agent.tenant_id)
        db.add(peer)
        await db.flush()
        peer_binding = AgentTool(agent_id=peer.id, tool_id=tool_id, enabled=True,
                                 source="user_installed", installed_by_agent_id=peer.id)
        db.add(peer_binding)
        binding_id = await db.scalar(select(AgentTool.id).where(
            AgentTool.agent_id == agent.id, AgentTool.tool_id == tool_id,
        ))
        await db.commit()
        peer_binding_id = peer_binding.id
        token = create_access_token(str(caller.id), caller.role)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/tools/agent-tool/{binding_id}",
                                       headers={"Authorization": f"Bearer {token}"})
        allowed = actor not in {"foreign", "use_only"}
        assert response.status_code == (200 if allowed else 403), response.text
        async with async_session() as db:
            assert (await db.get(AgentTool, binding_id) is None) == allowed
            assert await db.get(AgentTool, peer_binding_id) is not None
            assert await db.get(Tool, tool_id) is not None
        if allowed:
            # The final authorized removal deletes the orphaned MCP Tool.
            response = await client.delete(f"/api/tools/agent-tool/{peer_binding_id}",
                                           headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200, response.text
            async with async_session() as db:
                assert await db.get(Tool, tool_id) is None
