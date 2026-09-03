import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services.directory_user_status import sync_tenant_user_statuses


@pytest.fixture
async def status_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Tenant.__table__,
        User.__table__,
        IdentityProvider.__table__,
        OrgMember.__table__,
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
    await engine.dispose()


@pytest.mark.asyncio
async def test_tenant_user_status_aggregates_all_provider_accounts(status_session):
    tenant_id = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Directory User",
        role="member",
        is_active=False,
    )
    first_provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="SCIM",
        provider_type="oauth2",
        is_active=True,
    )
    second_provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="DingTalk",
        provider_type="dingtalk",
        is_active=True,
    )
    first_account = OrgMember(
        tenant_id=tenant_id,
        provider_id=first_provider.id,
        user_id=user.id,
        external_id="scim-user",
        name="Directory User",
        status="inactive",
    )
    second_account = OrgMember(
        tenant_id=tenant_id,
        provider_id=second_provider.id,
        user_id=user.id,
        external_id="dingtalk-user",
        name="Directory User",
        status="active",
    )
    status_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            user,
            first_provider,
            second_provider,
            first_account,
            second_account,
        ]
    )
    await status_session.flush()

    enabled = await sync_tenant_user_statuses(
        status_session,
        tenant_id=tenant_id,
        changed_provider_id=first_provider.id,
    )
    assert user.is_active is True
    assert enabled == {"enabled": 1, "disabled": 0}

    second_account.status = "inactive"
    await status_session.flush()
    disabled = await sync_tenant_user_statuses(
        status_session,
        tenant_id=tenant_id,
        changed_provider_id=second_provider.id,
    )
    assert user.is_active is False
    assert disabled == {"enabled": 0, "disabled": 1}


@pytest.mark.asyncio
async def test_tenant_user_status_is_tenant_scoped_and_ignores_inactive_provider(
    status_session,
):
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    user = User(
        id=uuid.uuid4(), tenant_id=tenant_id, display_name="Member", is_active=True,
    )
    other_user = User(
        id=uuid.uuid4(), tenant_id=other_tenant_id, display_name="Other", is_active=True,
    )
    provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="SCIM", provider_type="oauth2",
    )
    inactive_provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="Old", provider_type="oauth2",
        is_active=False,
    )
    status_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            Tenant(
                id=other_tenant_id,
                name="Other",
                slug=f"other-{uuid.uuid4().hex[:8]}",
            ),
            user,
            other_user,
            provider,
            inactive_provider,
            OrgMember(
                tenant_id=tenant_id,
                provider_id=provider.id,
                user_id=user.id,
                external_id="disabled",
                name="Member",
                status="inactive",
            ),
            OrgMember(
                tenant_id=tenant_id,
                provider_id=inactive_provider.id,
                user_id=user.id,
                external_id="stale-active",
                name="Member",
                status="active",
            ),
        ]
    )
    await status_session.flush()

    await sync_tenant_user_statuses(
        status_session,
        tenant_id=tenant_id,
        changed_provider_id=provider.id,
    )
    assert user.is_active is False
    assert other_user.is_active is True
