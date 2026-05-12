"""placeholder_allowlist is a list[str] of allowed placeholder root names."""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import async_session, engine
from app.models.user import User, Identity
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _admin_client():
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"pa_{suffix}", email=f"pa_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="PA", role="platform_admin", is_active=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    token = create_access_token(str(user.id), "platform_admin")
    return user.id, token


async def test_allowlist_roundtrip_create_patch_get():
    _, token = await _admin_client()
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        suffix = uuid.uuid4().hex[:6]
        # Create with allowlist
        resp = await client.post("/api/admin/mcp-servers", headers=headers, json={
            "name": f"srv_{suffix}",
            "display_name": "srv",
            "base_url_template": "https://srv.example",
            "placeholder_allowlist": ["user.email", "agent.name"],
        })
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        assert resp.json()["placeholder_allowlist"] == ["user.email", "agent.name"]

        # Default null when omitted
        resp2 = await client.post("/api/admin/mcp-servers", headers=headers, json={
            "name": f"srv2_{suffix}",
            "display_name": "srv2",
            "base_url_template": "https://srv.example",
        })
        assert resp2.status_code == 201
        assert resp2.json()["placeholder_allowlist"] is None

        # PATCH to update
        resp3 = await client.patch(f"/api/admin/mcp-servers/{sid}", headers=headers, json={
            "placeholder_allowlist": ["tenant.id"],
        })
        assert resp3.status_code == 200, resp3.text
        assert resp3.json()["placeholder_allowlist"] == ["tenant.id"]

        # PATCH with null clears
        resp4 = await client.patch(f"/api/admin/mcp-servers/{sid}", headers=headers, json={
            "placeholder_allowlist": None,
        })
        # null in PATCH is "don't touch" by convention — verify behavior matches what we want
        assert resp4.status_code == 200
        # If "null = don't touch", value stays ["tenant.id"]. If "null = clear", becomes None.
        # We document: null means "don't touch" (consistent with credential_template).
        assert resp4.json()["placeholder_allowlist"] == ["tenant.id"]

        # Empty list explicitly clears the restriction
        resp5 = await client.patch(f"/api/admin/mcp-servers/{sid}", headers=headers, json={
            "placeholder_allowlist": [],
        })
        assert resp5.status_code == 200
        assert resp5.json()["placeholder_allowlist"] == []
