from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.auth_provider import ExternalUserInfo, OAuth2AuthProvider
from app.services.oauth_identity import (
    oauth_authority_scope,
    parse_oauth2_sso_state,
    sign_oauth2_sso_state,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_bound_user(
    *,
    email: str,
    subject: str,
    phone: str,
    provider_name: str = "Enterprise OAuth",
    provider_config: dict | None = None,
    active: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with async_session() as db:
        tenant = Tenant(name="OAuth Authority", slug=f"oauth-authority-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.flush()
        provider = IdentityProvider(
            provider_type="oauth2",
            name=provider_name,
            is_active=True,
            sso_login_enabled=True,
            tenant_id=tenant.id,
            config=provider_config or {},
        )
        identity = Identity(
            username=f"oauth-{uuid.uuid4().hex[:10]}",
            email=email,
            phone=phone,
            email_verified=True,
        )
        db.add_all([provider, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Bound OAuth User",
            role="member",
            is_active=active,
        )
        db.add(user)
        await db.flush()
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=subject,
            name="Bound OAuth User",
            email=email,
            phone=phone,
            user_id=user.id,
            status="active",
        )
        binding = ChannelUserBinding(
            tenant_id=tenant.id,
            provider_id=provider.id,
            installation_scope=oauth_authority_scope(provider),
            channel_type="oauth2",
            id_type="subject",
            subject=subject,
            user_id=user.id,
        )
        db.add_all([member, binding])
        await db.commit()
        return tenant.id, provider.id, identity.id, user.id


async def _login(
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
    *,
    subject: str,
    email: str | None,
    phone: str,
) -> tuple[uuid.UUID, bool]:
    async with async_session() as db:
        provider_row = await db.get(IdentityProvider, provider_id)
        provider = OAuth2AuthProvider(provider=provider_row)
        user, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=subject,
                name="Bound OAuth User",
                email=email,
                mobile=phone,
                raw_data={"userId": subject, "email": email, "mobile": phone},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        return user.id, created


async def test_exact_oauth_binding_refreshes_email_and_all_projections_atomically():
    suffix = uuid.uuid4().hex[:10]
    old_email = f"old-{suffix}@example.com"
    new_email = f"new-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, user_id = await _seed_bound_user(
        email=old_email,
        subject=subject,
        phone=phone,
    )

    resolved_user_id, created = await _login(
        tenant_id,
        provider_id,
        subject=subject,
        email=new_email.upper(),
        phone=phone,
    )

    assert resolved_user_id == user_id
    assert created is False
    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        assert identity.email == new_email
        assert identity.email_verified is True
        members = (
            await db.execute(select(OrgMember).where(OrgMember.user_id == user_id).order_by(OrgMember.id))
        ).scalars().all()
        assert {member.email for member in members} == {new_email}
        audits = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.user_id == user_id,
                    AuditLog.action == "oauth_identity_email_refreshed",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        assert audits[0].details["old_email"] == old_email
        assert audits[0].details["new_email"] == new_email


async def test_exact_oauth_binding_is_idempotent_without_duplicate_audit():
    suffix = uuid.uuid4().hex[:10]
    email = f"same-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, _identity_id, user_id = await _seed_bound_user(
        email=email,
        subject=subject,
        phone=phone,
    )

    await _login(tenant_id, provider_id, subject=subject, email=email, phone=phone)
    await _login(tenant_id, provider_id, subject=subject, email=email.upper(), phone=phone)

    async with async_session() as db:
        audit_count = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.user_id == user_id,
                AuditLog.action == "oauth_identity_email_refreshed",
            )
        )
        assert audit_count == 0


