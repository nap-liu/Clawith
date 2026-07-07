import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import enterprise as enterprise_api
from app.database import Base
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
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
        email="liuxi@example.com",
        phone=None,
        title="",
        department_path="研发部",
        avatar_url=None,
        external_id="ou_123",
        provider_id=uuid.uuid4(),
    )
    db = DummyDB([(member, "Feishu", "feishu", "刘喜")])

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
            "email": "liuxi@example.com",
            "phone": None,
            "title": "",
            "department_path": "总部/研发部",
            "avatar_url": None,
            "external_id": "ou_123",
            "provider_id": str(member.provider_id),
            "provider_name": "Feishu",
            "provider_type": "feishu",
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
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        await conn.execute(
            text("CREATE TABLE agent_relationships (id CHAR(32) PRIMARY KEY, member_id CHAR(32) NOT NULL)")
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
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
    assert response[0]["department_path"] == "Root/信息技术部/技术部/前端开发"
