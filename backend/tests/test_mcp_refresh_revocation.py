"""Real PostgreSQL revocation while provider discovery is suspended."""

import asyncio
import json
import uuid
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tool import AgentTool, Tool
from app.services.agent_mcp_lifecycle import refresh_mcp_server, uninstall_mcp_server
from app.services.mcp_refresh_service import refresh_mcp_server_tools
from app.services.mcp_refresh_snapshot import MCPRefreshChanged, MCPRefreshUnavailable
from app.services.mcp_server_service import agent_private_server_name
from mcp_tool_refresh_support import _isolate, _make_agent  # noqa: F401 -- shared autouse fixture

pytestmark = pytest.mark.asyncio


async def _installation():
    user, agent, server = await _make_agent()
    async with async_session() as db:
        tool = Tool(name=f"mcp_{server.name}_read", display_name="Read", type="mcp",
                    tenant_id=agent.tenant_id, source="agent", enabled=True,
                    mcp_server_id=server.id, mcp_tool_name="read")
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True,
                         source="user_installed", installed_by_agent_id=agent.id))
        await db.commit()
        return user, agent, server, tool.id


@pytest.mark.parametrize("change", ["disable", "uninstall", "override"])
async def test_discovery_has_no_transaction_and_cannot_restore_revoked_state(change):
    user, agent, server, tid = await _installation()
    entered, release = asyncio.Event(), asyncio.Event()
    session_state = {}

    class Provider:
        server_instructions = "New instructions"

        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self):
            assert not session_state["db"].in_transaction()
            entered.set()
            await release.wait()
            return [{"name": "new_tool", "inputSchema": {}}]

    async def run_refresh():
        async with async_session() as db:
            session_state["db"] = db
            try:
                return await refresh_mcp_server_tools(db, server.id, agent_id=agent.id,
                                                     user_id=user.id, assign_to_agent=True)
            finally:
                await db.rollback()

    with patch("app.services.mcp_refresh_service.MCPClient", Provider):
        task = asyncio.create_task(run_refresh())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if change == "disable":
                # The actual platform endpoint owns its own transaction.
                from app.main import app
                from app.models.user import User

                async with async_session() as db:
                    admin = await db.get(User, user.id)
                    admin.role = "platform_admin"
                    await db.commit()
                token = create_access_token(str(user.id), "platform_admin")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.put("/api/tools/bulk", json=[{"tool_id": str(tid), "enabled": False}],
                                                headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == 200, response.text
            elif change == "uninstall":
                assert json.loads(await uninstall_mcp_server(agent.id, server.id))["ok"]
            else:
                async with async_session() as db:
                    db.add(MCPServerOverride(mcp_server_id=server.id, scope_type="agent",
                                             scope_id=agent.id, url_template="https://changed.example/mcp"))
                    await db.commit()
            release.set()
            with pytest.raises((MCPRefreshUnavailable, MCPRefreshChanged, LookupError)):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with async_session() as db:
        tools = (await db.scalars(select(Tool).where(Tool.mcp_server_id == server.id))).all()
        assert not any(tool.mcp_tool_name == "new_tool" for tool in tools)
        if change == "disable":
            assert len(tools) == 1 and tools[0].enabled is False
        if change == "uninstall":
            assert not await db.scalar(select(AgentTool.id).where(AgentTool.agent_id == agent.id))


async def test_disabled_installation_rejects_refresh_before_discovery():
    user, agent, server, tid = await _installation()
    async with async_session() as db:
        tool = await db.get(Tool, tid)
        tool.enabled = False
        await db.commit()
    with patch("app.services.mcp_refresh_service.MCPClient") as provider:
        result = json.loads(await refresh_mcp_server(agent.id, server.id, user_id=user.id, session_id=""))
    assert result["ok"] is False
    assert result["error"] == "mcp_refresh_unavailable"
    provider.assert_not_called()


async def test_admin_refresh_preserves_disabled_rows_and_keeps_new_tool_contract():
    user, agent, server, tid = await _installation()
    async with async_session() as db:
        tool = await db.get(Tool, tid)
        tool.enabled = False
        tool.source = "admin"
        await db.commit()
    class Provider:
        server_instructions = None

        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self):
            return [{"name": "read", "inputSchema": {}}, {"name": "new_tool", "inputSchema": {}}]
    with patch("app.services.mcp_refresh_service.MCPClient", Provider):
        async with async_session() as db:
            result = await refresh_mcp_server_tools(db, server.id)
            await db.commit()
    assert result.created == 1
    async with async_session() as db:
        tools = (await db.scalars(select(Tool).where(Tool.mcp_server_id == server.id))).all()
        assert {tool.mcp_tool_name: tool.enabled for tool in tools} == {"read": False, "new_tool": True}


