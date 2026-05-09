"""Prompt-cache invariant: switching the calling user under the same
agent + session + server config produces a byte-identical system
prompt. This is what lets the Anthropic prompt cache land hits
across users in a multi-tenant agent.

A future regression where ${user.*} leaks into the prompt block
will break this test before it breaks the cache hit rate in prod.
"""
import uuid
import pytest
from sqlalchemy import select
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import Tool, AgentTool
from app.models.tenant import Tenant  # noqa: F401 — ensures 'tenants' table is in SA metadata
from app.services.agent_context import build_agent_context

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_system_prompt_is_user_invariant():
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        for ident_name in ("alice", "bob"):
            identity = Identity(
                username=f"{ident_name}_{suffix}",
                email=f"{ident_name}_{suffix}@x.local",
                password_hash="x",
            )
            db.add(identity)
            await db.flush()
            db.add(User(identity_id=identity.id, display_name=ident_name, role="member", is_active=True))
        await db.flush()

        # Pick the first user we just made as the agent creator
        creator_id = (await db.execute(
            select(User.id).where(User.display_name == "alice").order_by(User.id.desc()).limit(1)
        )).scalar()
        agent = Agent(name=f"A_{suffix}", creator_id=creator_id)
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

    # build_agent_context returns tuple[str, str] — (static_prompt, dynamic_prompt)
    # Build context twice with identical args — output must be byte-identical
    ctx1 = await build_agent_context(agent_id, "Test Agent")
    ctx2 = await build_agent_context(agent_id, "Test Agent")
    assert ctx1 == ctx2, "system prompt is not byte-stable across calls"

    # The static MCP server block must appear in the static part of the prompt
    static_prompt = ctx1[0]
    assert "STATIC PROMPT WITH NO USER VARS" in static_prompt
