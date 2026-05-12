"""Dry-run accepts draft_overrides and applies them on top of persisted state."""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_draft_overrides_override_persisted_url_and_prompt():
    """Uses synthetic identity so placeholders resolve to known constants:
    PreviewAgent for ${agent.name}, fixed UUIDs for ${tenant.id}, etc.
    The real `_build_user_ctx` doesn't query the DB for agent.name, so we
    can't rely on a real Agent's name resolving through that path here.
    """
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"pa_{suffix}", email=f"pa_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="PA", role="platform_admin", is_active=True)
        db.add(user)
        await db.flush()
        srv = MCPServer(
            name=f"s_{suffix}",
            display_name="s",
            base_url_template="https://persisted.example/mcp",
            system_prompt_block="persisted prompt",
            created_by_user_id=user.id,
        )
        db.add(srv)
        await db.commit()
        server_id = srv.id

    token = create_access_token(str(user.id), "platform_admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Baseline: no draft → persisted values come back, synthetic identity
        resp = await client.post(
            f"/api/admin/mcp-servers/{server_id}/dry-run",
            headers={"Authorization": f"Bearer {token}"},
            json={"identity": "synthetic", "scope": "platform"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["resolved_url"] == "https://persisted.example/mcp"
        assert "persisted prompt" in resp.json()["resolved_prompt"]

        # With draft: resolved values reflect the draft, not persisted
        # Use ${agent.name} which resolves to "PreviewAgent" under synthetic identity
        # (see _SYNTHETIC_CTX in mcp_servers.py).
        resp2 = await client.post(
            f"/api/admin/mcp-servers/{server_id}/dry-run",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "identity": "synthetic",
                "scope": "platform",
                "draft_overrides": {
                    "base_url_template": "https://drafted.example/mcp/${agent.name}",
                    "system_prompt_block": "drafted prompt for ${agent.name}",
                },
            },
        )
        assert resp2.status_code == 200, resp2.text
        body = resp2.json()
        # url replaces entirely; placeholders resolve against synthetic context
        assert body["resolved_url"] == "https://drafted.example/mcp/PreviewAgent", body
        # prompt: draft appended on top of persisted; both visible
        assert "persisted prompt" in body["resolved_prompt"]
        assert "drafted prompt for PreviewAgent" in body["resolved_prompt"]
        assert "draft" in body["used_layers"]