async def test_first_login_without_trust_binding_does_not_overwrite_conflicting_email():
    suffix = uuid.uuid4().hex[:10]
    old_email = f"first-old-{suffix}@example.com"
    new_email = f"first-new-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _user_id = await _seed_bound_user(
        email=old_email,
        subject=subject,
        phone=phone,
    )
    async with async_session() as db:
        await db.execute(
            ChannelUserBinding.__table__.delete().where(
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.subject == subject,
            )
        )
        await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _login(tenant_id, provider_id, subject=subject, email=new_email, phone=phone)
    assert exc_info.value.status_code == 409

    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == old_email
        binding_count = await db.scalar(
            select(func.count(ChannelUserBinding.id)).where(
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.subject == subject,
            )
        )
        assert binding_count == 0


async def test_provider_authority_change_invalidates_old_binding_for_email_refresh():
    suffix = uuid.uuid4().hex[:10]
    old_email = f"scope-old-{suffix}@example.com"
    new_email = f"scope-new-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _user_id = await _seed_bound_user(
        email=old_email,
        subject=subject,
        phone=phone,
        provider_config={"field_mapping": {"user_id": "userId", "email": "email"}},
    )
    async with async_session() as db:
        provider = await db.get(IdentityProvider, provider_id)
        provider.config = {"field_mapping": {"user_id": "employeeId", "email": "workEmail"}}
        await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _login(tenant_id, provider_id, subject=subject, email=new_email, phone=phone)
    assert exc_info.value.status_code == 409

    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == old_email
        binding_count = await db.scalar(
            select(func.count(ChannelUserBinding.id)).where(
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.subject == subject,
            )
        )
        assert binding_count == 1


async def test_concurrent_first_logins_converge_to_one_subject_binding_and_user():
    suffix = uuid.uuid4().hex[:10]
    email = f"concurrent-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    async with async_session() as db:
        tenant = Tenant(name="OAuth Concurrent", slug=f"oauth-concurrent-{suffix}")
        db.add(tenant)
        await db.flush()
        provider = IdentityProvider(
            provider_type="oauth2",
            name="Concurrent OAuth",
            is_active=True,
            sso_login_enabled=True,
            tenant_id=tenant.id,
            config={},
        )
        db.add(provider)
        await db.commit()
        tenant_id = tenant.id
        provider_id = provider.id

    results = await asyncio.gather(
        _login(tenant_id, provider_id, subject=subject, email=email, phone=phone),
        _login(tenant_id, provider_id, subject=subject, email=email, phone=phone),
    )
    assert len({user_id for user_id, _created in results}) == 1

    async with async_session() as db:
        binding_count = await db.scalar(
            select(func.count(ChannelUserBinding.id)).where(
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.subject == subject,
            )
        )
        assert binding_count == 1


async def test_exact_oauth_binding_rejects_email_owned_by_another_identity_without_partial_writes():
    suffix = uuid.uuid4().hex[:10]
    old_email = f"owner-old-{suffix}@example.com"
    occupied_email = f"occupied-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, user_id = await _seed_bound_user(
        email=old_email,
        subject=subject,
        phone=phone,
    )
    async with async_session() as db:
        db.add(Identity(username=f"other-{suffix}", email=occupied_email, email_verified=True))
        await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _login(
            tenant_id,
            provider_id,
            subject=subject,
            email=occupied_email,
            phone=phone,
        )
    assert exc_info.value.status_code == 409

    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        assert identity.email == old_email
        member_emails = set(
            (
                await db.execute(select(OrgMember.email).where(OrgMember.user_id == user_id))
            ).scalars().all()
        )
        assert member_emails == {old_email}
        audit_count = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.user_id == user_id,
                AuditLog.action == "oauth_identity_email_refreshed",
            )
        )
        assert audit_count == 0


