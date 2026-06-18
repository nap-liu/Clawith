"""Tests for MCP file read/write tools (read_agent_file / write_agent_file)."""
from __future__ import annotations

import uuid
import pytest
from pathlib import Path
from types import SimpleNamespace

from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.models.mcp_server import MCPServer  # noqa: F401  (resolve Tool.mcp_server_id FK metadata)


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose(); yield; await engine.dispose()


def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant():
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t); await db.commit(); await db.refresh(t); return t


async def _seed_user(tenant_id=None):
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u); await db.commit(); await db.refresh(u); return u


async def _pat(user, scope="write"):
    from app.services.pat_service import issue_pat
    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t", scope=scope)
    return token


async def _seed_agent(creator, name="Agent", access_mode="company"):
    from app.models.agent import Agent
    from app.models.participant import Participant
    async with async_session() as db:
        a = Agent(name=name, creator_id=creator.id, tenant_id=creator.tenant_id,
                  agent_type="native", access_mode=access_mode, status="idle")
        db.add(a); await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit(); await db.refresh(a); return a


async def test_read_agent_file_reads_soul():
    """Read soul.md that was seeded at the agent base dir (same as agent_manager._agent_dir)."""
    from app.mcp_server.tools_files import read_agent_file_impl
    from app.api.files import _agent_base_dir

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="read")

    # Seed the soul.md at _agent_base_dir / "soul.md" — same as agent_manager._agent_dir
    base = _agent_base_dir(agent.id)
    base.mkdir(parents=True, exist_ok=True)
    soul_path = base / "soul.md"
    soul_path.write_text("# Soul\nhello world", encoding="utf-8")

    out = await read_agent_file_impl(_ctx(token), agent=str(agent.id), path="soul.md")
    assert "hello world" in out


async def test_write_agent_file_new_no_confirm():
    """Writing a brand-new file requires no confirm and returns ✅."""
    from app.mcp_server.tools_files import read_agent_file_impl, write_agent_file_impl
    from app.api.files import _agent_base_dir

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    base = _agent_base_dir(agent.id)
    base.mkdir(parents=True, exist_ok=True)

    out = await write_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md", content="v1")
    assert "✅" in out, f"Expected ✅ in: {out}"

    # Read it back to confirm the content was written
    read_out = await read_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md")
    assert "v1" in read_out


async def test_write_agent_file_overwrite_needs_confirm():
    """Overwriting an existing file requires confirm=True; without it returns guidance."""
    from app.mcp_server.tools_files import read_agent_file_impl, write_agent_file_impl
    from app.api.files import _agent_base_dir

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    base = _agent_base_dir(agent.id)
    base.mkdir(parents=True, exist_ok=True)

    # Write v1 first (no confirm needed — new file)
    out1 = await write_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md", content="v1")
    assert "✅" in out1

    # Try to overwrite WITHOUT confirm → should get guidance, not ✅
    out2 = await write_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md", content="v2")
    assert "confirm=true" in out2.lower() or "confirm=True" in out2
    # File should still be v1
    read_after_noop = await read_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md")
    assert "v1" in read_after_noop

    # Overwrite WITH confirm=True → should succeed and embed BEFORE content
    out3 = await write_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md", content="v2", confirm=True)
    assert "✅" in out3, f"Expected ✅ in: {out3}"
    # The response should embed the old content for rollback
    assert "v1" in out3, f"Expected prior content (v1) embedded in: {out3}"

    # File should now be v2
    read_final = await read_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md")
    assert "v2" in read_final


async def test_write_agent_file_requires_write_scope():
    """A read-scope PAT should be rejected for write operations."""
    from app.mcp_server.tools_files import write_agent_file_impl
    from app.api.files import _agent_base_dir

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="read")

    base = _agent_base_dir(agent.id)
    base.mkdir(parents=True, exist_ok=True)

    out = await write_agent_file_impl(_ctx(token), agent=str(agent.id), path="notes.md", content="x")
    assert "需要 write" in out, f"Expected write-scope error in: {out}"
