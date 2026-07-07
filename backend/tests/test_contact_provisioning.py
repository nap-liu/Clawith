from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.contact_provisioning import contact_provisioning


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _phone() -> str:
    return f"8613{uuid.uuid4().int % 10**9:09d}"


async def _seed_tenant(name: str = "Tenant") -> Tenant:
    async with async_session() as db:
        tenant = Tenant(name=name, slug=f"tenant-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)
        return tenant


async def _seed_provider(tenant_id: uuid.UUID, provider_type: str = "dingtalk") -> IdentityProvider:
    async with async_session() as db:
        provider = IdentityProvider(
            provider_type=provider_type,
            name=f"{provider_type}-{uuid.uuid4().hex[:6]}",
            is_active=True,
            config={},
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider


async def _seed_org_member(
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
    *,
    phone: str | None,
    email: str | None = None,
    name: str = "DingTalk User",
    external_id: str | None = None,
) -> OrgMember:
    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=provider_id,
            name=name,
            external_id=external_id or f"dt_{uuid.uuid4().hex[:10]}",
            unionid=f"union_{uuid.uuid4().hex[:10]}",
            phone=phone,
            email=email,
            status="active",
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        return member


async def test_provision_org_member_creates_user_identity_participant_by_mobile():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    phone = _phone()
    raw_phone = f"+{phone[:2]} {phone[2:]}"
    member = await _seed_org_member(
        tenant.id,
        provider.id,
        phone=raw_phone,
        email=None,
        name="张三",
        external_id="dt_create_user",
    )

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user_created is True
        assert result.skipped_reason is None
        assert member.user_id == result.user.id

        user = (
            await db.execute(
                select(User)
                .where(User.id == member.user_id)
                .options(selectinload(User.identity))
            )
        ).scalar_one()
        assert user.tenant_id == tenant.id
        assert user.display_name == "张三"
        assert user.role == "member"
        assert user.registration_source == "dingtalk_org_sync"
        assert user.source == "dingtalk"
        assert user.identity.phone == phone
        assert user.identity.email == f"dingtalk_{provider.id.hex}_dt_create_user@dingtalk.local"

        participant_count = (
            await db.execute(
                select(func.count())
                .select_from(Participant)
                .where(Participant.type == "user", Participant.ref_id == user.id)
            )
        ).scalar_one()
        assert participant_count == 1


async def test_provision_org_member_reuses_global_identity_but_creates_current_tenant_user():
    other_tenant = await _seed_tenant("Other")
    current_tenant = await _seed_tenant("Current")
    provider = await _seed_provider(current_tenant.id)
    phone = _phone()

    async with async_session() as db:
        identity = Identity(
            username=f"existing_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        other_user = User(
            identity_id=identity.id,
            tenant_id=other_tenant.id,
            display_name="Other Tenant User",
            role="member",
            is_active=True,
        )
        db.add(other_user)
        await db.commit()
        identity_id = identity.id
        other_user_id = other_user.id

    member = await _seed_org_member(current_tenant.id, provider.id, phone=phone)

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user_created is True
        assert result.global_identity_matched_no_tenant_user is True
        assert result.user.id != other_user_id
        assert result.user.identity_id == identity_id
        assert result.user.tenant_id == current_tenant.id
        assert member.user_id == result.user.id


async def test_provision_org_member_links_existing_user_by_normalized_mobile():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    normalized_phone = _phone()
    raw_phone = f"+{normalized_phone[:2]} {normalized_phone[2:5]}-{normalized_phone[5:]}"

    async with async_session() as db:
        identity = Identity(
            username=f"linked_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=normalized_phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Existing Mobile User",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    member = await _seed_org_member(tenant.id, provider.id, phone=raw_phone)

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user_created is False
        assert result.user_linked is True
        assert result.tenant_user_matched is True
        assert result.user.id == user_id
        assert member.user_id == user_id


async def test_provision_org_member_phone_match_replaces_stale_existing_user_link():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    stale_phone = _phone()
    correct_phone = _phone()

    async with async_session() as db:
        stale_identity = Identity(
            username=f"stale_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=stale_phone,
            password_hash="x",
        )
        correct_identity = Identity(
            username=f"correct_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=correct_phone,
            password_hash="x",
        )
        db.add_all([stale_identity, correct_identity])
        await db.flush()
        stale_user = User(
            identity_id=stale_identity.id,
            tenant_id=tenant.id,
            display_name="Stale User",
            role="member",
            is_active=True,
        )
        correct_user = User(
            identity_id=correct_identity.id,
            tenant_id=tenant.id,
            display_name="Correct User",
            role="member",
            is_active=True,
        )
        db.add_all([stale_user, correct_user])
        await db.commit()
        stale_user_id = stale_user.id
        correct_user_id = correct_user.id

    member = await _seed_org_member(tenant.id, provider.id, phone=correct_phone)
    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        member.user_id = stale_user_id
        await db.commit()

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user.id == correct_user_id
        assert result.user.id != stale_user_id
        assert member.user_id == correct_user_id


async def test_provision_org_member_keeps_existing_active_link_when_no_phone_match_exists():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    org_phone = _phone()

    async with async_session() as db:
        identity = Identity(
            username=f"manual_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=None,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Manual Link",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    member = await _seed_org_member(tenant.id, provider.id, phone=org_phone)
    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        member.user_id = user_id
        await db.commit()

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user.id == user_id
        assert result.user_created is False
        assert result.user_linked is True
        assert member.user_id == user_id
        assert result.user.identity.phone == org_phone


async def test_provision_org_member_normalizes_existing_link_phone():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    normalized_phone = _phone()
    unnormalized_phone = f"+{normalized_phone[:2]} {normalized_phone[2:5]} {normalized_phone[5:]}"

    async with async_session() as db:
        identity = Identity(
            username=f"manual_norm_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=unnormalized_phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Manual Link",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    member = await _seed_org_member(tenant.id, provider.id, phone=normalized_phone)
    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        member.user_id = user_id
        await db.commit()

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user.id == user_id
        assert result.user.identity.phone == normalized_phone


async def test_provision_org_member_does_not_overwrite_existing_link_with_other_identity_phone():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    owned_phone = _phone()

    async with async_session() as db:
        linked_identity = Identity(
            username=f"linked_owner_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=None,
            password_hash="x",
        )
        other_identity = Identity(
            username=f"other_owner_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=owned_phone,
            password_hash="x",
        )
        db.add_all([linked_identity, other_identity])
        await db.flush()
        user = User(
            identity_id=linked_identity.id,
            tenant_id=tenant.id,
            display_name="Manual Link",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    member = await _seed_org_member(tenant.id, provider.id, phone=owned_phone)
    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        member.user_id = user_id
        await db.commit()

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user.id == user_id
        assert result.user.identity.phone is None
        assert member.user_id == user_id


async def test_provision_org_member_does_not_link_inactive_tenant_user():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    phone = _phone()

    async with async_session() as db:
        identity = Identity(
            username=f"inactive_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        db.add(
            User(
                identity_id=identity.id,
                tenant_id=tenant.id,
                display_name="Inactive User",
                role="member",
                is_active=False,
            )
        )
        await db.commit()

    member = await _seed_org_member(tenant.id, provider.id, phone=phone)

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user is None
        assert result.user_created is False
        assert result.skipped_reason == "skipped_requires_confirmation"
        assert member.user_id is None


async def test_provision_org_member_skips_create_without_mobile():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    member = await _seed_org_member(tenant.id, provider.id, phone=None, email="nomobile@example.com")

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user is None
        assert result.user_created is False
        assert result.skipped_reason == "missing_mobile"
        assert member.user_id is None


async def test_provision_org_member_upserts_participant_idempotently():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    phone = _phone()

    async with async_session() as db:
        identity = Identity(
            username=f"participant_{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            phone=phone,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Existing User",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        db.add(Participant(type="user", ref_id=user.id, display_name=user.display_name))
        await db.commit()
        user_id = user.id

    member = await _seed_org_member(tenant.id, provider.id, phone=phone)

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        result = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()

        assert result.user_created is False
        assert result.user_linked is True
        assert result.user.id == user_id
        assert member.user_id == user_id
        participant_count = (
            await db.execute(
                select(func.count())
                .select_from(Participant)
                .where(Participant.type == "user", Participant.ref_id == user_id)
            )
        ).scalar_one()
        assert participant_count == 1


async def test_provision_org_member_upgrades_placeholder_email_without_overwriting_real_email():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    member = await _seed_org_member(
        tenant.id,
        provider.id,
        phone=_phone(),
        email=None,
        external_id="dt_email_upgrade",
    )

    async with async_session() as db:
        member = await db.get(OrgMember, member.id)
        first = await contact_provisioning.ensure_user_for_org_member(db, member)
        assert first.user.identity.email == f"dingtalk_{provider.id.hex}_dt_email_upgrade@dingtalk.local"

        real_email = f"real-{uuid.uuid4().hex[:10]}@example.com"
        member.email = real_email
        second = await contact_provisioning.ensure_user_for_org_member(db, member)
        assert second.user.identity.email == real_email

        member.email = None
        third = await contact_provisioning.ensure_user_for_org_member(db, member)
        await db.commit()
        assert third.user.identity.email == real_email