async def test_same_subject_is_isolated_between_exact_oauth_providers():
    suffix = uuid.uuid4().hex[:10]
    subject = f"shared-subject-{suffix}"
    phone_one = f"8618{uuid.uuid4().int % 10**9:09d}"
    phone_two = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_one_id, identity_one_id, user_one_id = await _seed_bound_user(
        email=f"one-{suffix}@example.com",
        subject=subject,
        phone=phone_one,
        provider_name="OAuth One",
        provider_config={"client_id": "client-one"},
    )
    async with async_session() as db:
        provider_two = IdentityProvider(
            provider_type="oauth2",
            name="OAuth Two",
            is_active=True,
            sso_login_enabled=True,
            tenant_id=tenant_id,
            config={"client_id": "client-two"},
        )
        identity_two = Identity(
            username=f"two-{suffix}",
            email=f"two-{suffix}@example.com",
            phone=phone_two,
            email_verified=True,
        )
        db.add_all([provider_two, identity_two])
        await db.flush()
        user_two = User(
            identity_id=identity_two.id,
            tenant_id=tenant_id,
            display_name="OAuth Two User",
            role="member",
            is_active=True,
        )
        db.add(user_two)
        await db.flush()
        db.add_all(
            [
                OrgMember(
                    tenant_id=tenant_id,
                    provider_id=provider_two.id,
                    external_id=subject,
                    name="OAuth Two User",
                    email=identity_two.email,
                    phone=phone_two,
                    user_id=user_two.id,
                    status="active",
                ),
                ChannelUserBinding(
                    tenant_id=tenant_id,
                    provider_id=provider_two.id,
                    installation_scope=oauth_authority_scope(provider_two),
                    channel_type="oauth2",
                    id_type="subject",
                    subject=subject,
                    user_id=user_two.id,
                ),
            ]
        )
        await db.commit()
        identity_two_id = identity_two.id

    new_email = f"one-new-{suffix}@example.com"
    resolved_user_id, _ = await _login(
        tenant_id,
        provider_one_id,
        subject=subject,
        email=new_email,
        phone=phone_one,
    )
    assert resolved_user_id == user_one_id

    async with async_session() as db:
        assert (await db.get(Identity, identity_one_id)).email == new_email
        assert (await db.get(Identity, identity_two_id)).email == f"two-{suffix}@example.com"


async def test_exact_subject_with_different_user_projection_is_rejected():
    suffix = uuid.uuid4().hex[:10]
    email = f"projection-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _user_id = await _seed_bound_user(
        email=email,
        subject=subject,
        phone=phone,
    )
    async with async_session() as db:
        other_identity = Identity(
            username=f"projection-other-{suffix}",
            email=f"projection-other-{suffix}@example.com",
            phone=f"8617{uuid.uuid4().int % 10**9:09d}",
            email_verified=True,
        )
        db.add(other_identity)
        await db.flush()
        other_user = User(
            identity_id=other_identity.id,
            tenant_id=tenant_id,
            display_name="Projection Conflict",
            role="member",
            is_active=True,
        )
        db.add(other_user)
        await db.flush()
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id=subject,
                name="Projection Conflict",
                email=other_identity.email,
                phone=other_identity.phone,
                user_id=other_user.id,
                status="active",
            )
        )
        await db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _login(
            tenant_id,
            provider_id,
            subject=subject,
            email=f"projection-new-{suffix}@example.com",
            phone=phone,
        )
    assert exc_info.value.status_code == 409
    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == email


async def test_single_wrong_user_projection_is_rejected_before_it_can_be_rebound():
    suffix = uuid.uuid4().hex[:10]
    email = f"single-projection-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _trusted_user_id = await _seed_bound_user(
        email=email,
        subject=subject,
        phone=phone,
    )
    async with async_session() as db:
        other_identity = Identity(
            username=f"single-projection-other-{suffix}",
            email=f"single-projection-other-{suffix}@example.com",
            phone=f"8617{uuid.uuid4().int % 10**9:09d}",
            email_verified=True,
        )
        db.add(other_identity)
        await db.flush()
        other_user = User(
            identity_id=other_identity.id,
            tenant_id=tenant_id,
            display_name="Single Projection Conflict",
            role="member",
            is_active=True,
        )
        db.add(other_user)
        await db.flush()
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider_id,
                    OrgMember.external_id == subject,
                )
            )
        ).scalar_one()
        member.user_id = other_user.id
        await db.commit()
        other_user_id = other_user.id

    with pytest.raises(HTTPException) as exc_info:
        await _login(
            tenant_id,
            provider_id,
            subject=subject,
            email=f"single-projection-new-{suffix}@example.com",
            phone=phone,
        )
    assert exc_info.value.status_code == 409
    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == email
        member_user_id = await db.scalar(
            select(OrgMember.user_id).where(
                OrgMember.provider_id == provider_id,
                OrgMember.external_id == subject,
            )
        )
        assert member_user_id == other_user_id


