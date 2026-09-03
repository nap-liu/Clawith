import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import enterprise as enterprise_api
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


class DummyResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class DummyDB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return DummyResult(self.rows)


@pytest.mark.asyncio
async def test_list_org_members_handles_user_display_name_column(monkeypatch):
    member = SimpleNamespace(
        id=uuid.uuid4(),
        name="刘喜",
        nickname="喜茶",
        email="liuxi@example.com",
        phone="13800138000",
        title="",
        department_path="研发部",
        avatar_url=None,
        external_id="ou_123",
        provider_id=uuid.uuid4(),
    )
    db = DummyDB([(
        member,
        "Standard Identity",
        "oauth2",
        {"directory_protocol": "scim"},
        "刘喜",
    )])

    async def fake_derive_member_department_paths(_db, members):
        return {members[0].id: "总部/研发部"}

    monkeypatch.setattr(enterprise_api, "derive_member_department_paths", fake_derive_member_department_paths)

    response = await enterprise_api.list_org_members(
        search="刘喜",
        current_user=SimpleNamespace(role="platform_admin", tenant_id=None),
        db=db,
    )

    assert response == [
        {
            "id": str(member.id),
            "name": "刘喜",
            "nickname": "喜茶",
            "email": "liuxi@example.com",
            "phone_masked": "138****8000",
            "title": "",
            "department_path": "总部/研发部",
            "avatar_url": None,
            "external_id": "ou_123",
            "provider_id": str(member.provider_id),
            "provider_name": "Standard Identity",
            "provider_type": "oauth2",
            "directory_protocol": "scim",
            "user_display_name": "刘喜",
        }
    ]


@pytest.fixture
async def org_member_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Identity.__table__,
        Tenant.__table__,
        User.__table__,
        IdentityProvider.__table__,
        OrgDepartment.__table__,
        OrgMember.__table__,
        DirectoryAccountGroup.__table__,
        DirectoryGroupEdge.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        await conn.execute(
            text("CREATE TABLE agent_relationships (id CHAR(32) PRIMARY KEY, member_id CHAR(32) NOT NULL)")
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS agent_relationships"))
        await conn.run_sync(
            lambda c: Base.metadata.drop_all(c, tables=tables)
        )
    await engine.dispose()


async def _add_agent_relationship(session, member_id: uuid.UUID):
    await session.execute(
        text("INSERT INTO agent_relationships (id, member_id) VALUES (:id, :member_id)"),
        {"id": uuid.uuid4().hex, "member_id": member_id.hex},
    )


@pytest.mark.asyncio
async def test_list_org_members_scopes_canonical_selection_to_requested_provider(org_member_session):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    old_provider_id = uuid.uuid4()
    dingtalk_provider_id = uuid.uuid4()
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)

    org_member_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            Identity(id=uuid.uuid4(), username=f"user_{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@example.com"),
            User(
                id=user_id,
                identity_id=None,
                tenant_id=tenant_id,
                display_name="刘喜",
                role="member",
                is_active=True,
            ),
            IdentityProvider(id=old_provider_id, tenant_id=tenant_id, name="OAuth2", provider_type="oauth2"),
            IdentityProvider(id=dingtalk_provider_id, tenant_id=tenant_id, name="DingTalk", provider_type="dingtalk"),
        ]
    )
    await org_member_session.flush()

    old_member = OrgMember(
        id=uuid.uuid4(),
        provider_id=old_provider_id,
        user_id=user_id,
        tenant_id=tenant_id,
        name="刘喜",
        external_id=None,
        department_path="",
        status="active",
        synced_at=now - timedelta(days=30),
    )
    dingtalk_member = OrgMember(
        id=uuid.uuid4(),
        provider_id=dingtalk_provider_id,
        user_id=user_id,
        tenant_id=tenant_id,
        name="刘喜",
        external_id="632277911",
        department_path="Root/信息技术部/技术部/前端开发",
        phone="13800000000",
        status="active",
        synced_at=now,
    )
    org_member_session.add_all([old_member, dingtalk_member])
    await org_member_session.flush()

    await _add_agent_relationship(org_member_session, old_member.id)
    await _add_agent_relationship(org_member_session, old_member.id)
    await _add_agent_relationship(org_member_session, dingtalk_member.id)

    response = await enterprise_api.list_org_members(
        tenant_id=str(tenant_id),
        provider_id=str(dingtalk_provider_id),
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        db=org_member_session,
    )

    assert [row["id"] for row in response] == [str(dingtalk_member.id)]
    assert response[0]["provider_type"] == "dingtalk"
    assert response[0]["department_path"] == "信息技术部/技术部/前端开发"


