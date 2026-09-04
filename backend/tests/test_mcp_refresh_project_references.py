"""MCP refresh behavior when project Agents reference a source Agent server."""

import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.project import Project, ProjectCapabilityBinding
from app.models.tool import AgentTool, Tool
from app.services.agent_mcp_lifecycle import refresh_mcp_server
from mcp_tool_refresh_support import _isolate, _make_agent

pytestmark = pytest.mark.asyncio


async def _add_project_reference(user, source_agent, server, *, enabled=False):
    async with async_session() as db:
        project = Project(
            tenant_id=source_agent.tenant_id,
            owner_user_id=user.id,
            name=f"Project {uuid.uuid4().hex[:8]}",
            description="",
            goal="",
            success_criteria=[],
            visibility="private",
            status="planning",
            settings={},
        )
        db.add(project)
        await db.flush()
        project_agent_id = uuid.uuid4()
        project_agent = Agent(
            id=project_agent_id,
            name=f"Project Agent {uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=source_agent.tenant_id,
            scope="project",
            project_id=project.id,
            source_agent_id=source_agent.id,
            agent_dir=Agent.project_agent_dir(project_agent_id),
        )
        db.add(project_agent)
        seed_tool = Tool(
            name=f"mcp_{server.name}_seed",
            display_name="Seed",
            description="old",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="seed",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(seed_tool)
        await db.flush()
        db.add_all(
            [
                AgentTool(
                    agent_id=source_agent.id,
                    tool_id=seed_tool.id,
                    enabled=True,
                    config={"api_key": "source-assignment-secret"},
                    source="user_installed",
                    installed_by_agent_id=source_agent.id,
                ),
                AgentTool(
                    agent_id=project_agent.id,
                    tool_id=seed_tool.id,
                    enabled=False,
                    config={"project_option": "preserve"},
                    source="user_installed",
                    installed_by_agent_id=project_agent.id,
                ),
                ProjectCapabilityBinding(
                    tenant_id=project.tenant_id,
                    project_id=project.id,
                    capability_type="mcp",
                    capability_id=server.id,
                    capability_name=server.display_name,
                    source="inherited",
                    inherited_from_agent_id=project_agent.id,
                    is_enabled=enabled,
                    scope={},
                    config={},
                ),
            ]
        )
        await db.commit()
        return project_agent, seed_tool


class _RefreshingClient:
    server_instructions = "refreshed"
    captured_api_key = None

    def __init__(self, _server_url, api_key=None, headers=None):
        del headers
        type(self).captured_api_key = api_key

    async def list_tools(self):
        return [
            {
                "name": "seed",
                "description": "updated",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "new_tool",
                "description": "new",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]


async def test_source_refresh_preserves_and_updates_project_reference():
    user, source_agent, server = await _make_agent()
    async with async_session() as db:
        row = await db.get(MCPServer, server.id)
        row.credential_template = "source-server-secret"
        await db.commit()
    project_agent, seed_tool = await _add_project_reference(
        user,
        source_agent,
        server,
        enabled=False,
    )

    with patch("app.services.mcp_refresh_service.MCPClient", _RefreshingClient):
        result = json.loads(
            await refresh_mcp_server(
                source_agent.id,
                server.id,
                user_id=user.id,
                session_id="project-reference-refresh",
            )
        )

    assert result["ok"] is True
    assert result["mcp_server_id"] == str(server.id)
    assert result["created"] == 1
    assert result["assigned"] == 1
    assert _RefreshingClient.captured_api_key == "source-server-secret"

    async with async_session() as db:
        refreshed_tools = (
            await db.execute(
                select(Tool)
                .where(Tool.mcp_server_id == server.id)
                .order_by(Tool.mcp_tool_name)
            )
        ).scalars().all()
        tools_by_name = {tool.mcp_tool_name: tool for tool in refreshed_tools}
        project_assignments = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == project_agent.id,
                    AgentTool.tool_id.in_([tool.id for tool in refreshed_tools]),
                )
            )
        ).scalars().all()
        by_tool_id = {assignment.tool_id: assignment for assignment in project_assignments}

    existing = by_tool_id[seed_tool.id]
    assert existing.enabled is False
    assert existing.config == {"project_option": "preserve"}
    assert existing.installed_by_agent_id == project_agent.id
    new_assignment = by_tool_id[tools_by_name["new_tool"].id]
    assert new_assignment.enabled is False
    assert new_assignment.config == {}
    assert new_assignment.source == "user_installed"
    assert new_assignment.installed_by_agent_id is None


async def test_unrelated_owner_still_blocks_refresh_with_project_reference():
    user, source_agent, server = await _make_agent()
    project_agent, seed_tool = await _add_project_reference(
        user,
        source_agent,
        server,
    )
    async with async_session() as db:
        other_agent = Agent(
            name=f"Other {uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=source_agent.tenant_id,
        )
        db.add(other_agent)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=other_agent.id,
                tool_id=seed_tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=other_agent.id,
            )
        )
        await db.commit()

    with patch("app.services.mcp_refresh_service.MCPClient") as client:
        result = json.loads(
            await refresh_mcp_server(
                source_agent.id,
                server.id,
                user_id=user.id,
                session_id="mixed-owner-refresh",
            )
        )

    assert result["ok"] is False
    assert result["error"] == "agent_refresh_not_isolated"
    client.assert_not_called()
    async with async_session() as db:
        server_ids = (
            await db.execute(
                select(Tool.mcp_server_id)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id.in_([source_agent.id, project_agent.id]))
            )
        ).scalars().all()
    assert set(server_ids) == {server.id}


async def test_project_agent_can_refresh_its_own_mcp():
    user, source_agent, _source_server = await _make_agent()
    async with async_session() as db:
        project = Project(
            tenant_id=source_agent.tenant_id,
            owner_user_id=user.id,
            name=f"Own MCP {uuid.uuid4().hex[:8]}",
            description="",
            goal="",
            success_criteria=[],
            visibility="private",
            status="planning",
            settings={},
        )
        db.add(project)
        await db.flush()
        project_agent_id = uuid.uuid4()
        project_agent = Agent(
            id=project_agent_id,
            name="Project MCP Owner",
            creator_id=user.id,
            tenant_id=source_agent.tenant_id,
            scope="project",
            project_id=project.id,
            source_agent_id=source_agent.id,
            agent_dir=Agent.project_agent_dir(project_agent_id),
        )
        own_server = MCPServer(
            tenant_id=source_agent.tenant_id,
            name=f"project-own-{uuid.uuid4().hex[:8]}",
            display_name="Project Own",
            base_url_template="https://project-own.example/mcp",
            headers_template={},
        )
        db.add_all([project_agent, own_server])
        await db.flush()
        own_tool = Tool(
            name=f"mcp_{own_server.name}_seed",
            display_name="Seed",
            type="mcp",
            category="mcp",
            mcp_server_id=own_server.id,
            mcp_server_name=own_server.name,
            mcp_tool_name="seed",
            source="agent",
            tenant_id=own_server.tenant_id,
        )
        db.add(own_tool)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=project_agent.id,
                tool_id=own_tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=project_agent.id,
            )
        )
        await db.commit()

    with patch("app.services.mcp_refresh_service.MCPClient", _RefreshingClient):
        result = json.loads(
            await refresh_mcp_server(
                project_agent.id,
                own_server.id,
                user_id=user.id,
                session_id="project-owned-refresh",
            )
        )

    assert result["ok"] is True
    assert result["mcp_server_id"] == str(own_server.id)
    assert result["created"] == 1
