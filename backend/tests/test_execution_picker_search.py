"""Execution-user picker shares the normalized member search contract."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agents as agents_api
from app.database import Base
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, DirectoryAccountGroup, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from test_agent_permission_directory import _allow_manage


@pytest.fixture
async def execution_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Identity.__table__,
        Tenant.__table__,
        User.__table__,
        IdentityProvider.__table__,
        OrgMember.__table__,
        DirectoryAccountGroup.__table__,
        ChannelUserBinding.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_execution_picker_searches_identity_and_directory_contacts(
    execution_session,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    manager = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Manager",
        role="member",
        is_active=True,
    )
    identity = Identity(
        id=uuid.uuid4(),
        username=f"alice-{uuid.uuid4().hex[:8]}",
        email="alice@example.com",
        phone="13800000000",
    )
    target = User(
        id=uuid.uuid4(),
        identity_id=identity.id,
        tenant_id=tenant_id,
        display_name="Alice Zhang",
        role="member",
        is_active=True,
    )
    provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="SCIM",
        provider_type="oauth2",
    )
    member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider.id,
        user_id=target.id,
        external_id="alice",
        name="张艾丽",
        email="directory-alice@example.com",
        phone="13900000000",
        status="active",
    )
    execution_session.add_all(
        [
            Tenant(
                id=tenant_id,
                name="Acme",
                slug=f"acme-{uuid.uuid4().hex[:8]}",
            ),
            manager,
            identity,
            target,
            provider,
            member,
        ]
    )
    await execution_session.flush()
    await _allow_manage(monkeypatch, tenant_id)

    for search in ("13800000000", "1390000", "directory-alice", "alice"):
        response = await agents_api.get_agent_permission_members(
            agent_id=uuid.uuid4(),
            execution_assignable=True,
            search=search,
            page=1,
            page_size=50,
            current_user=manager,
            db=execution_session,
        )
        assert [item["id"] for item in response["items"]] == [str(target.id)]
