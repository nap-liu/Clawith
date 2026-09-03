"""Search behavior shared by organization member pickers."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, DirectoryAccountGroup, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.org_directory import permission_directory_members


@pytest.mark.asyncio
async def test_picker_searches_platform_and_provider_name_phone_and_email():
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
    tenant_id = uuid.uuid4()
    async with Session() as db:
        provider = IdentityProvider(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="SCIM",
            provider_type="oauth2",
        )
        first_identity = Identity(
            id=uuid.uuid4(),
            username=f"first-{uuid.uuid4().hex[:8]}",
            email=f"first-{uuid.uuid4().hex[:8]}@example.com",
            phone="13800000000",
        )
        second_identity = Identity(
            id=uuid.uuid4(),
            username=f"second-{uuid.uuid4().hex[:8]}",
            email=f"second-{uuid.uuid4().hex[:8]}@example.com",
            phone="138000000001",
        )
        first_user = User(
            id=uuid.uuid4(),
            identity_id=first_identity.id,
            tenant_id=tenant_id,
            display_name="Alice Zhang",
            role="member",
            is_active=True,
        )
        second_user = User(
            id=uuid.uuid4(),
            identity_id=second_identity.id,
            tenant_id=tenant_id,
            display_name="Bob",
            role="member",
            is_active=True,
        )
        first_member = OrgMember(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider_id=provider.id,
            user_id=first_user.id,
            external_id="first",
            name="张艾丽",
            email="directory-alice@example.com",
            phone="13900000000",
            status="active",
        )
        second_member = OrgMember(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider_id=provider.id,
            user_id=second_user.id,
            external_id="second",
            name="Bob",
            status="active",
        )
        db.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="Acme",
                    slug=f"acme-{uuid.uuid4().hex[:8]}",
                ),
                provider,
                first_identity,
                second_identity,
                first_user,
                second_user,
                first_member,
                second_member,
            ]
        )
        await db.flush()

        exact = await permission_directory_members(
            db, tenant_id=tenant_id, search="13800000000"
        )
        provider_email = await permission_directory_members(
            db, tenant_id=tenant_id, search="directory-alice"
        )
        fuzzy_name = await permission_directory_members(
            db, tenant_id=tenant_id, search="alice"
        )

        assert [item["id"] for item in exact["items"]] == [
            str(first_user.id),
            str(second_user.id),
        ]
        assert [item["id"] for item in provider_email["items"]] == [
            str(first_user.id)
        ]
        assert [item["id"] for item in fuzzy_name["items"]] == [str(first_user.id)]
    await engine.dispose()