async def _shared_with_existing_private():
    user, agent, server, tid = await _installation()
    async with async_session() as db:
        other = Agent(name=f"Other {uuid.uuid4().hex[:8]}", creator_id=user.id, tenant_id=agent.tenant_id)
        db.add(other)
        await db.flush()
        db.add(AgentTool(agent_id=other.id, tool_id=tid, enabled=True,
                         source="user_installed", installed_by_agent_id=other.id))
        private = MCPServer(
            name=agent_private_server_name(server.display_name, server.base_url_template, agent.id),
            display_name=server.display_name, base_url_template=server.base_url_template,
            tenant_id=server.tenant_id, headers_template={},
        )
        db.add(private)
        await db.flush()
        private_tool = Tool(name=f"mcp_{private.name}_read", display_name="Read", type="mcp",
                            tenant_id=agent.tenant_id, source="agent", enabled=True,
                            mcp_server_id=private.id, mcp_tool_name="read")
        db.add(private_tool)
        await db.commit()
        return user, agent, server, tid, private.id, private_tool.id, other.id


@pytest.mark.parametrize("consumer", ["enterprise", "other_agent", "globally_disabled"])
async def test_existing_private_target_is_authorized_before_provider(consumer):
    user, agent, server, tid, private_id, private_tid, other_id = await _shared_with_existing_private()
    async with async_session() as db:
        if consumer == "enterprise":
            private = await db.get(MCPServer, private_id)
            private.created_by_user_id = user.id
        elif consumer == "other_agent":
            db.add(AgentTool(agent_id=other_id, tool_id=private_tid, enabled=True))
        else:
            tool = await db.get(Tool, private_tid)
            tool.enabled = False
        await db.commit()
    with patch("app.services.mcp_refresh_service.MCPClient") as provider:
        result = json.loads(await refresh_mcp_server(agent.id, server.id, user_id=user.id, session_id=""))
    assert result["ok"] is False
    provider.assert_not_called()
    async with async_session() as db:
        assert await db.scalar(select(AgentTool.id).where(AgentTool.agent_id == agent.id, AgentTool.tool_id == tid))


async def test_existing_private_target_change_discards_discovery_without_moving_bindings():
    user, agent, server, tid, private_id, private_tid, _other_id = await _shared_with_existing_private()

    class Provider:
        server_instructions = None

        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self):
            async with async_session() as db:
                tool = await db.get(Tool, private_tid)
                tool.enabled = False
                await db.commit()
            return [{"name": "new_tool", "inputSchema": {}}]

    with patch("app.services.mcp_refresh_service.MCPClient", Provider):
        result = json.loads(await refresh_mcp_server(agent.id, server.id, user_id=user.id, session_id=""))
    assert result["ok"] is False
    async with async_session() as db:
        assert await db.scalar(select(AgentTool.id).where(AgentTool.agent_id == agent.id, AgentTool.tool_id == tid))
        assert not await db.scalar(select(Tool.id).where(Tool.mcp_server_id == private_id, Tool.mcp_tool_name == "new_tool"))


async def test_project_disable_waiting_on_refresh_disables_new_assignments():
    from app.main import app
    from app.models.project import ProjectCapabilityBinding, ProjectMemberSnapshot
    from app.services import mcp_refresh_service, project_member_runtime
    from test_mcp_refresh_project_references import _add_project_reference, _RefreshingClient

    user, agent, server = await _make_agent()
    project_agent, _tool = await _add_project_reference(user, agent, server, enabled=True)
    async with async_session() as db:
        binding_id = await db.scalar(select(ProjectCapabilityBinding.id).where(
            ProjectCapabilityBinding.capability_id == server.id,
        ))
        db.add(ProjectMemberSnapshot(tenant_id=agent.tenant_id, project_id=project_agent.project_id,
                                     agent_id=project_agent.id, name_snapshot=project_agent.name))
        await db.commit()

    locked, release, disabling = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_plan = mcp_refresh_service.plan_refresh
    original_sync = project_member_runtime.sync_project_capability_assignment
    calls = 0

    async def paused_plan(*args, **kwargs):
        nonlocal calls
        plan = await original_plan(*args, **kwargs)
        calls += 1
        if calls == 2:
            locked.set()
            await release.wait()
        return plan

    async def observed_sync(*args, **kwargs):
        disabling.set()
        return await original_sync(*args, **kwargs)

    async def disable_project():
        token = create_access_token(str(user.id), user.role)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.patch(
                f"/api/projects/{project_agent.project_id}/capabilities/{binding_id}",
                json={"is_enabled": False}, headers={"Authorization": f"Bearer {token}"},
            )

    with patch.object(mcp_refresh_service, "MCPClient", _RefreshingClient), \
            patch.object(mcp_refresh_service, "plan_refresh", paused_plan), \
            patch.object(project_member_runtime, "sync_project_capability_assignment", observed_sync):
        refresh = asyncio.create_task(refresh_mcp_server(agent.id, server.id, user_id=user.id, session_id=""))
        disable = None
        try:
            await asyncio.wait_for(locked.wait(), 5)
            disable = asyncio.create_task(disable_project())
            await asyncio.wait_for(disabling.wait(), 5)
            release.set()
            assert json.loads(await asyncio.wait_for(refresh, 5))["ok"] is True
            response = await asyncio.wait_for(disable, 5)
            assert response.status_code == 200, response.text
        finally:
            release.set()
            tasks = [task for task in (refresh, disable) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with async_session() as db:
        assignments = (await db.scalars(select(AgentTool).where(AgentTool.agent_id == project_agent.id))).all()
        assert len(assignments) == 2
        assert all(assignment.enabled is False for assignment in assignments)
