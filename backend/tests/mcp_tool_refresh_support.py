"""Shared fixtures for MCP tool refresh tests."""

import uuid

import pytest

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_agent() -> tuple[User, Agent, MCPServer]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"T_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"u_{suffix}",
            email=f"u-{suffix}@x.local",
            phone=f"1{int(suffix, 16):010d}",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Refresh Owner",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name=f"A_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        server = MCPServer(
            tenant_id=tenant.id,
            name=f"refresh-{suffix}",
            display_name="Refresh",
            base_url_template="https://global.example/mcp",
            headers_template={},
        )
        db.add(server)
        await db.commit()
        return user, agent, server
