"""Verify MCP_USE_LEGACY_COLLECTOR routes to pre-P0b code path.

Approach to @lru_cache on get_settings
---------------------------------------
The task spec considers patching ``get_settings`` with unittest.mock. That
works only when the dispatcher (_collect_extension_prompts) calls
``get_settings()`` at invocation time — which it does (line 343 in
agent_context.py reads ``get_settings().MCP_USE_LEGACY_COLLECTOR``).

However, the cleaner pattern already established in this test suite is:
  monkeypatch.setenv(...)       — set env before the import resolves
  get_settings.cache_clear()    — bust the @lru_cache
  <run code>
  get_settings.cache_clear()    — restore clean slate after

This avoids any mock machinery and is consistent with
test_tool_output_store.py, test_message_budget.py, and
test_llm_caller_integration.py.

Because the two assertions live in separate test functions, each function
gets its own fixture scope:
  - test_new_path_ignores_unlinked_tools — env flag NOT set
  - test_legacy_path_reads_unlinked_tools — env flag IS set

A shared module-level agent_id (set during collection) would race between
async setups; instead we create a single agent in a session-scoped fixture
so both tests share the same seeded row.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride  # noqa: F401 — FK side-effect: registers mcp_servers table in metadata
from app.models.tenant import Tenant  # noqa: F401 — FK side-effect import
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _isolate():
    """Dispose async engine before and after each test (prevents connection leaks)."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(scope="module")
async def seeded_agent_id() -> uuid.UUID:
    """Seed a single Identity → User → Agent + Tool(mcp_server_id=None) chain.

    The tool has system_prompt_block set but mcp_server_id IS NULL, which
    represents the *unmigrated* state: only the legacy collector sees it.
    """
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"u_{suffix}",
            email=f"u_{suffix}@x.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id,
            display_name="U",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()

        agent = Agent(name=f"Agent_{suffix}", creator_id=user.id)
        db.add(agent)
        await db.flush()

        tool = Tool(
            name=f"mcp_test_{suffix}",
            display_name="t",
            type="mcp",
            mcp_server_url=f"https://test.example/{suffix}",
            system_prompt_block="LEGACY-BLOCK",
            mcp_server_id=None,  # not linked to mcp_servers → new path ignores it
        )
        db.add(tool)
        await db.flush()

        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        await db.refresh(agent)
        return agent.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_new_path_ignores_unlinked_tools(seeded_agent_id: uuid.UUID):
    """Default (new) path: tool with mcp_server_id=NULL is NOT surfaced.

    The new collector joins against mcp_servers via Tool.mcp_server_id; when
    that column is NULL the tool contributes no MCP prompt block.
    """
    # Make sure the flag is NOT set for this test.
    os.environ.pop("MCP_USE_LEGACY_COLLECTOR", None)

    from app.config import get_settings
    from app.services.agent_context import _collect_extension_prompts

    get_settings.cache_clear()
    try:
        blocks = await _collect_extension_prompts(seeded_agent_id)
    finally:
        get_settings.cache_clear()

    combined = "\n".join(blocks)
    assert "LEGACY-BLOCK" not in combined, (
        f"New path must NOT expose unlinked tool prompt blocks; got: {combined!r}"
    )


async def test_legacy_path_reads_unlinked_tools(seeded_agent_id: uuid.UUID, monkeypatch):
    """Legacy path (MCP_USE_LEGACY_COLLECTOR=1): reads tools.system_prompt_block directly.

    Even though mcp_server_id is NULL the legacy collector reads the column
    from the tools table, so the prompt block IS included.
    """
    monkeypatch.setenv("MCP_USE_LEGACY_COLLECTOR", "1")

    from app.config import get_settings
    from app.services.agent_context import _collect_extension_prompts

    get_settings.cache_clear()
    try:
        blocks = await _collect_extension_prompts(seeded_agent_id)
    finally:
        get_settings.cache_clear()

    combined = "\n".join(blocks)
    assert "LEGACY-BLOCK" in combined, (
        f"Legacy path MUST surface the tool's system_prompt_block; got: {combined!r}"
    )
