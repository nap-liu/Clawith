"""Run the actual MCP data migration against isolated PostgreSQL state."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine
from app.models import registry  # noqa: F401
from app.models.mcp_server import MCPServer
from app.models.tenant import Tenant
from app.models.tool import Tool


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


def _upgrade(connection):
    path = Path(__file__).parents[1] / "alembic/versions/20260509_mcp_servers_data_migration.py"
    spec = importlib.util.spec_from_file_location("mcp_data_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocks, expected", [(["SAME", "SAME"], "SAME"), (["ALPHA", "ZULU"], "ZULU"), ([None, None], None)])
async def test_actual_migration_groups_by_tenant_and_is_repeatable(blocks, expected):
    suffix = uuid.uuid4().hex
    url = f"https://provider.example.test/{suffix}"
    async with AsyncSession(engine) as db:
        tenants = [Tenant(name=f"Migration {n}", slug=f"migration-{suffix}-{n}") for n in range(2)]
        db.add_all(tenants)
        await db.flush()
        tools = [
            Tool(
                name=f"migration_{suffix}_{n}", display_name="Migration tool", type="mcp",
                mcp_server_url=url, system_prompt_block=block,
                tenant_id=tenants[0 if n < 2 else 1].id,
                mcp_server_instructions="Provider instructions",
            )
            for n, block in enumerate([*blocks, "OTHER_TENANT"])
        ]
        db.add_all(tools)
        await db.flush()
        connection = await db.connection()
        await connection.run_sync(_upgrade)
        query = select(MCPServer).where(MCPServer.base_url_template == url)
        servers = {row.tenant_id: row for row in (await db.scalars(query)).all()}
        assert len(servers) == 2
        assert servers[tenants[0].id].system_prompt_block == expected
        assert servers[tenants[1].id].system_prompt_block == "OTHER_TENANT"
        for tool in tools:
            await db.refresh(tool)
            assert tool.mcp_server_id == servers[tool.tenant_id].id
            assert servers[tool.tenant_id].instructions == "Provider instructions"
        original_ids = {row.id for row in servers.values()}
        await connection.run_sync(_upgrade)
        assert {row.id for row in (await db.scalars(query)).all()} == original_ids
        # Session close rolls back the fixture and migration together.
