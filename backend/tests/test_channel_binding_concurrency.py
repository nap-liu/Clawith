import asyncio
import uuid

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import DBAPIError

from app.database import async_session, engine
from app.models.agent import Agent, AgentPermission
from app.models.channel_config import ChannelConfig
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentAgentRelationship,
    AgentRelationship,
    ChannelUserBinding,
    OrgMember,
    RelationshipSuppression,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_user_service import (
    ChannelUserResolutionError,
    ChannelUserService,
)
from app.services.agent_tools import _feishu_user_search
from app.services.recipient_resolver import (
    RecipientResolutionError,
    list_human_recipient_channels,
    load_human_recipient_profiles,
    resolve_agent_recipient,
    resolve_human_channel_recipient,
    resolve_platform_user_recipient,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.parametrize(
    ("channel_type", "expected_binding_count"),
    [("wechat", 1), ("dingtalk", 3)],
)
@pytest.mark.asyncio
async def test_identical_channel_ingress_100_way_converges_to_one_user_and_shell(
    channel_type: str,
    expected_binding_count: int,
):
    suffix = uuid.uuid4().hex[:12]
    subject = f"wx_{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Concurrency {suffix}", slug=f"concurrency-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = User(
            tenant_id=tenant.id,
            identity_id=None,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        db.add(creator)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Concurrency Agent {suffix}",
            status="idle",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type=channel_type,
            name=f"{channel_type} {suffix}",
            config={"installation_scope": f"{channel_type}-installation-{suffix}"},
            is_active=True,
        )
        db.add_all([agent, provider])
        await db.commit()
        tenant_id = tenant.id
        agent_id = agent.id
        provider_id = provider.id

    async def resolve_once() -> uuid.UUID:
        extra_info = {"external_id": subject, "name": "Concurrent User"}
        if channel_type == "dingtalk":
            extra_info.update(
                {
                    "open_id": f"open_{suffix}",
                    "unionid": f"union_{suffix}",
                }
            )
        async with async_session() as db:
            target_agent = await db.get(Agent, agent_id)
            resolved = await ChannelUserService().resolve_channel_user(
                db,
                target_agent,
                channel_type,
                subject,
                extra_info,
            )
            resolved_id = resolved.id
            await db.commit()
            return resolved_id

    resolved_ids = await asyncio.gather(*(resolve_once() for _ in range(100)))
    assert len(set(resolved_ids)) == 1

    async with async_session() as db:
        bindings = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.tenant_id == tenant_id,
                    ChannelUserBinding.provider_id == provider_id,
                )
            )
        ).scalars().all()
        shell_count = await db.scalar(
            select(func.count(OrgMember.id)).where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.provider_id == provider_id,
                OrgMember.external_id == subject,
            )
        )
        channel_user_count = await db.scalar(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.registration_source == f"{channel_type}_channel",
            )
        )

    assert len(bindings) == expected_binding_count
    assert {binding.user_id for binding in bindings} == {resolved_ids[0]}
    assert shell_count == 1
    assert channel_user_count == 1


@pytest.mark.asyncio
async def test_same_subject_in_two_bot_installations_does_not_cross_bind():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Scoped {suffix}", slug=f"scoped-{suffix}")
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
        first_agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"First {suffix}",
            status="idle",
        )
        second_agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Second {suffix}",
            status="idle",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="slack",
            name=f"Slack {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([first_agent, second_agent, provider])
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=first_agent.id,
                    channel_type="slack",
                    app_id=f"app-a-{suffix}",
                    is_configured=True,
                ),
                ChannelConfig(
                    agent_id=second_agent.id,
                    channel_type="slack",
                    app_id=f"app-b-{suffix}",
                    is_configured=True,
                ),
            ]
        )
        await db.commit()
        first_agent_id = first_agent.id
        second_agent_id = second_agent.id
        tenant_id = tenant.id

    subject = f"U-{suffix}"
    resolved_ids = []
    for agent_id in (first_agent_id, second_agent_id):
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
            resolved = await ChannelUserService().resolve_channel_user(
                db,
                agent,
                "slack",
                subject,
                {"external_id": subject, "name": "Same provider subject"},
            )
            resolved_ids.append(resolved.id)
            await db.commit()

    assert len(set(resolved_ids)) == 2
    async with async_session() as db:
        bindings = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.tenant_id == tenant_id,
                    ChannelUserBinding.channel_type == "slack",
                    ChannelUserBinding.subject == subject,
                )
            )
        ).scalars().all()
    assert len(bindings) == 2
    assert len({binding.installation_scope for binding in bindings}) == 2

    async with async_session() as db:
        for source_agent_id, user_id in zip(
            (first_agent_id, second_agent_id), resolved_ids, strict=True
        ):
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.user_id == user_id,
                        OrgMember.external_id == subject,
                    )
                )
            ).scalar_one()
            db.add(
                AgentRelationship(
                    agent_id=source_agent_id,
                    user_id=user_id,
                    member_id=member.id,
                )
            )
        await db.commit()

    for source_agent_id, user_id in zip(
        (first_agent_id, second_agent_id), resolved_ids, strict=True
    ):
        async with async_session() as db:
            route = await resolve_human_channel_recipient(
                db, source_agent_id, user_id, channel="slack"
            )
            assert route.user.id == user_id
            assert route.member.user_id == user_id


