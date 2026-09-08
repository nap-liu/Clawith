"""The online seed/rollback operation preserves every unrelated tool field."""

import copy

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.tool import Tool
from app.scripts.scene_runtime_schema import RUNTIME_FIELDS, synchronize
from app.services.tool_seeder import seed_builtin_tools


@pytest.mark.asyncio
async def test_scene_schema_apply_rollback_is_precise_and_idempotent():
    await engine.dispose()
    await seed_builtin_tools()
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "manage_scene"))
        original_schema = copy.deepcopy(tool.parameters_schema)
        original_flags = (tool.enabled, tool.is_default, tool.config)
    result = await synchronize("rollback")
    assert result["after"] == []
    assert (await synchronize("rollback"))["changed"] is False
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "manage_scene"))
        expected = copy.deepcopy(original_schema)
        for field in RUNTIME_FIELDS:
            expected["properties"].pop(field, None)
        assert tool.parameters_schema == expected
        assert (tool.enabled, tool.is_default, tool.config) == original_flags
    assert (await synchronize("apply"))["after"] == sorted(RUNTIME_FIELDS)
    assert (await synchronize("apply"))["changed"] is False
    assert (await synchronize("status"))["after"] == sorted(RUNTIME_FIELDS)
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "manage_scene"))
        assert tool.parameters_schema == original_schema
        assert (tool.enabled, tool.is_default, tool.config) == original_flags
    await engine.dispose()
