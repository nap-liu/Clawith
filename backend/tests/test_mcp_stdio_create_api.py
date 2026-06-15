"""FIX 1 — create_mcp_server POST must persist stdio fields.
FIX 2 — MCPServerOverrideOut must include stdio fields.
FIX 3 — MCPServerOut.env_template must mask literal secrets.

These tests are written to FAIL before the corresponding fixes are applied.
"""
from __future__ import annotations

import uuid
import datetime
import pytest
import httpx

from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.core.security import create_access_token
from app.schemas.mcp_server import MCPServerOut, MCPServerOverrideOut

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_admin_token() -> str:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"adm_{suffix}", email=f"adm_{suffix}@x.local",
            password_hash="x", is_platform_admin=True,
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id, display_name="Admin",
            role="platform_admin", is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return create_access_token(str(user.id), "platform_admin")


@pytest.fixture
async def client():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# FIX 1: POST /api/admin/mcp-servers must persist transport + stdio fields
# ---------------------------------------------------------------------------

async def test_create_stdio_server_persists_fields(client):
    """POST with transport=stdio must persist command/args/env/transport in the DB.

    FAILS before FIX 1 because create_mcp_server does not pass these fields
    to MCPServer(...)."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    r = await client.post(
        "/api/admin/mcp-servers",
        json={
            "name": f"yx_{suffix}",
            "display_name": "Yunxiao",
            "base_url_template": "",
            "transport": "stdio",
            "command_template": "npx",
            "args_template": ["-y", "alibabacloud-devops-mcp-server"],
            "env_template": {"YUNXIAO_ACCESS_TOKEN": "${agent.yunxiao_token}"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    # Response must echo the stdio fields back
    assert body["transport"] == "stdio", f"transport not in response: {body}"
    assert body["command_template"] == "npx"
    assert body["args_template"] == ["-y", "alibabacloud-devops-mcp-server"]
    assert body["env_template"] == {"YUNXIAO_ACCESS_TOKEN": "${agent.yunxiao_token}"}

    server_id = body["id"]

    # GET must also return them (DB persisted, not just response-time injection)
    r2 = await client.get(
        f"/api/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["transport"] == "stdio"
    assert body2["command_template"] == "npx"
    assert body2["args_template"] == ["-y", "alibabacloud-devops-mcp-server"]
    assert body2["env_template"] == {"YUNXIAO_ACCESS_TOKEN": "${agent.yunxiao_token}"}


async def test_create_stdio_server_db_row_has_fields(client):
    """Direct DB check: MCPServer row must have transport/command/args/env set.

    FAILS before FIX 1."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    r = await client.post(
        "/api/admin/mcp-servers",
        json={
            "name": f"zx_{suffix}",
            "display_name": "ZX",
            "base_url_template": "",
            "transport": "stdio",
            "command_template": "uvx",
            "args_template": ["mcp-server-git"],
            "env_template": {"GH_TOKEN": "literal-secret"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    server_id = r.json()["id"]

    async with async_session() as db:
        from sqlalchemy import select
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.id == uuid.UUID(server_id))
        )).scalar_one()
        assert srv.transport == "stdio"
        assert srv.command_template == "uvx"
        assert srv.args_template == ["mcp-server-git"]
        assert srv.env_template == {"GH_TOKEN": "literal-secret"}


# ---------------------------------------------------------------------------
# FIX 2: MCPServerOverrideOut must include command_template/args_template/env_template
# ---------------------------------------------------------------------------

async def test_override_out_includes_stdio_fields():
    """MCPServerOverrideOut.from_orm_model must include stdio fields.

    FAILS before FIX 2 because MCPServerOverrideOut has no such fields."""
    # Build a minimal fake ORM object
    class FakeOverride:
        id = uuid.uuid4()
        mcp_server_id = uuid.uuid4()
        scope_type = "agent"
        scope_id = uuid.uuid4()
        system_prompt_block = None
        url_template = None
        headers_template = None
        credential_template = None
        last_modified_by_user_id = None
        created_at = datetime.datetime.now(datetime.timezone.utc)
        updated_at = datetime.datetime.now(datetime.timezone.utc)
        command_template = "npx"
        args_template = ["-y", "pkg"]
        env_template = {"YUNXIAO_ACCESS_TOKEN": "${agent.tok}"}

    out = MCPServerOverrideOut.from_orm_model(FakeOverride())
    assert hasattr(out, "command_template"), "MCPServerOverrideOut missing command_template"
    assert out.command_template == "npx"
    assert hasattr(out, "args_template"), "MCPServerOverrideOut missing args_template"
    assert out.args_template == ["-y", "pkg"]
    assert hasattr(out, "env_template"), "MCPServerOverrideOut missing env_template"
    assert out.env_template == {"YUNXIAO_ACCESS_TOKEN": "${agent.tok}"}