@pytest.mark.asyncio
async def test_duplicate_directory_subject_with_different_users_fails_closed():
    suffix = uuid.uuid4().hex[:12]
    subject = f"staff-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Ambiguous {suffix}", slug=f"ambiguous-{suffix}")
        db.add(tenant)
        await db.flush()
        first_user = User(
            tenant_id=tenant.id,
            display_name="First",
            role="member",
            is_active=True,
        )
        second_user = User(
            tenant_id=tenant.id,
            display_name="Second",
            role="member",
            is_active=True,
        )
        db.add_all([first_user, second_user])
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=first_user.id,
            name=f"Ambiguous Agent {suffix}",
            status="idle",
        )
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name=f"DingTalk {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([agent, provider])
        await db.flush()
        db.add_all(
            [
                OrgMember(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    external_id=subject,
                    name="Duplicate A",
                    status="active",
                    user_id=first_user.id,
                ),
                OrgMember(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    external_id=subject,
                    name="Duplicate B",
                    status="active",
                    user_id=second_user.id,
                ),
            ]
        )
        await db.commit()
        agent_id = agent.id

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        with pytest.raises(ChannelUserResolutionError, match="migration_required"):
            await ChannelUserService().resolve_channel_user(
                db,
                agent,
                "dingtalk",
                subject,
                {"external_id": subject},
            )


@pytest.mark.asyncio
async def test_generic_legacy_subject_with_multiple_installations_fails_without_new_user():
    suffix = uuid.uuid4().hex[:12]
    subject = f"slack-user-{suffix}"
    async with async_session() as db:
        tenant = Tenant(name=f"Generic Scope {suffix}", slug=f"generic-scope-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = User(
            tenant_id=tenant.id,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        legacy_user = User(
            tenant_id=tenant.id,
            display_name="Legacy Slack User",
            role="member",
            is_active=True,
        )
        db.add_all([creator, legacy_user])
        await db.flush()
        agents = [
            Agent(
                tenant_id=tenant.id,
                creator_id=creator.id,
                name=f"Slack Agent {index} {suffix}",
                status="idle",
            )
            for index in range(2)
        ]
        provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="slack",
            name=f"Slack {suffix}",
            config={},
            is_active=True,
        )
        db.add_all([*agents, provider])
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="slack",
                    extra_config={"workspace_id": f"workspace-{index}-{suffix}"},
                    is_configured=True,
                )
                for index, agent in enumerate(agents)
            ]
        )
        db.add(
            OrgMember(
                tenant_id=tenant.id,
                provider_id=provider.id,
                external_id=subject,
                name="Legacy Slack User",
                status="active",
                user_id=legacy_user.id,
            )
        )
        await db.commit()
        tenant_id = tenant.id
        agent_id = agents[0].id
        before_count = await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        with pytest.raises(ChannelUserResolutionError, match="migration_required"):
            await ChannelUserService().resolve_channel_user(
                db,
                agent,
                "slack",
                subject,
                {"external_id": subject},
            )
        await db.rollback()
        after_count = await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )
    assert after_count == before_count


