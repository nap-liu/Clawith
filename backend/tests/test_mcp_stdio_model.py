# backend/tests/test_mcp_stdio_model.py
import uuid
import pytest
from sqlalchemy.exc import IntegrityError
from app.database import async_session, engine
from app.models.user import User, Identity  # noqa: F401 — needed for FK resolution
from app.models.mcp_server import MCPServer

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_mcpserver_stdio_fields_roundtrip():
    s = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"yx_{s}", display_name="yx",
            base_url_template="",            # stdio 不用 url
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "alibabacloud-devops-mcp-server"],
            env_template={"YUNXIAO_ACCESS_TOKEN": "${agent.yunxiao_token}"},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        assert srv.transport == "stdio"
        assert srv.command_template == "npx"
        assert srv.args_template == ["-y", "alibabacloud-devops-mcp-server"]
        assert srv.env_template["YUNXIAO_ACCESS_TOKEN"] == "${agent.yunxiao_token}"


async def test_mcpserver_defaults_http():
    s = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(name=f"h_{s}", display_name="h",
                        base_url_template="https://x/mcp", headers_template={})
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        assert srv.transport == "http"          # 默认
        assert srv.command_template is None


async def test_mcpserver_bogus_transport_violates_check_constraint():
    """Inserting transport='bogus' must raise IntegrityError (CHECK constraint)."""
    s = uuid.uuid4().hex[:6]
    with pytest.raises(IntegrityError):
        async with async_session() as db:
            srv = MCPServer(
                name=f"bad_{s}", display_name="bad",
                base_url_template="", headers_template={},
                transport="bogus",
            )
            db.add(srv)
            await db.commit()
