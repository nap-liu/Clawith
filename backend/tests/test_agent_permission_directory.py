import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agents as agents_api
from app.core.permissions import get_agent_access_level_for_user_id
from app.database import Base
from app.models.agent import AgentPermission
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture
async def directory_session():
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
        await conn.run_sync(lambda connection: Base.metadata.create_all(connection, tables=tables))
        await conn.execute(
            text("CREATE TABLE agent_relationships (id CHAR(32) PRIMARY KEY, member_id CHAR(32) NOT NULL)")
        )
        await conn.execute(
            text(
                "CREATE TABLE agent_permissions ("
                "id CHAR(32) PRIMARY KEY, agent_id CHAR(32) NOT NULL, "
                "scope_type VARCHAR(20) NOT NULL, scope_id CHAR(32), "
                "access_level VARCHAR(20) NOT NULL)"
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _allow_manage(monkeypatch, tenant_id: uuid.UUID):
    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=_agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)


@pytest.mark.asyncio
async def test_permission_departments_are_lazy_and_include_my_department(
    directory_session,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    current_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Manager",
        role="member",
        is_active=True,
    )
    provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="DingTalk", provider_type="dingtalk"
    )
    other_provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="Feishu", provider_type="feishu"
    )
    root = OrgDepartment(
        id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider.id,
        name="Root", path="Root", status="active"
    )
    center = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider.id,
        name="营运中心",
        path="Root/营运中心",
        parent_id=root.id,
        status="active",
    )
    team = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider.id,
        name="广东营运部",
        path="Root/营运中心/广东营运部",
        parent_id=center.id,
        status="active",
    )
    malformed_cross_provider_child = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=other_provider.id,
        name="Must not appear",
        path="Root/Must not appear",
        parent_id=root.id,
        status="active",
    )
    directory_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            provider,
            other_provider,
            current_user,
            root,
            center,
            team,
            malformed_cross_provider_child,
            OrgMember(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                provider_id=provider.id,
                user_id=current_user.id,
                name="Manager",
                department_id=team.id,
                department_path=team.path,
                status="active",
            ),
            OrgMember(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                provider_id=provider.id,
                name="Unlinked directory member",
                department_id=team.id,
                department_path=team.path,
                status="active",
            ),
        ]
    )
    await directory_session.flush()
    await _allow_manage(monkeypatch, tenant_id)

    root_response = await agents_api.get_agent_permission_departments(
        agent_id=uuid.uuid4(),
        limit=100,
        current_user=current_user,
        db=directory_session,
    )

    assert [item["id"] for item in root_response["items"]] == [str(root.id)]
    assert root_response["items"][0]["has_children"] is True
    assert root_response["my_department"]["id"] == str(team.id)
    assert root_response["my_department"]["direct_member_count"] == 1

    children_response = await agents_api.get_agent_permission_departments(
        agent_id=uuid.uuid4(),
        parent_id=root.id,
        limit=100,
        current_user=current_user,
        db=directory_session,
    )
    assert [item["id"] for item in children_response["items"]] == [str(center.id)]
    assert children_response["my_department"] is None


