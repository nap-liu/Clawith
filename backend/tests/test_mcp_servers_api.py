"""CRUD + ACL + cred-masking tests for /api/admin/mcp-servers."""
import uuid
import pytest
import httpx
from sqlalchemy import select
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.mcp_server import MCPServer
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_user(role: str) -> tuple[User, str]:
    """Create user with given role; return (user, jwt_token)."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"u_{suffix}",
            email=f"u_{suffix}@x.local",
            password_hash="x",
            is_platform_admin=(role == "platform_admin"),
        )
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role=role, is_active=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        token = create_access_token(str(user.id), role)
        return user, token


@pytest.fixture
async def client():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_requires_platform_admin(client):
    _, member_token = await _make_user("member")
    r = await client.get("/api/admin/mcp-servers", headers={"Authorization": f"Bearer {member_token}"})
    assert r.status_code == 403


async def test_list_returns_servers(client):
    _, admin_token = await _make_user("platform_admin")
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"test_{suffix}",
            display_name=f"Test {suffix}",
            base_url_template=f"https://test-{suffix}.example",
            headers_template={"X": "y"},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    r = await client.get("/api/admin/mcp-servers", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    rows = r.json()
    found = next((row for row in rows if row["id"] == str(srv_id)), None)
    assert found is not None
    assert found["display_name"] == f"Test {suffix}"
    assert found["credential_state"] == "unset"


async def test_create_then_get_detail(client):
    _, admin_token = await _make_user("platform_admin")
    suffix = uuid.uuid4().hex[:6]
    payload = {
        "name": f"created_{suffix}",
        "display_name": "Created Server",
        "base_url_template": f"https://created-{suffix}.test",
        "headers_template": {"H": "v"},
        "credential_template": "secret-key-123",
        "system_prompt_block": "Use citations.",
    }
    r = await client.post(
        "/api/admin/mcp-servers", json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == f"created_{suffix}"
    # Credential masked even on create response
    assert "credential_template" not in body
    assert body["credential_state"] == "set"
    new_id = body["id"]

    # GET detail
    r2 = await client.get(
        f"/api/admin/mcp-servers/{new_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200
    detail = r2.json()
    assert detail["system_prompt_block"] == "Use citations."
    assert detail["credential_state"] == "set"
    assert "credential_template" not in detail


async def test_patch_updates_only_provided_fields(client):
    _, admin_token = await _make_user("platform_admin")
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"patch_{suffix}", display_name="Old", base_url_template="https://old",
            headers_template={"K": "v"}, credential_template="enc-blob",
            system_prompt_block="OLD",
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    r = await client.patch(
        f"/api/admin/mcp-servers/{srv_id}",
        json={"system_prompt_block": "NEW"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["system_prompt_block"] == "NEW"
    assert body["display_name"] == "Old"  # untouched
    assert body["base_url_template"] == "https://old"  # untouched
    assert body["credential_state"] == "set"  # cred not nuked


async def test_delete_404_for_nonexistent(client):
    _, admin_token = await _make_user("platform_admin")
    r = await client.delete(
        f"/api/admin/mcp-servers/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


async def test_delete_cascades_to_overrides(client):
    """Delete server → mcp_server_overrides rows for it should also vanish (FK CASCADE)."""
    from app.models.mcp_server import MCPServerOverride
    _, admin_token = await _make_user("platform_admin")
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(name=f"del_{suffix}", display_name="d",
                        base_url_template="https://d", headers_template={})
        db.add(srv)
        await db.flush()
        db.add(MCPServerOverride(
            mcp_server_id=srv.id, scope_type="agent", scope_id=uuid.uuid4(),
            system_prompt_block="extra",
        ))
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    r = await client.delete(
        f"/api/admin/mcp-servers/{srv_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 204

    async with async_session() as db:
        n = (await db.execute(
            select(MCPServerOverride).where(MCPServerOverride.mcp_server_id == srv_id)
        )).scalars().all()
        assert n == [], "overrides should be cascade-deleted with server"


# ---------------------------------------------------------------------------
# ACL: non-platform_admin must be rejected on all write / action endpoints
# ---------------------------------------------------------------------------

async def test_create_requires_platform_admin(client):
    _, member_token = await _make_user("member")
    r = await client.post(
        "/api/admin/mcp-servers",
        json={"name": "x", "display_name": "x", "base_url_template": "https://x"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code == 403


async def test_patch_requires_platform_admin(client):
    _, member_token = await _make_user("member")
    r = await client.patch(
        f"/api/admin/mcp-servers/{uuid.uuid4()}",
        json={"display_name": "y"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code == 403


async def test_delete_requires_platform_admin(client):
    _, member_token = await _make_user("member")
    r = await client.delete(
        f"/api/admin/mcp-servers/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code == 403


async def test_test_connection_requires_platform_admin(client):
    _, member_token = await _make_user("member")
    r = await client.post(
        f"/api/admin/mcp-servers/{uuid.uuid4()}/test-connection",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Credential safety: PATCH with credential_template=null must not clear value
# ---------------------------------------------------------------------------

async def test_patch_credential_none_does_not_clear(client):
    _, admin_token = await _make_user("platform_admin")
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"keep_{suffix}", display_name="k", base_url_template="https://k",
            headers_template={}, credential_template="must-not-vanish",
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    # Send credential_template: null — should be ignored per schema contract
    r = await client.patch(
        f"/api/admin/mcp-servers/{srv_id}",
        json={"credential_template": None, "display_name": "k2"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["credential_state"] == "set"  # NOT cleared

    async with async_session() as db:
        srv2 = (await db.execute(select(MCPServer).where(MCPServer.id == srv_id))).scalar_one()
        assert srv2.credential_template == "must-not-vanish"
