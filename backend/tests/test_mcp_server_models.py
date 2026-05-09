"""Smoke + invariants test for MCPServer / MCPServerOverride."""
import uuid
import pytest
from app.database import async_session, engine
from app.models.mcp_server import MCPServer, MCPServerOverride

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_mcp_server_insert_and_read():
    async with async_session() as db:
        srv = MCPServer(
            name=f"ragflow_test_{uuid.uuid4().hex[:6]}",
            display_name="RAGFlow",
            base_url_template="https://rag.example.com/${tenant.id}",
            headers_template={"X-User": "${user.id}"},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        assert srv.id is not None
        assert srv.headers_template == {"X-User": "${user.id}"}


async def test_override_cascade_on_server_delete():
    async with async_session() as db:
        srv = MCPServer(
            name=f"x_{uuid.uuid4().hex[:6]}",
            display_name="x",
            base_url_template="https://x",
            headers_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        ovr = MCPServerOverride(
            mcp_server_id=srv.id,
            scope_type="tenant",
            scope_id=uuid.uuid4(),
            system_prompt_block="extra",
        )
        db.add(ovr)
        await db.commit()
        ovr_id = ovr.id

        await db.delete(srv)
        await db.commit()

        from sqlalchemy import select
        result = await db.execute(select(MCPServerOverride).where(MCPServerOverride.id == ovr_id))
        assert result.scalar_one_or_none() is None