async def test_override_out_missing_fields_are_none_by_default():
    """Override without stdio fields should return None for those fields."""
    class FakeOverride:
        id = uuid.uuid4()
        mcp_server_id = uuid.uuid4()
        scope_type = "tenant"
        scope_id = uuid.uuid4()
        system_prompt_block = None
        url_template = None
        headers_template = None
        credential_template = None
        last_modified_by_user_id = None
        created_at = datetime.datetime.now(datetime.timezone.utc)
        updated_at = datetime.datetime.now(datetime.timezone.utc)
        # No stdio fields

    out = MCPServerOverrideOut.from_orm_model(FakeOverride())
    assert getattr(out, "command_template", "MISSING") is None
    assert getattr(out, "args_template", "MISSING") is None
    assert getattr(out, "env_template", "MISSING") is None


# ---------------------------------------------------------------------------
# FIX 3: MCPServerOut.env_template must mask literal secrets
# ---------------------------------------------------------------------------

def test_env_template_masks_literal_secret():
    """Literal secret in env_template must be masked to '***'.

    FAILS before FIX 3 because from_orm_model returns env_template as-is."""
    class FakeServer:
        id = uuid.uuid4()
        tenant_id = None
        name = "yx"
        display_name = "yx"
        base_url_template = ""
        headers_template = {}
        credential_template = None
        system_prompt_block = None
        placeholder_allowlist = None
        instructions = None
        instructions_captured_at = None
        created_by_user_id = None
        created_at = datetime.datetime.now(datetime.timezone.utc)
        updated_at = datetime.datetime.now(datetime.timezone.utc)
        transport = "stdio"
        command_template = "npx"
        args_template = ["-y", "pkg"]
        env_template = {
            "YUNXIAO_ACCESS_TOKEN": "pt-literal-secret",
            "REGION": "cn",
            "API_KEY": "${agent.k}",
        }

    out = MCPServerOut.from_orm_model(FakeServer())
    assert out.env_template is not None
    # Literal secret must be masked
    assert out.env_template["YUNXIAO_ACCESS_TOKEN"] == "***", (
        f"Expected '***', got {out.env_template['YUNXIAO_ACCESS_TOKEN']!r}"
    )
    # Non-sensitive key must pass through
    assert out.env_template["REGION"] == "cn"
    # Placeholder value must NOT be masked (contains '${')
    assert out.env_template["API_KEY"] == "${agent.k}"


def test_env_template_masks_password_key():
    """Keys matching PASSWORD/SECRET/AUTH must be masked when value is literal."""
    class FakeServer:
        id = uuid.uuid4()
        tenant_id = None
        name = "s"
        display_name = "s"
        base_url_template = ""
        headers_template = {}
        credential_template = None
        system_prompt_block = None
        placeholder_allowlist = None
        instructions = None
        instructions_captured_at = None
        created_by_user_id = None
        created_at = datetime.datetime.now(datetime.timezone.utc)
        updated_at = datetime.datetime.now(datetime.timezone.utc)
        transport = "stdio"
        command_template = "npx"
        args_template = []
        env_template = {
            "DB_PASSWORD": "hunter2",
            "CLIENT_SECRET": "mysecret",
            "AUTH_TOKEN": "bearer-xyz",
            "PUBLIC_URL": "https://example.com",
            "DB_PASSWORD_TEMPLATE": "${agent.dbpw}",
        }

    out = MCPServerOut.from_orm_model(FakeServer())
    assert out.env_template["DB_PASSWORD"] == "***"
    assert out.env_template["CLIENT_SECRET"] == "***"
    assert out.env_template["AUTH_TOKEN"] == "***"
    assert out.env_template["PUBLIC_URL"] == "https://example.com"
    # Placeholder value: must not be masked even if key is sensitive-looking
    assert out.env_template["DB_PASSWORD_TEMPLATE"] == "${agent.dbpw}"


def test_env_template_none_passthrough():
    """None env_template returns None (not an error)."""
    class FakeServer:
        id = uuid.uuid4()
        tenant_id = None
        name = "s"
        display_name = "s"
        base_url_template = ""
        headers_template = {}
        credential_template = None
        system_prompt_block = None
        placeholder_allowlist = None
        instructions = None
        instructions_captured_at = None
        created_by_user_id = None
        created_at = datetime.datetime.now(datetime.timezone.utc)
        updated_at = datetime.datetime.now(datetime.timezone.utc)
        transport = "http"
        command_template = None
        args_template = None
        env_template = None

    out = MCPServerOut.from_orm_model(FakeServer())
    assert out.env_template is None
