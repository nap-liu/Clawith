"""Provider aliases converge into the current installation without splitting users."""

import uuid

import pytest
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_user_service import ChannelUserService


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.parametrize(
    ("channel_type", "id_type"),
    [("dingtalk", "sender_id"), ("slack", "external_id")],
)
@pytest.mark.asyncio
async def test_provider_alias_upgrades_to_current_installation_without_new_user(
    channel_type: str,
    id_type: str,
):
    suffix = uuid.uuid4().hex[:12]
    subject = f"legacy-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Alias {suffix}", slug=f"alias-{suffix}")
        db.add(tenant)
        await db.flush()
        historical_user = User(
            tenant_id=tenant.id,
            display_name="Historical channel user",
            role="member",
            is_active=True,
        )
        db.add(historical_user)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=historical_user.id,
            name=f"Alias Agent {suffix}",
            status="idle",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type=channel_type,
            name=f"Alias Provider {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([agent, provider])
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type=channel_type,
                    app_id=f"app-{suffix}",
                    is_configured=True,
                ),
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    installation_scope=f"provider:{provider.id}",
                    channel_type=channel_type,
                    id_type=id_type,
                    subject=subject,
                    user_id=historical_user.id,
                ),
            ]
        )
        await db.commit()
        tenant_id = tenant.id
        agent_id = agent.id
        provider_id = provider.id
        expected_user_id = historical_user.id

    async with async_session() as db:
        target_agent = await db.get(Agent, agent_id)
        target_provider = await db.get(IdentityProvider, provider_id)
        extra_info = (
            {"sender_id": subject}
            if channel_type == "dingtalk"
            else {"external_id": subject}
        )
        resolved = await ChannelUserService().resolve_channel_user(
            db,
            target_agent,
            channel_type,
            None if channel_type == "dingtalk" else subject,
            extra_info,
            provider=target_provider,
        )
        current_scope = await ChannelUserService().resolve_installation_scope(
            db, target_agent, channel_type
        )
        await db.commit()

    async with async_session() as db:
        assert resolved.id == expected_user_id
        assert await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        ) == 1
        assert await db.scalar(
            select(func.count(OrgMember.id)).where(OrgMember.tenant_id == tenant_id)
        ) == 0
        assert await db.scalar(
            select(func.count(ChannelUserBinding.id)).where(
                ChannelUserBinding.tenant_id == tenant_id,
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.installation_scope == current_scope,
                ChannelUserBinding.channel_type == channel_type,
                ChannelUserBinding.id_type == id_type,
                ChannelUserBinding.subject == subject,
                ChannelUserBinding.user_id == expected_user_id,
            )
        ) == 1


@pytest.mark.asyncio
async def test_enterprise_provider_subject_is_reused_across_dingtalk_robots():
    suffix = uuid.uuid4().hex[:12]
    subject = f"staff-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Rotation {suffix}", slug=f"rotation-{suffix}")
        db.add(tenant)
        await db.flush()
        historical_user = User(
            tenant_id=tenant.id,
            display_name="Historical DingTalk user",
            role="member",
            is_active=True,
        )
        db.add(historical_user)
        await db.flush()
        historical_agent = Agent(
            tenant_id=tenant.id,
            creator_id=historical_user.id,
            name=f"Historical Agent {suffix}",
            status="idle",
        )
        receiving_agent = Agent(
            tenant_id=tenant.id,
            creator_id=historical_user.id,
            name=f"Receiving Agent {suffix}",
            status="idle",
        )
        db.add_all([historical_agent, receiving_agent])
        await db.flush()
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name=f"Rotated Provider {suffix}",
            config={
                "installation_scope": (
                    f"agent:{historical_agent.id}:channel:dingtalk:issuer:old-issuer"
                )
            },
            is_active=True,
        )
        db.add(provider)
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=receiving_agent.id,
                    channel_type="dingtalk",
                    app_id=f"new-app-{suffix}",
                    is_configured=True,
                ),
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    installation_scope=(
                        f"agent:{historical_agent.id}:channel:dingtalk:issuer:old-issuer"
                    ),
                    channel_type="dingtalk",
                    id_type="staff_id",
                    subject=subject,
                    user_id=historical_user.id,
                ),
            ]
        )
        await db.commit()
        agent_id = receiving_agent.id
        provider_id = provider.id
        tenant_id = tenant.id
        expected_user_id = historical_user.id

    async with async_session() as db:
        target_agent = await db.get(Agent, agent_id)
        service = ChannelUserService()
        resolved = await service.resolve_channel_user(
            db,
            target_agent,
            "dingtalk",
            subject,
            {"external_id": subject},
        )
        current_scope = await service.resolve_installation_scope(
            db, target_agent, "dingtalk"
        )
        await db.commit()

    async with async_session() as db:
        assert resolved.id == expected_user_id
        assert await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        ) == 1
        assert await db.scalar(
            select(func.count(ChannelUserBinding.id)).where(
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.installation_scope == current_scope,
                ChannelUserBinding.id_type == "staff_id",
                ChannelUserBinding.subject == subject,
                ChannelUserBinding.user_id == expected_user_id,
            )
        ) == 1
