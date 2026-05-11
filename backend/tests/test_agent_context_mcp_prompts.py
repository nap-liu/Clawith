"""Integration: agent + mcp_server + tenant override + agent override
→ _collect_extension_prompts returns the three-layer append in order.
"""
import uuid
import pytest
from app.database import async_session, engine
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tool import Tool, AgentTool
from app.services.agent_context import _collect_extension_prompts

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_three_layer_prompts_appended_in_order():
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        tenant = Tenant(name=f"T_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role="member", is_active=True, tenant_id=tenant.id)
        db.add(user)
        await db.flush()
        agent = Agent(name=f"A_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"srv_{suffix}",
            display_name="srv",
            base_url_template="https://srv.example",
            headers_template={},
            system_prompt_block="LAYER-PLATFORM",
        )
        db.add(srv)
        await db.flush()

        db.add(MCPServerOverride(
            mcp_server_id=srv.id, scope_type="tenant", scope_id=tenant.id,
            system_prompt_block="LAYER-TENANT",
        ))
        db.add(MCPServerOverride(
            mcp_server_id=srv.id, scope_type="agent", scope_id=agent.id,
            system_prompt_block="LAYER-AGENT",
        ))

        tool = Tool(
            name=f"mcp_t_{suffix}", display_name="t", type="mcp",
            mcp_server_url="https://srv.example", mcp_server_id=srv.id,
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()

        agent_id = agent.id

    blocks = await _collect_extension_prompts(agent_id)
    merged = "\n\n".join(blocks)
    # Order check
    assert merged.index("LAYER-PLATFORM") < merged.index("LAYER-TENANT") < merged.index("LAYER-AGENT")
    # Single block joined with blank line within the per-server group
    expected_block = "LAYER-PLATFORM\n\nLAYER-TENANT\n\nLAYER-AGENT"
    assert expected_block in merged


async def test_skips_empty_override_layer():
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name=f"A_{suffix}", creator_id=user.id)
        db.add(agent)
        await db.flush()
        srv = MCPServer(
            name=f"srv_{suffix}", display_name="srv",
            base_url_template="https://srv", headers_template={},
            system_prompt_block="ONLY-PLATFORM",
        )
        db.add(srv)
        await db.flush()
        db.add(MCPServerOverride(
            mcp_server_id=srv.id, scope_type="agent", scope_id=agent.id,
            system_prompt_block=None,  # explicitly empty
        ))
        tool = Tool(name=f"mcp_t_{suffix}", display_name="t", type="mcp",
                    mcp_server_url="https://srv", mcp_server_id=srv.id)
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id

    blocks = await _collect_extension_prompts(agent_id)
    merged = "\n\n".join(blocks)
    assert "ONLY-PLATFORM" in merged
    # No LAYER-AGENT garbage from the empty override
    assert "LAYER-AGENT" not in merged
    assert "LAYER-TENANT" not in merged
