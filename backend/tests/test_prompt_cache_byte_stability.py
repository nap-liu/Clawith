"""Prompt-cache invariant: switching the calling user under the same
agent + session + server config produces a byte-identical system
prompt. This is what lets the Anthropic prompt cache land hits
across users in a multi-tenant agent.

A future regression where ${user.*} leaks into the prompt block
will break this test before it breaks the cache hit rate in prod.
"""
import uuid
from datetime import datetime, timezone

import pytest
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import Tool, AgentTool
from app.models.tenant import Tenant
from app.services.agent_context import build_agent_context

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_system_prompt_is_user_invariant(monkeypatch):
    frozen_now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.services.timezone_utils.now_in_timezone", lambda _tz: frozen_now
    )
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        tenant = Tenant(name="Prompt cache test", slug=f"cache_{suffix}")
        db.add(tenant)
        await db.flush()
        users = []
        for ident_name in ("alice", "bob"):
            identity = Identity(
                username=f"{ident_name}_{suffix}",
                email=f"{ident_name}_{suffix}@x.local",
                password_hash="x",
            )
            db.add(identity)
            await db.flush()
            user = User(
                identity_id=identity.id, display_name=ident_name, role="member",
                is_active=True, tenant_id=tenant.id,
            )
            db.add(user)
            users.append(user)
        await db.flush()

        agent = Agent(name=f"A_{suffix}", creator_id=users[0].id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"srv_{suffix}", display_name="srv",
            base_url_template="https://srv", headers_template={},
            system_prompt_block="STATIC PROMPT WITH NO USER VARS",
        )
        db.add(srv)
        await db.flush()
        tool = Tool(
            name=f"mcp_{suffix}", display_name="t", type="mcp",
            mcp_server_url="https://srv", mcp_server_id=srv.id,
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id

    contexts = [
        await build_agent_context(
            agent_id, "Test Agent",
            current_user_name=user.display_name, current_user_id=user.id,
        )
        for user in users
    ]
    assert contexts[0][0].encode("utf-8") == contexts[1][0].encode("utf-8")
    assert contexts[0][1] != contexts[1][1]
    for index, (static_prompt, dynamic_prompt) in enumerate(contexts):
        assert str(users[index].id) in dynamic_prompt
        assert users[index].display_name in dynamic_prompt
        assert str(users[1 - index].id) not in dynamic_prompt
        assert str(users[index].id) not in static_prompt

    # The static MCP server block must appear in the static part of the prompt
    static_prompt = contexts[0][0]
    assert "STATIC PROMPT WITH NO USER VARS" in static_prompt
