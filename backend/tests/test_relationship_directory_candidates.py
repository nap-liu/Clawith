"""Relationship picker uses the same normalized directory identity contract."""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agents as agents_api
from app.api import relationships
from app.database import Base
from app.models.agent import AgentPermission
from app.models.identity import IdentityProvider
from app.models.org import (
    AgentRelationship,
    ChannelUserBinding,
    DirectoryAccountGroup,
    DirectoryGroupEdge,
    OrgDepartment,
    OrgMember,
)
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.mark.asyncio
async def test_relationship_candidate_includes_all_sources_and_channel_bindings(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Identity.__table__, Tenant.__table__, User.__table__,
        IdentityProvider.__table__, OrgDepartment.__table__, OrgMember.__table__,
        DirectoryGroupEdge.__table__, DirectoryAccountGroup.__table__,
        ChannelUserBinding.__table__, AgentPermission.__table__,
        AgentRelationship.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    async with Session() as db:
        manager = User(
            id=uuid.uuid4(), tenant_id=tenant_id, display_name="Manager",
            role="org_admin", is_active=True,
        )
        identity = Identity(
            id=uuid.uuid4(), username=f"person-{uuid.uuid4().hex[:8]}",
            email=f"person-{uuid.uuid4().hex[:8]}@example.test", is_active=True,
        )
        target = User(
            id=uuid.uuid4(), identity_id=identity.id, tenant_id=tenant_id,
            display_name="One Person", role="member", is_active=True,
        )
        provider_a = IdentityProvider(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_type="dingtalk",
            name="DingTalk A", is_active=True,
        )
        provider_b = IdentityProvider(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_type="scim",
            name="SCIM B", is_active=True,
        )
        group = OrgDepartment(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_a.id,
            external_id="group", name="Group", status="active",
        )
        member_a = OrgMember(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_a.id,
            external_id="person-a", user_id=target.id, name="One Person",
            department_id=group.id, status="active",
        )
        member_b = OrgMember(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_b.id,
            external_id="person-b", user_id=target.id, name="One Person",
            status="active",
        )
        binding = ChannelUserBinding(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_a.id,
            installation_scope="corp-a", channel_type="dingtalk",
            id_type="staff_id", subject="opaque-subject", user_id=target.id,
        )
        agent_id = uuid.uuid4()
        relationship = AgentRelationship(
            agent_id=agent_id, user_id=target.id, member_id=member_a.id,
            relation="colleague",
        )
        permission = AgentPermission(
            agent_id=agent_id, scope_type="user", scope_id=target.id,
            access_level="use",
        )
        db.add_all([
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            manager, identity, target, provider_a, provider_b, group,
            member_a, member_b, binding, relationship, permission,
        ])
        await db.flush()
        agent = SimpleNamespace(
            id=agent_id, tenant_id=tenant_id, creator_id=manager.id,
            access_mode="company", company_access_level="use",
        )

        async def allow_manage(_db, _user, _agent_id):
            return agent, "manage"

        monkeypatch.setattr(relationships, "check_agent_access", allow_manage)
        result = await relationships.search_human_relationship_candidates(
            agent_id=agent.id, search="One", current_user=manager, db=db
        )

        assert len(result) == 1
        assert {source["provider_name"] for source in result[0]["directory_sources"]} == {
            "DingTalk A", "SCIM B"
        }
        assert result[0]["channel_bindings"] == [{
            "provider_id": str(provider_a.id),
            "provider_name": "DingTalk A",
            "provider_type": "dingtalk",
            "channel_type": "dingtalk",
            "installation_scope": "corp-a",
            "id_type": "staff_id",
        }]

        monkeypatch.setattr(agents_api, "check_agent_access", allow_manage)
        saved_permissions = await agents_api.get_agent_permissions(
            agent_id=agent.id, current_user=manager, db=db,
        )
        saved_user = next(item for item in saved_permissions["user_access"] if item["id"] == str(target.id))
        assert saved_user["directory_sources"] == result[0]["directory_sources"]
        assert saved_user["channel_bindings"] == result[0]["channel_bindings"]

        from app.services import recipient_resolver

        async def no_relationship_sync(*_args, **_kwargs):
            return False

        async def load_profiles(*_args, **_kwargs):
            profile = SimpleNamespace(
                member=member_a, user=target, channels=("dingtalk",),
                provider_names=("DingTalk A",), access_status="active",
                access_status_reason=None,
            )
            return {target.id: profile}

        monkeypatch.setattr(relationships, "ensure_access_granted_platform_relationships", no_relationship_sync)
        monkeypatch.setattr(recipient_resolver, "load_human_recipient_profiles", load_profiles)
        saved_relationships = await relationships.get_relationships(
            agent_id=agent.id, current_user=manager, db=db,
        )
        assert saved_relationships[0]["member"]["directory_sources"] == result[0]["directory_sources"]
        assert saved_relationships[0]["member"]["channel_bindings"] == result[0]["channel_bindings"]
    await engine.dispose()
