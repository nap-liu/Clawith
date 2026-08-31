"""Resolver and Feishu search channel-binding integration tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from tests.test_channel_binding_concurrency import (
    Agent,
    AgentPermission,
    AgentAgentRelationship,
    AgentRelationship,
    ChannelConfig,
    ChannelUserService,
    IdentityProvider,
    OrgMember,
    RecipientResolutionError,
    RelationshipSuppression,
    Tenant,
    User,
    _dispose_engine_between_cases,
    _feishu_user_search,
    async_session,
    resolve_agent_recipient,
    resolve_human_channel_recipient,
    resolve_platform_user_recipient,
)


@pytest.mark.asyncio
async def test_resolvers_recheck_revoked_a2a_suppression_and_platform_reachability():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Resolver {suffix}", slug=f"resolver-{suffix}")
        db.add(tenant)
        await db.flush()
        source_owner = User(
            tenant_id=tenant.id,
            display_name="Source Owner",
            role="member",
            is_active=True,
        )
        target_owner = User(
            tenant_id=tenant.id,
            display_name="Target Owner",
            role="member",
            is_active=True,
        )
        external_user = User(
            tenant_id=tenant.id,
            identity_id=None,
            display_name="External Only",
            role="member",
            is_active=True,
        )
        db.add_all([source_owner, target_owner, external_user])
        await db.flush()
        source = Agent(
            tenant_id=tenant.id,
            creator_id=source_owner.id,
            name=f"Source {suffix}",
            status="running",
        )
        target = Agent(
            tenant_id=tenant.id,
            creator_id=target_owner.id,
            name=f"Custom Target {suffix}",
            status="running",
            access_mode="custom",
        )
        db.add_all([source, target])
        await db.flush()
        permission = AgentPermission(
            agent_id=target.id,
            scope_type="user",
            scope_id=source_owner.id,
            access_level="use",
        )
        edge = AgentAgentRelationship(
            agent_id=source.id,
            target_agent_id=target.id,
            created_by_user_id=source_owner.id,
        )
        human_edge = AgentRelationship(
            agent_id=source.id,
            user_id=external_user.id,
            created_by_user_id=source_owner.id,
        )
        db.add_all([permission, edge, human_edge])
        await db.commit()
        source_id = source.id
        target_id = target.id
        external_user_id = external_user.id
        permission_id = permission.id

    async with async_session() as db:
        assert (
            await resolve_agent_recipient(db, source_id, target_id)
        ).target_agent.id == target_id
        await db.execute(
            delete(AgentPermission).where(AgentPermission.id == permission_id)
        )
        await db.commit()

    async with async_session() as db:
        with pytest.raises(RecipientResolutionError) as revoked:
            await resolve_agent_recipient(db, source_id, target_id)
        assert revoked.value.code == "relationship_inactive"

        with pytest.raises(RecipientResolutionError) as unreachable:
            await resolve_platform_user_recipient(db, source_id, external_user_id)
        assert unreachable.value.code == "platform_recipient_unreachable"

        db.add(
            RelationshipSuppression(
                agent_id=source_id,
                target_type="user",
                target_id=external_user_id,
            )
        )
        await db.commit()

    async with async_session() as db:
        with pytest.raises(RecipientResolutionError) as suppressed:
            await resolve_human_channel_recipient(
                db, source_id, external_user_id, channel="feishu"
            )
        assert suppressed.value.code == "relationship_inactive"


@pytest.mark.asyncio
async def test_feishu_search_returns_all_duplicate_names_with_canonical_ids_only():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Search {suffix}", slug=f"search-{suffix}")
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
            name=f"Search Agent {suffix}",
            status="running",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="feishu",
            name=f"Feishu {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([agent, provider])
        await db.flush()
        db.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="feishu",
                app_id=f"cli-{suffix}",
                app_secret="test-only",
                is_configured=True,
            )
        )
        await db.commit()
        agent_id = agent.id

    canonical_ids: list[uuid.UUID] = []
    for index in range(2):
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
            user = await ChannelUserService().resolve_channel_user(
                db,
                agent,
                "feishu",
                f"ou_{suffix}_{index}",
                {
                    "open_id": f"ou_{suffix}_{index}",
                    "external_id": f"u_{suffix}_{index}",
                    "name": "同名用户",
                    "department": f"部门 {index}",
                },
            )
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.user_id == user.id,
                        OrgMember.tenant_id == user.tenant_id,
                    )
                )
            ).scalar_one()
            member.department_path = f"部门 {index}"
            db.add(
                AgentRelationship(
                    agent_id=agent_id,
                    user_id=user.id,
                    member_id=member.id,
                )
            )
            canonical_ids.append(user.id)
            await db.commit()

    result = await _feishu_user_search(agent_id, {"name": "同名"})
    assert all(str(user_id) in result for user_id in canonical_ids)
    assert "存在重名" in result
    assert "open_id" not in result
    assert f"ou_{suffix}" not in result
    assert f"u_{suffix}" not in result
