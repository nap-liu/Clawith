import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.identity import IdentityProvider
from app.models.org import DirectoryGroupEdge, OrgDepartment, OrgMember
from app.models.tenant import Tenant
from app.services.provider_directory_browser import provider_directory_departments


@pytest.fixture
async def directory_browser_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Tenant.__table__,
        IdentityProvider.__table__,
        OrgDepartment.__table__,
        OrgMember.__table__,
        DirectoryGroupEdge.__table__,
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
async def test_provider_directory_browser_loads_one_nonempty_level_at_a_time(
    directory_browser_session,
):
    tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()
    leaf_id = uuid.uuid4()
    empty_id = uuid.uuid4()
    directory_browser_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            IdentityProvider(
                id=provider_id,
                tenant_id=tenant_id,
                name="Standard SCIM",
                provider_type="oauth2",
                is_active=True,
            ),
            OrgDepartment(
                id=root_id,
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="company",
                name="Acme",
                path="Acme",
                member_count=2,
            ),
            OrgDepartment(
                id=child_id,
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="engineering",
                name="Engineering",
                path="Acme/Engineering",
                parent_id=root_id,
                member_count=2,
            ),
            OrgDepartment(
                id=leaf_id,
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="frontend",
                name="Frontend",
                path="Acme/Engineering/Frontend",
                parent_id=child_id,
                member_count=2,
            ),
            OrgDepartment(
                id=empty_id,
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="empty",
                name="Empty",
                path="Acme/Empty",
                parent_id=root_id,
                member_count=0,
            ),
            OrgMember(
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="member-1",
                name="One",
                department_id=leaf_id,
                status="active",
            ),
            OrgMember(
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="member-2",
                name="Two",
                department_id=leaf_id,
                status="active",
            ),
        ]
    )
    await directory_browser_session.flush()

    roots = await provider_directory_departments(
        directory_browser_session,
        tenant_id=tenant_id,
        provider_id=provider_id,
    )
    assert [item["id"] for item in roots["items"]] == [str(root_id)]
    assert roots["items"][0]["has_children"] is True
    assert roots["total_member"] == 2

    children = await provider_directory_departments(
        directory_browser_session,
        tenant_id=tenant_id,
        provider_id=provider_id,
        parent_id=root_id,
    )
    assert [item["id"] for item in children["items"]] == [str(child_id)]
    assert children["items"][0]["has_children"] is True
    assert children["total_member"] == 0


@pytest.mark.asyncio
async def test_provider_directory_browser_promotes_legacy_virtual_root_children(
    directory_browser_session,
):
    tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    virtual_root_id = uuid.uuid4()
    child_id = uuid.uuid4()
    directory_browser_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            IdentityProvider(
                id=provider_id,
                tenant_id=tenant_id,
                name="DingTalk",
                provider_type="dingtalk",
                is_active=True,
            ),
            OrgDepartment(
                id=virtual_root_id,
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="1",
                name="Root",
                path="Root",
                member_count=1,
            ),
            OrgDepartment(
                id=child_id,
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id="engineering",
                name="Engineering",
                path="Root/Engineering",
                parent_id=virtual_root_id,
                member_count=1,
            ),
        ]
    )
    await directory_browser_session.flush()

    roots = await provider_directory_departments(
        directory_browser_session,
        tenant_id=tenant_id,
        provider_id=provider_id,
    )
    assert [item["id"] for item in roots["items"]] == [str(child_id)]
