"""Cross-channel quoted-message sender identity tests."""

from __future__ import annotations

import json
import uuid

import pytest

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.identity import IdentityProvider
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_user_service import channel_user_service
from app.services.quoted_message import (
    render_quoted_message_for_llm,
    resolve_quoted_message_sender,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    yield
    await engine.dispose()


def _quote(sender_ref: str) -> dict:
    return {
        "message_type": "text",
        "content_status": "available",
        "text": "quoted body",
        "attachments": [],
        "sender_ref": sender_ref,
        "sender_name": "untrusted provider nickname",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "channel_type", "id_type"),
    [
        ("dingtalk", "dingtalk", "sender_id"),
        ("feishu", "feishu", "open_id"),
    ],
)
async def test_provider_alias_resolves_to_same_platform_user_across_agents(
    provider_type: str,
    channel_type: str,
    id_type: str,
):
    suffix = uuid.uuid4().hex[:12]
    provider_ref = f"provider-user-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Quote {suffix}", slug=f"quote-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = User(
            tenant_id=tenant.id,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        canonical_user = User(
            tenant_id=tenant.id,
            display_name="Canonical Sender",
            role="member",
            is_active=True,
        )
        db.add_all([creator, canonical_user])
        await db.flush()
        first_agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name="First Robot",
            status="idle",
        )
        second_agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name="Second Robot",
            status="idle",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type=provider_type,
            name=f"Provider {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([first_agent, second_agent, provider])
        await db.flush()

        learned = await channel_user_service.remember_provider_user_alias(
            db,
            provider=provider,
            channel_type=channel_type,
            id_type=id_type,
            subject=provider_ref,
            user=canonical_user,
        )
        resolved = await resolve_quoted_message_sender(
            db,
            _quote(provider_ref),
            provider=provider,
            channel_type=channel_type,
            provider_sender_id_type=id_type,
            agent=second_agent,
        )
        await db.commit()

    assert learned is True
    assert resolved is not None
    assert resolved["sender_status"] == "resolved"
    assert resolved["sender_user_id"] == str(canonical_user.id)
    assert resolved["sender_name"] == "Canonical Sender"
    assert "sender_agent_id" not in resolved
    assert "sender_ref" not in resolved
    assert "_provider_sender_ref" not in resolved

    llm_content = render_quoted_message_for_llm("current body", resolved)
    context = json.loads(llm_content.splitlines()[1])
    assert context["sender"] == {
        "type": "user",
        "id": str(canonical_user.id),
        "name": "Canonical Sender",
    }
    assert provider_ref not in llm_content
    assert "untrusted provider nickname" not in llm_content


@pytest.mark.asyncio
async def test_provider_alias_is_tenant_scoped_and_conflicts_fail_closed():
    suffix = uuid.uuid4().hex[:12]
    shared_ref = f"same-provider-ref-{suffix}"
    async with async_session() as db:
        tenant_a = Tenant(name=f"Tenant A {suffix}", slug=f"quote-a-{suffix}")
        tenant_b = Tenant(name=f"Tenant B {suffix}", slug=f"quote-b-{suffix}")
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        user_a = User(
            tenant_id=tenant_a.id,
            display_name="Sender A",
            role="member",
            is_active=True,
        )
        conflicting_user_a = User(
            tenant_id=tenant_a.id,
            display_name="Wrong Sender A",
            role="member",
            is_active=True,
        )
        user_b = User(
            tenant_id=tenant_b.id,
            display_name="Sender B",
            role="member",
            is_active=True,
        )
        provider_a = IdentityProvider(
            tenant_id=tenant_a.id,
            provider_type="wecom",
            name=f"WeCom A {suffix}",
            config={},
            is_active=True,
        )
        provider_b = IdentityProvider(
            tenant_id=tenant_b.id,
            provider_type="wecom",
            name=f"WeCom B {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([user_a, conflicting_user_a, user_b, provider_a, provider_b])
        await db.flush()

        assert await channel_user_service.remember_provider_user_alias(
            db,
            provider=provider_a,
            channel_type="wecom",
            id_type="user_id",
            subject=shared_ref,
            user=user_a,
        )
        assert await channel_user_service.remember_provider_user_alias(
            db,
            provider=provider_b,
            channel_type="wecom",
            id_type="user_id",
            subject=shared_ref,
            user=user_b,
        )

        resolved_a = await resolve_quoted_message_sender(
            db,
            _quote(shared_ref),
            provider=provider_a,
            channel_type="wecom",
            provider_sender_id_type="user_id",
        )
        resolved_b = await resolve_quoted_message_sender(
            db,
            _quote(shared_ref),
            provider=provider_b,
            channel_type="wecom",
            provider_sender_id_type="user_id",
        )
        assert resolved_a is not None
        assert resolved_a["sender_user_id"] == str(user_a.id)
        assert resolved_b is not None
        assert resolved_b["sender_user_id"] == str(user_b.id)

        assert not await channel_user_service.remember_provider_user_alias(
            db,
            provider=provider_a,
            channel_type="wecom",
            id_type="user_id",
            subject=shared_ref,
            user=conflicting_user_a,
        )

        conflicted = await resolve_quoted_message_sender(
            db,
            _quote(shared_ref),
            provider=provider_a,
            channel_type="wecom",
            provider_sender_id_type="user_id",
        )

    assert conflicted is not None
    assert conflicted["sender_status"] == "unknown"
    assert "sender_user_id" not in conflicted
    assert "sender_name" not in conflicted
    assert json.loads(
        render_quoted_message_for_llm("current body", conflicted).splitlines()[1]
    )["sender"] == {"type": "unknown"}


def test_legacy_provider_sender_reference_is_never_exposed_to_llm():
    provider_ref = "legacy-provider-secret-id"
    rendered = render_quoted_message_for_llm(
        "current body",
        {
            "message_type": "text",
            "content_status": "available",
            "text": "quoted body",
            "sender_ref": provider_ref,
            "sender_name": "provider nickname",
        },
    )

    context = json.loads(rendered.splitlines()[1])
    assert context["sender"] == {"type": "unknown"}
    assert provider_ref not in rendered
    assert "provider nickname" not in rendered


@pytest.mark.asyncio
async def test_provider_self_reference_resolves_to_platform_agent():
    suffix = uuid.uuid4().hex[:12]
    agent_provider_ref = f"bot-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Agent Quote {suffix}", slug=f"agent-quote-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = User(
            tenant_id=tenant.id,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        db.add(creator)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name="Canonical Agent",
            status="idle",
        )
        db.add(agent)
        await db.flush()

        resolved = await resolve_quoted_message_sender(
            db,
            _quote(agent_provider_ref),
            provider=None,
            channel_type="any-im",
            provider_sender_id_type="provider_user_id",
            agent=agent,
            agent_provider_ref=agent_provider_ref,
        )

    assert resolved is not None
    assert resolved["sender_status"] == "resolved"
    assert resolved["sender_agent_id"] == str(agent.id)
    assert resolved["sender_name"] == "Canonical Agent"
    assert "sender_user_id" not in resolved
    assert "sender_ref" not in resolved
    assert "_provider_sender_ref" not in resolved