@pytest.mark.asyncio
async def test_relationship_profile_aggregates_exact_routes_across_members():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Profile {suffix}", slug=f"profile-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = User(
            tenant_id=tenant.id,
            display_name="Creator",
            role="member",
            is_active=True,
        )
        target = User(
            tenant_id=tenant.id,
            display_name="Canonical Multi Channel User",
            role="member",
            is_active=True,
        )
        db.add_all([creator, target])
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Profile Agent {suffix}",
            status="idle",
        )
        feishu_provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="feishu",
            name="Feishu Directory",
            config={},
            is_active=True,
        )
        dingtalk_provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name="DingTalk Directory",
            config={},
            is_active=True,
        )
        db.add_all([agent, feishu_provider, dingtalk_provider])
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="feishu",
                    app_id=f"feishu-{suffix}",
                    is_configured=True,
                ),
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="dingtalk",
                    app_id=f"dingtalk-{suffix}",
                    is_configured=True,
                ),
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="discord",
                    extra_config={"application_id": f"discord-{suffix}"},
                    is_configured=True,
                ),
            ]
        )
        await db.flush()
        feishu_member = OrgMember(
            tenant_id=tenant.id,
            provider_id=feishu_provider.id,
            external_id=f"feishu-user-{suffix}",
            name="Feishu Display",
            status="active",
            user_id=target.id,
        )
        dingtalk_member = OrgMember(
            tenant_id=tenant.id,
            provider_id=dingtalk_provider.id,
            external_id=f"dingtalk-user-{suffix}",
            name="DingTalk Display",
            status="active",
            user_id=target.id,
        )
        inactive_legacy_member = OrgMember(
            tenant_id=tenant.id,
            provider_id=feishu_provider.id,
            external_id=f"old-{suffix}",
            name="Inactive Legacy",
            status="inactive",
            user_id=target.id,
        )
        db.add_all([feishu_member, dingtalk_member, inactive_legacy_member])
        await db.flush()
        service = ChannelUserService()
        feishu_scope = await service.resolve_installation_scope(db, agent, "feishu")
        dingtalk_scope = await service.resolve_installation_scope(db, agent, "dingtalk")
        db.add_all(
            [
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=feishu_provider.id,
                    installation_scope=feishu_scope,
                    channel_type="feishu",
                    id_type="user_id",
                    subject=feishu_member.external_id,
                    user_id=target.id,
                ),
                ChannelUserBinding(
                    tenant_id=tenant.id,
                    provider_id=dingtalk_provider.id,
                    installation_scope=dingtalk_scope,
                    channel_type="dingtalk",
                    id_type="staff_id",
                    subject=dingtalk_member.external_id,
                    user_id=target.id,
                ),
            ]
        )
        relationship = AgentRelationship(
            agent_id=agent.id,
            user_id=target.id,
            member_id=inactive_legacy_member.id,
            relation="collaborator",
        )
        db.add(relationship)
        await db.commit()
        agent_id = agent.id
        target_id = target.id
        relationship_id = relationship.id

    async with async_session() as db:
        source = await db.get(Agent, agent_id)
        relationship = await db.get(AgentRelationship, relationship_id)
        channels = await list_human_recipient_channels(db, agent_id, target_id)
        profiles = await load_human_recipient_profiles(db, source, [relationship])
    assert channels == ["dingtalk", "feishu"]
    assert set(profiles) == {target_id}
    assert profiles[target_id].user.display_name == "Canonical Multi Channel User"
    assert profiles[target_id].channels == ("dingtalk", "feishu")
    assert profiles[target_id].member.status == "active"
    assert set(profiles[target_id].provider_names) == {
        "DingTalk Directory",
        "Feishu Directory",
    }


@pytest.mark.asyncio
async def test_relationship_profile_query_count_is_constant_for_large_network():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Batch Profile {suffix}", slug=f"batch-profile-{suffix}")
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
            name=f"Batch Profile Agent {suffix}",
            status="idle",
        )
        users = [
            User(
                tenant_id=tenant.id,
                display_name=f"Person {index}",
                role="member",
                is_active=True,
            )
            for index in range(100)
        ]
        db.add_all([agent, *users])
        await db.flush()
        db.add_all(
            [
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="feishu",
                    app_id=f"batch-feishu-{suffix}",
                    is_configured=True,
                ),
                ChannelConfig(
                    agent_id=agent.id,
                    channel_type="dingtalk",
                    app_id=f"batch-dingtalk-{suffix}",
                    is_configured=True,
                ),
            ]
        )
        relationships = [
            AgentRelationship(agent_id=agent.id, user_id=user.id)
            for user in users
        ]
        db.add_all(relationships)
        await db.commit()
        agent_id = agent.id

    async with async_session() as db:
        from app.services.agent_context import _load_relationships_from_db

        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_query)
        try:
            context = await _load_relationships_from_db(db, agent_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_query)

    assert context.count("- user_id：") == 100
    assert query_count <= 12


@pytest.mark.asyncio
async def test_database_rejects_cross_tenant_human_and_agent_relationships():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        first_tenant = Tenant(name=f"First {suffix}", slug=f"first-{suffix}")
        second_tenant = Tenant(name=f"Second {suffix}", slug=f"second-{suffix}")
        db.add_all([first_tenant, second_tenant])
        await db.flush()
        first_user = User(
            tenant_id=first_tenant.id,
            display_name="First User",
            role="member",
            is_active=True,
        )
        second_user = User(
            tenant_id=second_tenant.id,
            display_name="Second User",
            role="member",
            is_active=True,
        )
        db.add_all([first_user, second_user])
        await db.flush()
        first_agent = Agent(
            tenant_id=first_tenant.id,
            creator_id=first_user.id,
            name=f"First Agent {suffix}",
            status="idle",
        )
        second_agent = Agent(
            tenant_id=second_tenant.id,
            creator_id=second_user.id,
            name=f"Second Agent {suffix}",
            status="idle",
        )
        db.add_all([first_agent, second_agent])
        await db.commit()
        first_agent_id = first_agent.id
        second_agent_id = second_agent.id
        second_user_id = second_user.id

    async with async_session() as db:
        db.add(
            AgentRelationship(
                agent_id=first_agent_id,
                user_id=second_user_id,
            )
        )
        with pytest.raises(DBAPIError, match="tenant mismatch"):
            await db.flush()
        await db.rollback()

    async with async_session() as db:
        db.add(
            AgentAgentRelationship(
                agent_id=first_agent_id,
                target_agent_id=second_agent_id,
            )
        )
        with pytest.raises(DBAPIError, match="tenant mismatch"):
            await db.flush()
        await db.rollback()


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