@pytest.mark.asyncio
async def test_permission_members_prefers_directory_profile_and_supports_descendants(
    directory_session,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    current_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Manager",
        role="member",
        is_active=True,
    )
    target_user = User(
        id=user_id,
        tenant_id=tenant_id,
        display_name="刘喜",
        role="member",
        is_active=True,
    )
    root = OrgDepartment(id=uuid.uuid4(), tenant_id=tenant_id, name="Root", path="Root", status="active")
    team = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="前端开发",
        path="Root/信息技术部/前端开发",
        parent_id=root.id,
        status="active",
    )
    old_provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="OAuth", provider_type="oauth2"
    )
    directory_provider = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="DingTalk", provider_type="dingtalk"
    )
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    old_member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=old_provider.id,
        user_id=user_id,
        name="Oauth2 User 123",
        status="active",
        synced_at=now - timedelta(days=20),
    )
    directory_member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=directory_provider.id,
        user_id=user_id,
        name="刘喜",
        name_translit_full="liuxi",
        name_translit_initial="lx",
        department_id=team.id,
        department_path=team.path,
        title="前端开发",
        external_id="ding-123",
        status="active",
        synced_at=now,
    )
    directory_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            current_user,
            target_user,
            root,
            team,
            old_provider,
            directory_provider,
            old_member,
            directory_member,
        ]
    )
    await directory_session.flush()
    await directory_session.execute(
        text("INSERT INTO agent_relationships (id, member_id) VALUES (:id, :member_id)"),
        {"id": uuid.uuid4().hex, "member_id": old_member.id.hex},
    )
    await _allow_manage(monkeypatch, tenant_id)

    response = await agents_api.get_agent_permission_members(
        agent_id=uuid.uuid4(),
        department_id=root.id,
        include_descendants=True,
        search=None,
        page=1,
        page_size=50,
        current_user=current_user,
        db=directory_session,
    )

    assert response["total"] == 1
    assert response["has_more"] is False
    assert response["items"] == [
        {
            "id": str(user_id),
            "member_id": str(directory_member.id),
                "name": "刘喜",
                "nickname": None,
            "department_id": str(team.id),
            "department_path": team.path,
            "title": "前端开发",
            "avatar_url": None,
            "email": None,
        }
    ]

    search_response = await agents_api.get_agent_permission_members(
        agent_id=uuid.uuid4(),
        search="lx",
        page=1,
        page_size=50,
        current_user=current_user,
        db=directory_session,
    )
    assert [item["id"] for item in search_response["items"]] == [str(user_id)]


@pytest.mark.asyncio
async def test_department_node_is_saved_and_grants_descendant_access(
    directory_session,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    manager = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Manager",
        role="member",
        is_active=True,
    )
    target_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Descendant member",
        role="member",
        is_active=True,
    )
    root = OrgDepartment(
        id=uuid.uuid4(), tenant_id=tenant_id, name="Root", path="Root", status="active"
    )
    parent = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="信息技术部",
        path="Root/信息技术部",
        parent_id=root.id,
        status="active",
    )
    child = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="前端开发",
        path="Root/信息技术部/前端开发",
        parent_id=parent.id,
        status="active",
    )
    directory_session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            manager,
            target_user,
            root,
            parent,
            child,
            OrgMember(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=target_user.id,
                name="Descendant member",
                department_id=child.id,
                department_path=child.path,
                status="active",
            ),
        ]
    )
    await directory_session.flush()

    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=manager.id,
        access_mode="custom",
        company_access_level="use",
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_relationship_sync(*_args, **_kwargs):
        return False

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        agents_api,
        "ensure_access_granted_platform_relationships",
        fake_relationship_sync,
    )

    await agents_api.update_agent_permissions(
        agent_id=agent_id,
        data={
            "scope_type": "custom",
            "department_access": [
                {"id": str(parent.id), "access_level": "use"},
            ],
            "user_access": [],
        },
        current_user=manager,
        db=directory_session,
    )

    response = await agents_api.get_agent_permissions(
        agent_id=agent_id,
        current_user=manager,
        db=directory_session,
    )
    assert response["department_access"] == [
        {
            "id": str(parent.id),
            "name": "信息技术部",
            "path": parent.path,
            "access_level": "use",
            "include_descendants": True,
        }
    ]
    assert await get_agent_access_level_for_user_id(
        directory_session,
        target_user.id,
        agent,
    ) == "use"


