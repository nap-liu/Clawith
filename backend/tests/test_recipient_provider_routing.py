"""Outbound routes honor the provider selected by the Agent channel config."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentRelationship,
    ChannelUserBinding,
    OrgMember,
    RelationshipSuppression,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_user_service import ChannelUserService
from app.services.dingtalk_group_mentions import prepare_group_user_mentions
from app.services.recipient_resolver import (
    RecipientResolutionError,
    list_human_recipient_channels,
    load_human_recipient_profiles,
    resolve_group_mention_recipient,
    resolve_human_channel_recipient,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_routes(*, exact_provider: bool, include_selected: bool):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Outbound {suffix}", slug=f"outbound-{suffix}")
        db.add(tenant)
        await db.flush()
        owner = User(
            tenant_id=tenant.id,
            display_name="Owner",
            role="member",
            is_active=True,
        )
        target = User(
            tenant_id=tenant.id,
            display_name="Recipient",
            role="member",
            is_active=True,
        )
        db.add_all([owner, target])
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Outbound Agent {suffix}",
            status="idle",
        )
        selected = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name="Selected DingTalk",
            config={},
            is_active=True,
        )
        other = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name="Other DingTalk",
            config={},
            is_active=True,
        )
        db.add_all([agent, selected, other])
        await db.flush()
        config = ChannelConfig(
            agent_id=agent.id,
            channel_type="dingtalk",
            app_id=f"shared-installation-{suffix}",
            is_configured=True,
            extra_config=(
                {"identity_provider_id": str(selected.id)}
                if exact_provider
                else {}
            ),
        )
        db.add(config)
        await db.flush()
        scope = await ChannelUserService().resolve_installation_scope(
            db, agent, "dingtalk"
        )

        provider_subjects = [(other, f"other-{suffix}")]
        if include_selected:
            provider_subjects.append((selected, f"selected-{suffix}"))
        members = []
        for provider, subject in provider_subjects:
            member = OrgMember(
                tenant_id=tenant.id,
                provider_id=provider.id,
                user_id=target.id,
                external_id=subject,
                name=provider.name,
                status="active",
            )
            members.append(member)
            db.add(member)
            db.add(
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    installation_scope=scope,
                    channel_type="dingtalk",
                    id_type="staff_id",
                    subject=subject,
                    user_id=target.id,
                )
            )
        await db.flush()
        relationship = AgentRelationship(
            agent_id=agent.id,
            user_id=target.id,
            member_id=members[0].id,
            relation="collaborator",
        )
        db.add(relationship)
        await db.commit()
        return agent.id, target.id, selected.id, other.id, relationship.id


async def test_exact_provider_config_never_routes_same_scope_other_provider():
    agent_id, target_id, selected_id, _other_id, relationship_id = (
        await _seed_routes(exact_provider=True, include_selected=True)
    )
    async with async_session() as db:
        route = await resolve_human_channel_recipient(
            db, agent_id, target_id, channel="dingtalk"
        )
        source = await db.get(Agent, agent_id)
        relationship = await db.get(AgentRelationship, relationship_id)
        profiles = await load_human_recipient_profiles(db, source, [relationship])
    assert route.member.provider_id == selected_id
    assert profiles[target_id].channels == ("dingtalk",)


async def test_exact_provider_config_does_not_fall_back_to_other_provider():
    agent_id, target_id, _selected_id, _other_id, relationship_id = (
        await _seed_routes(exact_provider=True, include_selected=False)
    )
    async with async_session() as db:
        with pytest.raises(RecipientResolutionError) as exc_info:
            await resolve_human_channel_recipient(
                db, agent_id, target_id, channel="dingtalk"
            )
        source = await db.get(Agent, agent_id)
        relationship = await db.get(AgentRelationship, relationship_id)
        profiles = await load_human_recipient_profiles(db, source, [relationship])
    assert exc_info.value.code == "channel_unavailable"
    assert profiles[target_id].channels == ()


async def test_legacy_config_without_provider_keeps_scoped_binding_route():
    agent_id, target_id, _selected_id, other_id, _relationship_id = (
        await _seed_routes(exact_provider=False, include_selected=False)
    )
    async with async_session() as db:
        route = await resolve_human_channel_recipient(
            db, agent_id, target_id, channel="dingtalk"
        )
        channels = await list_human_recipient_channels(db, agent_id, target_id)
    assert route.member.provider_id == other_id
    assert channels == ["dingtalk"]


async def test_group_mention_route_does_not_require_relationship():
    agent_id, target_id, selected_id, _other_id, _relationship_id = (
        await _seed_routes(exact_provider=True, include_selected=True)
    )
    async with async_session() as db:
        db.add(
            RelationshipSuppression(
                agent_id=agent_id,
                target_type="user",
                target_id=target_id,
            )
        )
        await db.execute(
            delete(AgentRelationship).where(
                AgentRelationship.agent_id == agent_id,
                AgentRelationship.user_id == target_id,
            )
        )
        await db.commit()

    async with async_session() as db:
        route = await resolve_group_mention_recipient(db, agent_id, target_id)
        target_ids, target_names = await prepare_group_user_mentions(
            db,
            agent_id=agent_id,
            canonical_user_ids=[str(target_id)],
        )
        with pytest.raises(RecipientResolutionError) as proactive_error:
            await resolve_human_channel_recipient(
                db,
                agent_id,
                target_id,
                channel="dingtalk",
            )

    assert route.member.provider_id == selected_id
    assert target_ids == [route.member.external_id]
    assert target_names == ["Recipient"]
    assert proactive_error.value.code == "recipient_not_related"


async def test_group_mention_route_rejects_cross_tenant_user():
    agent_id, _target_id, _selected_id, _other_id, _relationship_id = (
        await _seed_routes(exact_provider=True, include_selected=True)
    )
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        other_tenant = Tenant(
            name=f"Other {suffix}",
            slug=f"other-{suffix}",
        )
        db.add(other_tenant)
        await db.flush()
        foreign_user = User(
            tenant_id=other_tenant.id,
            display_name="Foreign Recipient",
            role="member",
            is_active=True,
        )
        db.add(foreign_user)
        await db.commit()
        foreign_user_id = foreign_user.id

    async with async_session() as db:
        with pytest.raises(RecipientResolutionError) as exc_info:
            await resolve_group_mention_recipient(
                db,
                agent_id,
                foreign_user_id,
            )

    assert exc_info.value.code == "recipient_not_found"