@pytest.mark.parametrize("incoming_email", [None, ""])
async def test_exact_oauth_binding_keeps_existing_email_when_claim_is_empty(incoming_email):
    suffix = uuid.uuid4().hex[:10]
    email = f"keep-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _user_id = await _seed_bound_user(
        email=email,
        subject=subject,
        phone=phone,
    )

    await _login(tenant_id, provider_id, subject=subject, email=incoming_email, phone=phone)
    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == email


async def test_exact_oauth_binding_rejects_invalid_email_before_writes():
    suffix = uuid.uuid4().hex[:10]
    email = f"valid-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _user_id = await _seed_bound_user(
        email=email,
        subject=subject,
        phone=phone,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _login(tenant_id, provider_id, subject=subject, email="not-an-email", phone=phone)
    assert exc_info.value.status_code == 502
    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == email


async def test_disabled_exact_oauth_user_is_rejected_before_email_refresh():
    suffix = uuid.uuid4().hex[:10]
    email = f"disabled-{suffix}@example.com"
    subject = f"subject-{suffix}"
    phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    tenant_id, provider_id, identity_id, _user_id = await _seed_bound_user(
        email=email,
        subject=subject,
        phone=phone,
        active=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _login(
            tenant_id,
            provider_id,
            subject=subject,
            email=f"disabled-new-{suffix}@example.com",
            phone=phone,
        )
    assert exc_info.value.status_code == 403
    async with async_session() as db:
        assert (await db.get(Identity, identity_id)).email == email


async def test_authority_fingerprint_changes_when_subject_or_email_mapping_changes():
    provider_id = uuid.uuid4()
    base = IdentityProvider(
        id=provider_id,
        provider_type="oauth2",
        name="Fingerprint",
        tenant_id=uuid.uuid4(),
        config={"client_id": "client", "field_mapping": {"user_id": "userId", "email": "email"}},
    )
    same = IdentityProvider(
        id=provider_id,
        provider_type="oauth2",
        name="Renamed Only",
        tenant_id=base.tenant_id,
        config={"client_id": "client", "field_mapping": {"email": "email", "user_id": "userId"}},
    )
    changed_subject = IdentityProvider(
        id=provider_id,
        provider_type="oauth2",
        name="Fingerprint",
        tenant_id=base.tenant_id,
        config={"client_id": "client", "field_mapping": {"user_id": "sub", "email": "email"}},
    )
    changed_email = IdentityProvider(
        id=provider_id,
        provider_type="oauth2",
        name="Fingerprint",
        tenant_id=base.tenant_id,
        config={"client_id": "client", "field_mapping": {"user_id": "userId", "email": "mail"}},
    )

    assert oauth_authority_scope(base) == oauth_authority_scope(same)
    assert oauth_authority_scope(base) != oauth_authority_scope(changed_subject)
    assert oauth_authority_scope(base) != oauth_authority_scope(changed_email)


async def test_oauth_sso_state_binds_session_and_exact_provider_and_rejects_tampering():
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    state = sign_oauth2_sso_state(session_id, provider_id)

    assert parse_oauth2_sso_state(state) == (session_id, provider_id)
    tampered_last_char = "0" if state[-1] != "0" else "1"
    assert parse_oauth2_sso_state(f"{state[:-1]}{tampered_last_char}") is None
    assert parse_oauth2_sso_state(str(session_id)) is None