@pytest.mark.asyncio
async def test_provider_member_search_supports_exact_and_fuzzy_contact_values(
    org_member_session,
):
    tenant_id = uuid.uuid4()
    provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="SCIM", provider_type="oauth2"
    )
    identity_a = Identity(
        id=uuid.uuid4(), username=f"alice-{uuid.uuid4().hex[:8]}",
        email=f"alice-{uuid.uuid4().hex[:8]}@example.com", phone="13800000000",
    )
    identity_b = Identity(
        id=uuid.uuid4(), username=f"bob-{uuid.uuid4().hex[:8]}",
        email=f"bob-{uuid.uuid4().hex[:8]}@example.com", phone="138000000001",
    )
    user_a = User(
        id=uuid.uuid4(), identity_id=identity_a.id, tenant_id=tenant_id,
        display_name="Alice Zhang", role="member", is_active=True,
    )
    user_b = User(
        id=uuid.uuid4(), identity_id=identity_b.id, tenant_id=tenant_id,
        display_name="Bob", role="member", is_active=True,
    )
    member_a = OrgMember(
        tenant_id=tenant_id, provider_id=provider.id, user_id=user_a.id,
        external_id="a", name="张艾丽", email="directory-alice@example.com",
        phone="13900000000", status="active",
    )
    member_b = OrgMember(
        tenant_id=tenant_id, provider_id=provider.id, user_id=user_b.id,
        external_id="b", name="Bob", status="active",
    )
    org_member_session.add_all([
        Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
        provider, identity_a, identity_b, user_a, user_b, member_a, member_b,
    ])
    await org_member_session.flush()
    current_user = SimpleNamespace(role="org_admin", tenant_id=tenant_id)

    exact = await enterprise_api.list_org_members(
        tenant_id=str(tenant_id), provider_id=str(provider.id),
        search="13800000000", current_user=current_user, db=org_member_session,
    )
    source_email = await enterprise_api.list_org_members(
        tenant_id=str(tenant_id), provider_id=str(provider.id),
        search="directory-alice", current_user=current_user, db=org_member_session,
    )
    fuzzy_name = await enterprise_api.list_org_members(
        tenant_id=str(tenant_id), provider_id=str(provider.id),
        search="alice", current_user=current_user, db=org_member_session,
    )

    assert [item["id"] for item in exact] == [str(member_a.id), str(member_b.id)]
    assert [item["id"] for item in source_email] == [str(member_a.id)]
    assert [item["id"] for item in fuzzy_name] == [str(member_a.id)]


@pytest.mark.parametrize(
    ("provider_type", "provider_config", "root_external_id"),
    [
        ("dingtalk", {}, "1"),
        ("oauth2", {"directory_protocol": "scim"}, "root"),
    ],
)
@pytest.mark.asyncio
async def test_org_browser_hides_transport_root_and_promotes_children(
    org_member_session,
    provider_type,
    provider_config,
    root_external_id,
):
    tenant_id = uuid.uuid4()
    provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Directory",
        provider_type=provider_type,
        config=provider_config,
    )
    root = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider.id,
        external_id=root_external_id,
        name="Root",
        path="Root",
        member_count=1,
    )
    child = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider.id,
        external_id="department-1",
        name="研发部",
        parent_id=root.id,
        path="Root/研发部",
        member_count=1,
    )
    empty_child = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider.id,
        external_id="empty-department",
        name="空部门",
        parent_id=root.id,
        path="Root/空部门",
    )
    member = OrgMember(
        tenant_id=tenant_id,
        provider_id=provider.id,
        external_id="user-1",
        name="成员",
        department_id=child.id,
        department_path="Root/研发部",
    )
    org_member_session.add_all([
        Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
        provider,
        root,
        child,
        empty_child,
        member,
    ])
    await org_member_session.flush()
    current_user = SimpleNamespace(role="org_admin", tenant_id=tenant_id)

    departments = await enterprise_api.list_org_departments(
        tenant_id=str(tenant_id),
        provider_id=str(provider.id),
        current_user=current_user,
        db=org_member_session,
    )
    members = await enterprise_api.list_org_members(
        tenant_id=str(tenant_id),
        provider_id=str(provider.id),
        current_user=current_user,
        db=org_member_session,
    )

    assert [(item["name"], item["parent_id"], item["path"]) for item in departments["items"]] == [
        ("研发部", None, "研发部")
    ]
    assert members[0]["department_path"] == "研发部"
