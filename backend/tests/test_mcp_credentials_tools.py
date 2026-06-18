"""Tests for MCP agent credentials tools (list/set/delete).

Canonical helpers (_ctx, _seed_tenant, _seed_user, _pat, _seed_agent) cloned from
test_mcp_config_tools.py so this file is fully self-contained.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent_credential import AgentCredential
from app.models.mcp_server import MCPServer  # noqa: F401 (resolves FK metadata)
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


# ── Canonical helpers ─────────────────────────────────────────────────────────

def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant():
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None):
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def _pat(user, scope="write"):
    from app.services.pat_service import issue_pat

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t", scope=scope)
    return token


async def _seed_agent(creator, name="Agent", access_mode="company"):
    from app.models.agent import Agent
    from app.models.participant import Participant

    async with async_session() as db:
        a = Agent(
            name=name,
            creator_id=creator.id,
            tenant_id=creator.tenant_id,
            agent_type="native",
            access_mode=access_mode,
            status="idle",
        )
        db.add(a)
        await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit()
        await db.refresh(a)
        return a


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_set_credential_requires_confirm():
    """Creating a credential without confirm=True must return guidance, not create a row."""
    from app.mcp_server.tools_credentials import set_agent_credential_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)

    out = await set_agent_credential_impl(
        _ctx(token),
        agent=str(agent.id),
        credential_type="cookie",
        platform="example.com",
    )

    # Should return confirm guidance
    assert "confirm=true" in out.lower() or "confirm=True" in out, f"Expected confirm guidance, got: {out}"

    # No row must have been created
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentCredential).where(AgentCredential.agent_id == agent.id)
            )
        ).scalars().all()
    assert len(rows) == 0, "A credential row was created despite confirm=False"


async def test_set_credential_creates():
    """With confirm=True + valid cookies_json, creates a row with encrypted cookies."""
    from app.mcp_server.tools_credentials import set_agent_credential_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)
    cookies_plaintext = '[{"name":"x","value":"y"}]'

    out = await set_agent_credential_impl(
        _ctx(token),
        agent=str(agent.id),
        credential_type="cookie",
        platform="example.com",
        display_name="Test cred",
        cookies_json=cookies_plaintext,
        confirm=True,
    )

    assert "✅" in out, f"Expected success, got: {out}"

    # Exactly one row should exist
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentCredential).where(AgentCredential.agent_id == agent.id)
            )
        ).scalars().all()

    assert len(rows) == 1, f"Expected 1 credential row, got {len(rows)}"
    cred = rows[0]

    # has_cookies flag
    assert bool(cred.cookies_json), "cookies_json should be stored"

    # Stored value must NOT equal plaintext (it's encrypted)
    assert cred.cookies_json != cookies_plaintext, "cookies_json stored in plaintext — encryption not applied"


async def test_list_credentials_hides_cookies():
    """list_agent_credentials_impl must not expose the plaintext cookie value."""
    from app.mcp_server.tools_credentials import list_agent_credentials_impl, set_agent_credential_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)
    cookies_plaintext = '[{"name":"x","value":"y"}]'

    # Create a credential first
    await set_agent_credential_impl(
        _ctx(token),
        agent=str(agent.id),
        credential_type="cookie",
        platform="example.com",
        display_name="Visible cred",
        cookies_json=cookies_plaintext,
        confirm=True,
    )

    # List should include safe fields but NOT the plaintext secret
    out = await list_agent_credentials_impl(_ctx(token), agent=str(agent.id))

    assert "example.com" in out, f"Expected platform in output, got: {out}"
    assert "Visible cred" in out, f"Expected display_name in output, got: {out}"
    # The secret value must not appear
    assert '"y"' not in out and "\"y\"" not in out, f"Plaintext cookie value leaked in listing: {out}"
    # has_cookies flag should indicate presence
    assert "has_cookies" in out.lower() or "True" in out or "有" in out, f"No has_cookies indicator: {out}"


async def test_delete_credential_confirm():
    """Deleting without confirm=True returns guidance; with confirm=True removes the row."""
    from app.mcp_server.tools_credentials import delete_agent_credential_impl, set_agent_credential_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)

    # Create a credential to delete
    create_out = await set_agent_credential_impl(
        _ctx(token),
        agent=str(agent.id),
        credential_type="cookie",
        platform="todelete.com",
        confirm=True,
    )
    assert "✅" in create_out, f"Setup create failed: {create_out}"

    # Fetch the created credential id
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentCredential).where(AgentCredential.agent_id == agent.id)
            )
        ).scalars().all()
    assert len(rows) == 1
    cred_id = str(rows[0].id)

    # Delete without confirm → guidance, row still exists
    out_no_confirm = await delete_agent_credential_impl(
        _ctx(token), agent=str(agent.id), credential_id=cred_id
    )
    assert "confirm=true" in out_no_confirm.lower() or "confirm=True" in out_no_confirm, (
        f"Expected confirm guidance, got: {out_no_confirm}"
    )

    async with async_session() as db:
        still_there = (
            await db.execute(
                select(AgentCredential).where(AgentCredential.id == uuid.UUID(cred_id))
            )
        ).scalar_one_or_none()
    assert still_there is not None, "Row was deleted despite confirm=False"

    # Delete with confirm → success, row gone
    out_confirmed = await delete_agent_credential_impl(
        _ctx(token), agent=str(agent.id), credential_id=cred_id, confirm=True
    )
    assert "✅" in out_confirmed, f"Expected success on confirmed delete, got: {out_confirmed}"

    async with async_session() as db:
        gone = (
            await db.execute(
                select(AgentCredential).where(AgentCredential.id == uuid.UUID(cred_id))
            )
        ).scalar_one_or_none()
    assert gone is None, "Row still exists after confirmed delete"


async def test_credentials_requires_write():
    """set_agent_credential_impl must reject a read-scope PAT."""
    from app.mcp_server.tools_credentials import set_agent_credential_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    read_token = await _pat(user, scope="read")

    out = await set_agent_credential_impl(
        _ctx(read_token),
        agent=str(agent.id),
        credential_type="cookie",
        platform="example.com",
        confirm=True,
    )

    assert "需要 write" in out, f"Expected write-scope error, got: {out}"


async def test_set_credential_accepts_list_cookies():
    """cookies delivered as a native list (the MCP transport form) must be accepted.

    Regression: the str-only signature rejected lists over the real /mcp transport
    (clients/transport deliver JSON arrays as Python lists) — found in prod e2e.
    """
    from app.mcp_server.tools_credentials import set_agent_credential_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)

    out = await set_agent_credential_impl(
        _ctx(token),
        agent=str(agent.id),
        credential_type="cookie",
        platform="list.example",
        cookies_json=[{"name": "k", "value": "secretval"}],  # NATIVE LIST, not a string
        confirm=True,
    )
    assert "✅" in out, f"native list cookies should be accepted, got: {out}"

    async with async_session() as db:
        row = (
            await db.execute(
                select(AgentCredential).where(
                    AgentCredential.agent_id == agent.id,
                    AgentCredential.platform == "list.example",
                )
            )
        ).scalar_one_or_none()

    assert row is not None, "credential row should be created from a list input"
    assert bool(row.cookies_json), "cookies should be stored"
    assert "secretval" not in str(row.cookies_json), "cookies stored in plaintext — encryption not applied"
