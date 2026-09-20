"""Rollback compatibility for the add-media context builtin."""

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.scripts.rollback_add_media_to_ctx import rollback_add_media_to_ctx
from app.services.tool_seeder import seed_builtin_tools
from tests.test_published_page_access import _dispose_engine, _make_restricted_page  # noqa: F401


@pytest.mark.asyncio
async def test_rollback_removes_only_add_media_tool_and_is_idempotent():
    await _make_restricted_page()
    await seed_builtin_tools()
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "add_media_to_ctx"))
        assert tool is not None
        tool_id = tool.id
        assignments_before = len(list(await db.scalars(
            select(AgentTool.id).where(AgentTool.tool_id == tool_id)
        )))
        assert assignments_before > 0

    removed_assignments, removed_tools = await rollback_add_media_to_ctx()
    assert removed_assignments == assignments_before
    assert removed_tools == 1
    assert await rollback_add_media_to_ctx() == (0, 0)

    async with async_session() as db:
        assert await db.get(Tool, tool_id) is None
        assert await db.scalar(select(Tool.id).where(Tool.name == "read_media")) is not None
