"""Observable active-state checks for established channel bindings."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.dingtalk import process_dingtalk_message
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.channel_user_errors import ChannelUserResolutionError
from app.services.channel_user_service import ChannelUserService


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_binding(
    *,
    identity_active: bool = True,
    user_active: bool = True,
    source_status: str | None = "active",
) -> tuple[uuid.UUID, uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:10]
    subject = f"staff-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Binding State {suffix}", slug=f"binding-state-{suffix}")
        identity = Identity(
            email=f"binding-{suffix}@example.test",
            is_active=identity_active,
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            tenant_id=tenant.id,
            identity_id=identity.id,
            display_name="Bound Sender",
            role="member",
            is_active=user_active,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=user.id,
            name=f"Bound Agent {suffix}",
            status="idle",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name=f"Bound Provider {suffix}",
            is_active=True,
            config={},
        )
        db.add_all([agent, provider])
        await db.flush()
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="dingtalk",
                app_id=f"robot-{suffix}",
                is_configured=True,
                extra_config={"identity_provider_id": str(provider.id)},
            )
        )
        await db.flush()
        scope = await ChannelUserService().resolve_installation_scope(
            db, agent, "dingtalk"
        )
        db.add(
            ChannelUserBinding(
                tenant_id=tenant.id,
                provider_id=provider.id,
                installation_scope=scope,
                channel_type="dingtalk",
                id_type="staff_id",
                subject=subject,
                user_id=user.id,
            )
        )
        if source_status is not None:
            db.add(
                OrgMember(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    external_id=subject,
                    name="Directory Sender",
                    status=source_status,
                    user_id=user.id,
                )
            )
        await db.commit()
        return agent.id, provider.id, subject


@pytest.mark.parametrize(
    ("identity_active", "user_active", "source_status"),
    [
        (False, True, "active"),
        (True, False, "active"),
        (True, True, "inactive"),
    ],
)
async def test_established_binding_rejects_each_inactive_principal_layer(
    identity_active,
    user_active,
    source_status,
):
    agent_id, provider_id, subject = await _seed_binding(
        identity_active=identity_active,
        user_active=user_active,
        source_status=source_status,
    )
    with pytest.raises(ChannelUserResolutionError):
        await process_dingtalk_message(
            agent_id=agent_id,
            sender_staff_id=subject,
            user_text="Message from an inactive sender",
            conversation_id="inactive-sender",
            conversation_type="1",
        )
    async with async_session() as db:
        state = (await db.execute(
            select(Identity.is_active, User.is_active, OrgMember.status)
            .join(User, User.identity_id == Identity.id)
            .join(OrgMember, OrgMember.user_id == User.id)
            .where(OrgMember.provider_id == provider_id)
        )).one()
    assert state == (identity_active, user_active, source_status)


async def test_historical_binding_without_directory_source_remains_usable():
    agent_id, provider_id, subject = await _seed_binding(source_status=None)
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        provider = await db.get(IdentityProvider, provider_id)
        resolved = await ChannelUserService().resolve_channel_user(
            db,
            agent,
            "dingtalk",
            subject,
            {"external_id": subject},
            provider=provider,
        )
    assert resolved.is_active is True


async def test_fresh_provider_observation_reactivates_stale_source_account():
    agent_id, provider_id, subject = await _seed_binding(
        user_active=False,
        source_status="deleted",
    )
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        provider = await db.get(IdentityProvider, provider_id)
        resolved = await ChannelUserService().resolve_channel_user(
            db,
            agent,
            "dingtalk",
            subject,
            {
                "external_id": subject,
                "source_account_active": True,
            },
            provider=provider,
        )
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider_id,
                    OrgMember.external_id == subject,
                )
            )
        ).scalar_one()
        await db.commit()
    assert resolved.is_active is True
    assert member.status == "active"