@pytest.mark.asyncio
async def test_department_grant_uses_parent_ids_and_never_crosses_provider(
    directory_session,
):
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    provider_a = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="DingTalk A", provider_type="dingtalk"
    )
    provider_b = IdentityProvider(
        id=uuid.uuid4(), tenant_id=tenant_id, name="DingTalk B", provider_type="dingtalk"
    )
    root_a = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_a.id,
        name="Root",
        path="Root",
        status="active",
    )
    child_a = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_a.id,
        name="Engineering",
        path="Root/Engineering",
        parent_id=root_a.id,
        status="active",
    )
    unrelated_a = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_a.id,
        name="Same display path",
        path="Root/Engineering/Fake",
        status="active",
    )
    root_b = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_b.id,
        name="Root",
        path="Root",
        status="active",
    )
    child_b = OrgDepartment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_b.id,
        name="Engineering",
        path="Root/Engineering",
        parent_id=root_b.id,
        status="active",
    )
    allowed_user = User(
        id=uuid.uuid4(), tenant_id=tenant_id, display_name="Allowed", role="member", is_active=True
    )
    wrong_tree_user = User(
        id=uuid.uuid4(), tenant_id=tenant_id, display_name="Wrong tree", role="member", is_active=True
    )
    wrong_provider_user = User(
        id=uuid.uuid4(), tenant_id=tenant_id, display_name="Wrong provider", role="member", is_active=True
    )
    directory_session.add_all([
        Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
        provider_a,
        provider_b,
        root_a,
        child_a,
        unrelated_a,
        root_b,
        child_b,
        allowed_user,
        wrong_tree_user,
        wrong_provider_user,
        OrgMember(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_a.id,
            user_id=allowed_user.id, name="Allowed", department_id=child_a.id,
            department_path=child_a.path, status="active",
        ),
        OrgMember(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_a.id,
            user_id=wrong_tree_user.id, name="Wrong tree", department_id=unrelated_a.id,
            department_path=unrelated_a.path, status="active",
        ),
        OrgMember(
            id=uuid.uuid4(), tenant_id=tenant_id, provider_id=provider_b.id,
            user_id=wrong_provider_user.id, name="Wrong provider", department_id=child_b.id,
            department_path=child_b.path, status="active",
        ),
    ])
    await directory_session.flush()
    directory_session.add(
        AgentPermission(
            id=uuid.uuid4(),
            agent_id=agent_id,
            scope_type="department",
            scope_id=root_a.id,
            access_level="use",
        )
    )
    await directory_session.flush()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        access_mode="custom",
        company_access_level="use",
    )

    assert await get_agent_access_level_for_user_id(
        directory_session, allowed_user.id, agent
    ) == "use"
    assert await get_agent_access_level_for_user_id(
        directory_session, wrong_tree_user.id, agent
    ) is None
    assert await get_agent_access_level_for_user_id(
        directory_session, wrong_provider_user.id, agent
    ) is None


@pytest.mark.asyncio
async def test_permission_update_rejects_cross_tenant_user_before_replacing_grants(
    directory_session,
    monkeypatch,
):
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    manager = User(
        id=uuid.uuid4(), tenant_id=tenant_id, display_name="Manager",
        role="member", is_active=True,
    )
    foreign_user = User(
        id=uuid.uuid4(), tenant_id=other_tenant_id, display_name="Foreign",
        role="member", is_active=True,
    )
    directory_session.add_all([
        Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
        Tenant(id=other_tenant_id, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}"),
        manager,
        foreign_user,
    ])
    await directory_session.flush()
    original_permission = AgentPermission(
        id=uuid.uuid4(), agent_id=agent_id, scope_type="user",
        scope_id=manager.id, access_level="manage",
    )
    directory_session.add(original_permission)
    await directory_session.flush()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=manager.id,
        access_mode="custom",
        company_access_level="use",
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    with pytest.raises(HTTPException) as exc:
        await agents_api.update_agent_permissions(
            agent_id=agent_id,
            data={
                "scope_type": "custom",
                "user_access": [{"id": str(foreign_user.id), "access_level": "use"}],
                "department_access": [],
            },
            current_user=manager,
            db=directory_session,
        )
    assert exc.value.status_code == 400
    remaining = (
        await directory_session.execute(
            select(AgentPermission).where(AgentPermission.agent_id == agent_id)
        )
    ).scalars().all()
    assert [(row.scope_id, row.access_level) for row in remaining] == [
        (manager.id, "manage")
    ]
