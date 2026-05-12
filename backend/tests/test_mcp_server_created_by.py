"""MCPServer.created_by_user_id is set on creation and surfaced in GET."""
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


async def _make_platform_admin() -> tuple[uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"pa_{suffix}", email=f"pa_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="PA", role="platform_admin", is_active=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user.id, create_access_token(str(user.id), "platform_admin")


async def test_create_then_get_returns_created_by_user_id():
    user_id, token = await _make_platform_admin()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        suffix = uuid.uuid4().hex[:6]
        resp = await client.post(
            "/api/admin/mcp-servers",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": f"srv_{suffix}",
                "display_name": "srv",
                "base_url_template": "https://srv.example",
            },
        )
        assert resp.status_code == 201, resp.text
        server = resp.json()
        assert server["created_by_user_id"] == str(user_id), \
            f"expected creator user id in response, got {server.get('created_by_user_id')!r}"

        # Round-trip GET also surfaces it
        resp2 = await client.get(
            f"/api/admin/mcp-servers/{server['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["created_by_user_id"] == str(user_id)
