"""Tests for MCP agent channel config tools (get/set/delete)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None) -> User:
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


async def _get_channel_config(agent_id, channel: str):
    """Helper: reload ChannelConfig row for assertions."""
    from app.models.channel_config import ChannelConfig

    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == channel,
            )
        )
        return result.scalar_one_or_none()


# ── tests ───────────────────────────────────────────────────────────────────


async def test_set_channel_requires_confirm():
    """set_agent_channel_config_impl without confirm=True returns guidance; no DB row created."""
    from app.mcp_server.tools_channel import set_agent_channel_config_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await set_agent_channel_config_impl(
        _ctx(token), agent=str(agent.id), channel="slack", config={"app_secret": "xyz"}
    )
    # Should return guidance, not success
    assert "confirm" in out.lower()
    assert "✅" not in out

    # No row should have been created
    row = await _get_channel_config(agent.id, "slack")
    assert row is None


async def test_set_channel_creates_encrypted():
    """set with confirm=True creates a ChannelConfig row with encrypted secret.

    We use "api_key" which is in SENSITIVE_FIELD_KEYS and will be encrypted.
    The REST layer stores the encrypted api_key value in the app_secret column.
    """
    from app.mcp_server.tools_channel import set_agent_channel_config_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    plaintext = "my-secret-api-key-xyz"
    out = await set_agent_channel_config_impl(
        _ctx(token), agent=str(agent.id), channel="slack", config={"api_key": plaintext}, confirm=True
    )
    assert "✅" in out

    row = await _get_channel_config(agent.id, "slack")
    assert row is not None
    assert row.is_configured is True
    # api_key must be encrypted (stored in app_secret column, not in plaintext)
    assert row.app_secret != plaintext
    assert row.app_secret is not None


async def test_get_channel_masks_secret():
    """After set, get returns output that does NOT contain the plaintext secret."""
    from app.mcp_server.tools_channel import get_agent_channel_config_impl, set_agent_channel_config_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    # First configure with api_key (a SENSITIVE_FIELD_KEY — gets encrypted)
    await set_agent_channel_config_impl(
        _ctx(token), agent=str(agent.id), channel="slack", config={"api_key": "supersecret123"}, confirm=True
    )

    # Now get — secret must be masked (neither plaintext nor encrypted form should appear)
    out = await get_agent_channel_config_impl(_ctx(token), agent=str(agent.id), channel="slack")
    assert "supersecret123" not in out
    # Should indicate it IS configured
    assert "configured" in out.lower() or "✅" in out or "已配置" in out


async def test_delete_channel_confirm():
    """delete without confirm returns guidance; with confirm=True removes the row."""
    from app.mcp_server.tools_channel import delete_agent_channel_config_impl, set_agent_channel_config_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    # Set up a row first
    await set_agent_channel_config_impl(
        _ctx(token), agent=str(agent.id), channel="dingtalk", config={"app_secret": "abc"}, confirm=True
    )
    assert await _get_channel_config(agent.id, "dingtalk") is not None

    # Delete without confirm → guidance
    out = await delete_agent_channel_config_impl(_ctx(token), agent=str(agent.id), channel="dingtalk")
    assert "confirm" in out.lower()
    assert "✅" not in out
    # Row still present
    assert await _get_channel_config(agent.id, "dingtalk") is not None

    # Delete with confirm → row gone
    out2 = await delete_agent_channel_config_impl(
        _ctx(token), agent=str(agent.id), channel="dingtalk", confirm=True
    )
    assert "✅" in out2
    assert await _get_channel_config(agent.id, "dingtalk") is None


async def test_channel_requires_write():
    """A read-only PAT must be rejected for set_agent_channel_config_impl."""
    from app.mcp_server.tools_channel import set_agent_channel_config_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="read")

    out = await set_agent_channel_config_impl(
        _ctx(token), agent=str(agent.id), channel="feishu", config={"app_secret": "s"}, confirm=True
    )
    assert "write" in out.lower() or "权限" in out
    assert "✅" not in out
