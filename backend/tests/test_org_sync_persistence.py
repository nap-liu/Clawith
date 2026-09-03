"""Org-sync persistence and canonical-user integration tests."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.database import async_session
from app.models.identity import IdentityProvider
from app.models.org import (
    DirectoryAccountGroup,
    DirectoryGroupEdge,
    DirectorySyncRun,
    OrgDepartment,
    OrgMember,
)
from app.models.user import Identity, User
from app.services.dingtalk_identity_reconciliation import dingtalk_legacy_identity_reconciler
from app.services.org_sync_adapter import (
    DingTalkOrgSyncAdapter,
    ExternalDepartment,
    ExternalUser,
)
from app.services.org_sync_service import org_sync_service
from app.services.scim_directory import ScimDirectorySnapshot
from app.services.scim_org_sync_adapter import ScimOrgSyncAdapter
from test_org_sync_adapter import (
    _DummyAdapter,
    _FakeDingTalkResponse,
    _isolate_async_engine_between_tests,  # noqa: F401 - imported autouse fixture
    _seed_dingtalk_provider,
    _seed_tenant,
)


@pytest.mark.asyncio
async def test_dingtalk_duplicate_member_and_channel_row_lock_do_not_deadlock():
    tenant = await _seed_tenant()
    seeded_provider = await _seed_dingtalk_provider(tenant.id)
    external_id = f"staff-duplicate-{uuid.uuid4().hex[:8]}"
    unionid = f"union-duplicate-{uuid.uuid4().hex[:8]}"
    email = f"duplicate-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session() as db:
        provider = await db.get(IdentityProvider, seeded_provider.id)
        provider.config = {"auto_create_users_on_sync": False}
        await db.commit()

    adapter = DingTalkOrgSyncAdapter(tenant_id=tenant.id)

    def external_user() -> ExternalUser:
        return ExternalUser(
            external_id=external_id,
            unionid=unionid,
            name="Duplicate Department User",
            email=email,
            mobile="",
            status="active",
            raw_data={
                "userid": external_id,
                "unionid": unionid,
                "email": "",
                "org_email": email,
                "mobile": "",
            },
        )

    await adapter._upsert_member_in_short_transaction(
        seeded_provider.id,
        external_user(),
        "dept-a",
    )

    channel_has_member_lock = asyncio.Event()
    release_channel = asyncio.Event()

    async def channel_like_update():
        async with async_session() as db:
            await dingtalk_legacy_identity_reconciler.acquire_subject_lock(
                db,
                tenant_id=tenant.id,
                provider_id=seeded_provider.id,
                external_id=external_id,
            )
            member = (
                await db.execute(
                    select(OrgMember)
                    .where(
                        OrgMember.provider_id == seeded_provider.id,
                        OrgMember.external_id == external_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            member.nickname = "channel-held"
            channel_has_member_lock.set()
            await release_channel.wait()
            await db.commit()

    async def repeated_sync():
        await channel_has_member_lock.wait()
        return await adapter._upsert_member_in_short_transaction(
            seeded_provider.id,
            external_user(),
            "dept-b",
        )

    channel_task = asyncio.create_task(channel_like_update())
    repeated_task = asyncio.create_task(repeated_sync())
    await asyncio.wait_for(channel_has_member_lock.wait(), timeout=2)
    release_channel.set()
    await asyncio.wait_for(
        asyncio.gather(channel_task, repeated_task),
        timeout=5,
    )

    async with async_session() as db:
        members = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == seeded_provider.id,
                    OrgMember.external_id == external_id,
                )
            )
        ).scalars().all()
        assert len(members) == 1


async def _seed_provider(tenant_id: uuid.UUID, provider_type: str) -> IdentityProvider:
    async with async_session() as db:
        provider = IdentityProvider(
            provider_type=provider_type,
            name=f"{provider_type} {uuid.uuid4().hex[:6]}",
            is_active=True,
            config={},
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider


async def _seed_user(tenant_id: uuid.UUID, *, email: str | None = None, phone: str | None = None) -> User:
    async with async_session() as db:
        identity = Identity(
            username=f"user_{uuid.uuid4().hex[:10]}",
            email=email,
            phone=phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant_id,
            display_name=f"User {uuid.uuid4().hex[:6]}",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.asyncio
async def test_directory_run_is_durable_deduplicated_and_marks_conflict_review(monkeypatch):
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    async with async_session() as db:
        run, created = await org_sync_service.request_sync(
            db, provider.id, trigger_type="manual"
        )
        duplicate, duplicate_created = await org_sync_service.request_sync(
            db, provider.id, trigger_type="manual"
        )
        assert created is True
        assert duplicate_created is False
        assert duplicate.id == run.id
        run_id = run.id

    class ConflictAdapter:
        async def sync_org_structure(self, _db):
            return {
                "departments": 1,
                "members": 1,
                "identity_conflicts": 1,
                "errors": [],
            }

    async def adapter_factory(*_args, **_kwargs):
        return ConflictAdapter()

    monkeypatch.setattr(
        "app.services.org_sync_adapter.get_org_sync_adapter", adapter_factory
    )
    await org_sync_service.execute_run(run_id)

    async with async_session() as db:
        persisted = await db.get(DirectorySyncRun, run_id)
        provider = await db.get(IdentityProvider, provider.id)
        assert persisted.status == "needs_review"
        assert persisted.progress_percent == 100
        assert provider.last_sync_success_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("departments", "members", "expected_status"),
    [(1, 1, "partial_failed"), (0, 0, "failed")],
)
async def test_directory_run_prioritizes_sync_errors_over_identity_review(
    monkeypatch,
    departments,
    members,
    expected_status,
):
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    async with async_session() as db:
        run, _created = await org_sync_service.request_sync(
            db, provider.id, trigger_type="manual"
        )
        run_id = run.id

    class FailedAndConflictedAdapter:
        async def sync_org_structure(self, _db):
            return {
                "departments": departments,
                "members": members,
                "identity_conflicts": 2,
                "errors": ["group apply failed"],
            }

    async def adapter_factory(*_args, **_kwargs):
        return FailedAndConflictedAdapter()

    monkeypatch.setattr(
        "app.services.org_sync_adapter.get_org_sync_adapter", adapter_factory
    )
    await org_sync_service.execute_run(run_id)

    async with async_session() as db:
        persisted = await db.get(DirectorySyncRun, run_id)
        assert persisted.status == expected_status
        assert persisted.stage == "completed_with_errors"
        assert "1 error(s)" in persisted.error_summary
        assert "2 identity match(es) require review" in persisted.error_summary


@pytest.mark.asyncio
async def test_scim_adapter_persists_dag_multi_group_and_opaque_user_anchor(monkeypatch):
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id, "oauth2")
    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        provider.config = {
            "capabilities": {"directory_protocol": "scim"},
            "client_id": "test",
            "client_secret": "test",
            "directory": {"base_url": "https://scim.test/v2"},
        }
        await db.commit()

    user_schema = "urn:ietf:params:scim:schemas:core:2.0:User"
    group_schema = "urn:ietf:params:scim:schemas:core:2.0:Group"
    snapshot = ScimDirectorySnapshot.from_resources(
        [
            {
                "schemas": [user_schema],
                "id": "user-without-contact",
                "userName": "opaque-user",
                "active": True,
                "photos": [{"value": "https://cdn.example/opaque.png"}],
            }
        ],
        [
            {
                "schemas": [group_schema],
                "id": "root-a",
                "displayName": "Root A",
                "members": [{"value": "child", "type": "Group"}],
            },
            {
                "schemas": [group_schema],
                "id": "root-b",
                "displayName": "Root B",
                "members": [{"value": "child", "type": "Group"}],
            },
            {
                "schemas": [group_schema],
                "id": "child",
                "displayName": "Child",
                "members": [{"value": "user-without-contact", "type": "User"}],
            },
        ],
    )

    class FakeClient:
        async def fetch_snapshot(self):
            return snapshot

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.services.scim_org_sync_adapter.ScimClient.from_provider_config",
        lambda _config: FakeClient(),
    )
    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        result = await ScimOrgSyncAdapter(
            provider=provider, tenant_id=tenant.id
        ).sync_org_structure(db)
        member = await db.scalar(
            select(OrgMember).where(OrgMember.provider_id == provider.id)
        )
        assert result["errors"] == []
        assert member.user_id is not None
        assert member.avatar_url == "https://cdn.example/opaque.png"
        platform_user = await db.get(User, member.user_id)
        assert platform_user.avatar_url == "https://cdn.example/opaque.png"
        assert platform_user.identity_id is not None
        assert platform_user.is_active is True
        assert await db.scalar(
            select(func.count()).select_from(DirectoryGroupEdge).where(
                DirectoryGroupEdge.provider_id == provider.id
            )
        ) == 2
        assert await db.scalar(
            select(func.count()).select_from(DirectoryAccountGroup).where(
                DirectoryAccountGroup.provider_id == provider.id
            )
        ) == 1
        assert await db.scalar(
            select(func.count()).select_from(OrgDepartment).where(
                OrgDepartment.provider_id == provider.id
            )
        ) == 3

        snapshot = ScimDirectorySnapshot(
            users=(replace(snapshot.users[0], active=False),),
            groups=snapshot.groups,
        )
        await ScimOrgSyncAdapter(
            provider=provider, tenant_id=tenant.id
        ).sync_org_structure(db)
        await db.refresh(platform_user)
        assert platform_user.is_active is False


@pytest.mark.asyncio
async def test_org_sync_auto_creates_user_from_dingtalk_member_mobile():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"

    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="钉钉新成员",
        mobile=phone,
        email="",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        assert stats["user_created"] is True
        assert stats["profile_synced"] is True
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert member.user_id is not None
        created_user = await db.get(User, member.user_id)
        assert created_user is not None
        assert created_user.tenant_id == tenant.id
        assert created_user.registration_source == "dingtalk_org_sync"


@pytest.mark.asyncio
async def test_org_sync_refreshes_real_name_without_changing_username_or_nickname():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"
    username = f"stable_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        identity = Identity(username=username, phone=phone, password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="旧昵称",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            user_id=user.id,
            external_id=external_id,
            unionid=unionid,
            name="旧昵称",
            nickname="旧昵称",
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        user_id = user.id
        member_id = member.id

    external_user = ExternalUser(
        external_id=external_id,
        unionid=unionid,
        name="目录真实姓名",
        mobile=phone,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        refreshed_user = await db.get(User, user_id)
        refreshed_member = await db.get(OrgMember, member_id)
        refreshed_identity = await db.get(Identity, refreshed_user.identity_id)

        assert refreshed_identity.username == username
        assert refreshed_user.display_name == "目录真实姓名"
        assert refreshed_member.name == "目录真实姓名"
        assert refreshed_member.nickname == "旧昵称"


@pytest.mark.asyncio
async def test_org_sync_persists_contact_when_identity_reconciliation_conflicts():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    shared_email = f"shared-{uuid.uuid4().hex[:8]}@example.com"
    await _seed_user(tenant.id, email=shared_email, phone=f"8613{uuid.uuid4().int % 10**9:09d}")
    await _seed_user(tenant.id, email=f"mobile-{uuid.uuid4().hex[:8]}@example.com", phone=phone)

    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="手机号优先",
        mobile=phone,
        email=shared_email,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["identity_conflict"] is True
        assert member.email == shared_email
        assert member.phone == phone


@pytest.mark.asyncio
async def test_org_sync_missing_mobile_creates_verified_email_anchor():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)

    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="无手机号成员",
        mobile="",
        email=f"nomobile-{uuid.uuid4().hex[:8]}@example.com",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is True
        assert stats["user_skipped_no_phone"] is False
        assert member.user_id is not None
        canonical_user = await db.get(User, member.user_id)
        assert canonical_user.identity_id is not None


@pytest.mark.asyncio
async def test_org_sync_non_dingtalk_still_links_existing_user_by_email():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id, "feishu")
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    email = f"feishu-{uuid.uuid4().hex[:8]}@example.com"
    existing_user = await _seed_user(tenant.id, email=email)

    external_user = ExternalUser(
        external_id=f"fs_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="飞书已有用户",
        mobile="",
        email=email,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is False
        assert stats["user_linked"] is True
        assert member.user_id == existing_user.id


@pytest.mark.asyncio
async def test_org_sync_dingtalk_missing_mobile_preserves_display_phone_without_reusing_it():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    old_phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=external_id,
            unionid=unionid,
            name="历史手机号成员",
            phone=old_phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    external_user = ExternalUser(
        external_id=external_id,
        unionid=unionid,
        name="历史手机号成员",
        mobile="",
        email="",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = await db.get(OrgMember, member_id)
        assert stats["user_created"] is True
        assert stats["user_skipped_no_phone"] is False
        assert member.phone == old_phone
        assert member.user_id is not None
        canonical_user = await db.get(User, member.user_id)
        assert canonical_user.identity_id is not None
        identity = await db.get(Identity, canonical_user.identity_id)
        assert identity.phone is None


@pytest.mark.asyncio
async def test_dingtalk_user_list_uses_org_email_without_collapsing_raw_fields(
    monkeypatch,
):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, params=None, json=None):
            assert url == DingTalkOrgSyncAdapter.DINGTALK_USER_LIST_URL
            return _FakeDingTalkResponse(
                {
                    "errcode": 0,
                    "result": {
                        "list": [
                            {
                                "userid": "staff-org-email",
                                "name": "Org Email User",
                                "email": "",
                                "org_email": "Org.User@Example.COM",
                                "mobile": "13800138000",
                                "dept_id_list": [42],
                            }
                        ],
                        "has_more": False,
                    },
                }
            )

    async def fake_token():
        return "token"

    async def no_sleep(_seconds):
        return None

    adapter = DingTalkOrgSyncAdapter(
        config={"app_key": "key", "app_secret": "secret"}
    )
    adapter.get_access_token = fake_token
    monkeypatch.setattr("app.services.org_sync_adapter.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("app.services.org_sync_adapter.asyncio.sleep", no_sleep)

    users = await adapter.fetch_users("42")

    assert len(users) == 1
    assert users[0].email == "Org.User@Example.COM"
    assert users[0].raw_data["email"] == ""
    assert users[0].raw_data["org_email"] == "Org.User@Example.COM"


@pytest.mark.asyncio
async def test_org_sync_dingtalk_backfills_legacy_member_tenant_before_provisioning():
    tenant = await _seed_tenant()
    provider = await _seed_dingtalk_provider(tenant.id)
    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    phone = f"8613{uuid.uuid4().int % 10**9:09d}"
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        member = OrgMember(
            tenant_id=None,
            provider_id=provider.id,
            external_id=external_id,
            unionid=unionid,
            name="历史空租户成员",
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    external_user = ExternalUser(
        external_id=external_id,
        unionid=unionid,
        name="历史空租户成员",
        mobile=phone,
        email="",
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = await db.get(OrgMember, member_id)
        assert stats["user_created"] is True
        assert member.tenant_id == tenant.id
        assert member.user_id is not None


@pytest.mark.asyncio
async def test_org_sync_dingtalk_auto_create_disabled_does_not_link_by_email():
    tenant = await _seed_tenant()
    email = f"dingtalk-email-disabled-{uuid.uuid4().hex[:8]}@example.com"
    existing_user = await _seed_user(tenant.id, email=email)

    async with async_session() as db:
        provider = IdentityProvider(
            provider_type="dingtalk",
            name=f"DingTalk Disabled {uuid.uuid4().hex[:6]}",
            is_active=True,
            config={"auto_create_users_on_sync": False},
            tenant_id=tenant.id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)

    adapter = _DummyAdapter(provider=provider, tenant_id=tenant.id)
    external_user = ExternalUser(
        external_id=f"dt_{uuid.uuid4().hex[:8]}",
        unionid=f"union_{uuid.uuid4().hex[:8]}",
        name="禁用自动创建",
        mobile="",
        email=email,
    )

    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider.id)
        stats = await adapter._upsert_member(db, provider, external_user, "1")
        await db.commit()

        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_user.external_id,
                )
            )
        ).scalar_one()
        assert stats["user_created"] is False
        assert stats["user_linked"] is False
        assert member.user_id is None
        assert member.user_id != existing_user.id


@pytest.mark.asyncio
async def test_base_adapter_persists_multiple_parent_groups_for_any_provider():
    tenant = await _seed_tenant()
    seeded_provider = await _seed_provider(tenant.id, "feishu")
    adapter = _DummyAdapter(provider=seeded_provider, tenant_id=tenant.id)

    async with async_session() as db:
        provider = await db.get(IdentityProvider, seeded_provider.id)
        await adapter._upsert_department(
            db, provider, ExternalDepartment(external_id="root-a", name="Root A")
        )
        await adapter._upsert_department(
            db, provider, ExternalDepartment(external_id="root-b", name="Root B")
        )
        await adapter._upsert_department(
            db,
            provider,
            ExternalDepartment(
                external_id="child",
                name="Child",
                parent_external_ids=["root-a", "root-b"],
            ),
        )
        await db.commit()

        child = await db.scalar(
            select(OrgDepartment).where(
                OrgDepartment.provider_id == provider.id,
                OrgDepartment.external_id == "child",
            )
        )
        root_a = await db.scalar(
            select(OrgDepartment).where(
                OrgDepartment.provider_id == provider.id,
                OrgDepartment.external_id == "root-a",
            )
        )
        edge_count = await db.scalar(
            select(func.count()).select_from(DirectoryGroupEdge).where(
                DirectoryGroupEdge.provider_id == provider.id,
                DirectoryGroupEdge.child_group_id == child.id,
            )
        )
        assert child.parent_id == root_a.id
        assert edge_count == 2
