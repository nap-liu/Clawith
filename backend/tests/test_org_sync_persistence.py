"""Org-sync persistence and canonical-user integration tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.user import Identity, User
from app.services.dingtalk_identity_reconciliation import dingtalk_legacy_identity_reconciler
from app.services.org_sync_adapter import DingTalkOrgSyncAdapter, ExternalUser
from tests.test_org_sync_adapter import (
    _DummyAdapter,
    _FakeDingTalkResponse,
    _isolate_async_engine_between_tests,
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
async def test_org_sync_missing_mobile_creates_external_only_canonical_user():
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
        assert canonical_user.identity_id is None


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
        assert canonical_user.identity_id is None


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
