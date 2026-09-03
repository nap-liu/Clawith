import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agents as agents_api
from app.database import Base
from app.models.identity import IdentityProvider
from app.models.org import (
    DirectoryAccountGroup,
    DirectoryGroupEdge,
    OrgDepartment,
    OrgMember,
)
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture
async def platform_tree_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Identity.__table__,
        Tenant.__table__,
        User.__table__,
        IdentityProvider.__table__,
        OrgDepartment.__table__,
        OrgMember.__table__,
        DirectoryGroupEdge.__table__,
        DirectoryAccountGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=tables,
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.drop_all(
                sync_connection,
                tables=tables,
            )
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_permission_tree_prefers_standard_scim_and_hides_empty_groups(
    platform_tree_session,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Member",
        role="member",
        is_active=True,
    )
    scim_provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Standard SSO",
        provider_type="oauth2",
        config={"directory_protocol": "scim"},
    )
    dingtalk_provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="DingTalk",
        provider_type="dingtalk",
    )
    standard_root = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=scim_provider.id,
        external_id="standard-root",
        name="Acme",
        path="Acme",
        member_count=1,
    )
    empty_standard_group = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=scim_provider.id,
        external_id="empty",
        name="Empty",
        path="Empty",
    )
    source_root = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=dingtalk_provider.id,
        external_id="1",
        name="DingTalk Corp",
        path="DingTalk Corp",
        member_count=1,
    )
    platform_tree_session.add_all([
        Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
        user,
        scim_provider,
        dingtalk_provider,
        standard_root,
        empty_standard_group,
        source_root,
        OrgMember(
            tenant_id=tenant_id,
            provider_id=scim_provider.id,
            external_id="scim-user",
            user_id=user.id,
            name="Member",
            department_id=standard_root.id,
        ),
        OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_provider.id,
            external_id="dingtalk-user",
            user_id=user.id,
            name="Member",
            department_id=source_root.id,
        ),
    ])
    await platform_tree_session.flush()

    async def allow_manage(_db, _user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", allow_manage)
    response = await agents_api.get_agent_permission_departments(
        agent_id=uuid.uuid4(),
        parent_id=None,
        search=None,
        limit=100,
        current_user=user,
        db=platform_tree_session,
    )

    assert [item["name"] for item in response["items"]] == ["Acme"]
    assert response["items"][0]["provider_name"] == "Standard SSO"
    assert response["items"][0]["direct_member_count"] == 1
