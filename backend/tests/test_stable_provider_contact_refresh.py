"""Stable provider subjects remain the identity anchor after contact changes."""

import uuid

import pytest
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.audit import AuditLog
from app.models.identity import IdentityProvider
from app.models.org import ChannelUserBinding, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.auth_provider import BaseAuthProvider, ExternalUserInfo, OAuth2AuthProvider


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _login(
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
    *,
    subject: str,
    email: str,
    phone: str,
) -> tuple[uuid.UUID, uuid.UUID, bool]:
    async with async_session() as db:
        provider_row = await db.get(IdentityProvider, provider_id)
        provider = OAuth2AuthProvider(provider=provider_row)
        user, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=subject,
                name="Stable Provider User",
                email=email,
                mobile=phone,
                raw_data={
                    "sub": subject,
                    "email": email,
                    "phone_number": phone,
                },
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        return user.id, user.identity_id, created


async def test_bound_subject_refreshes_contacts_without_reidentification():
    suffix = uuid.uuid4().hex[:10]
    subject = f"stable-subject-{suffix}"
    original_email = f"before-{suffix}@example.com"
    changed_email = f"after-{suffix}@example.com"
    original_phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    changed_phone = f"8617{uuid.uuid4().int % 10**9:09d}"

    async with async_session() as db:
        tenant = Tenant(
            name=f"Stable Subject {suffix}",
            slug=f"stable-subject-{suffix}",
            im_provider="web_only",
        )
        identity = Identity(
            username=f"stable-{suffix}",
            email=original_email,
            phone=original_phone,
            email_verified=True,
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Existing Contact Owner",
            role="member",
            source="web",
            is_active=True,
        )
        provider = IdentityProvider(
            provider_type="oauth2",
            name=f"Stable OAuth {suffix}",
            tenant_id=tenant.id,
            is_active=True,
            sso_login_enabled=True,
            config={
                "identity_match_policy": {
                    "ordered_fields": ["phone", "email"],
                }
            },
        )
        db.add_all([user, provider])
        await db.commit()
        tenant_id = tenant.id
        provider_id = provider.id
        expected_user_id = user.id
        expected_identity_id = identity.id
        identity_count_before = await db.scalar(select(func.count(Identity.id)))

    # No provider binding exists yet: the first login may use configured
    # phone/email matching to attach this stable subject to the existing user.
    first_user_id, first_identity_id, first_created = await _login(
        tenant_id,
        provider_id,
        subject=subject,
        email=original_email.upper(),
        phone=f"+{original_phone[:2]} {original_phone[2:]}",
    )
    assert (first_user_id, first_identity_id, first_created) == (
        expected_user_id,
        expected_identity_id,
        False,
    )

    # Once bound, the exact provider subject is authoritative. Changed contact
    # attributes refresh the same physical Identity instead of re-identifying.
    second_user_id, second_identity_id, second_created = await _login(
        tenant_id,
        provider_id,
        subject=subject,
        email=changed_email.upper(),
        phone=changed_phone,
    )
    assert (second_user_id, second_identity_id, second_created) == (
        expected_user_id,
        expected_identity_id,
        False,
    )

    async with async_session() as db:
        refreshed = await db.get(Identity, expected_identity_id)
        assert refreshed.email == changed_email
        assert refreshed.phone == changed_phone
        assert await db.scalar(select(func.count(Identity.id))) == identity_count_before
        assert await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        ) == 1
        assert await db.scalar(
            select(func.count(ChannelUserBinding.id)).where(
                ChannelUserBinding.tenant_id == tenant_id,
                ChannelUserBinding.provider_id == provider_id,
                ChannelUserBinding.subject == subject,
                ChannelUserBinding.user_id == expected_user_id,
            )
        ) == 1
        assert await db.scalar(
            select(func.count(OrgMember.id)).where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.provider_id == provider_id,
                OrgMember.user_id == expected_user_id,
            )
        ) == 1
        assert await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.user_id == expected_user_id,
                AuditLog.action.in_((
                    "identity_match_lower_priority_conflict",
                    "provider_subject_contact_conflict",
                )),
            )
        ) == 0


class _WeComProvider(BaseAuthProvider):
    provider_type = "wecom"

    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        return redirect_uri

    async def exchange_code_for_token(
        self, code: str, redirect_uri: str | None = None
    ) -> dict:
        return {"access_token": code}

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        raise NotImplementedError


async def test_non_oauth_sso_refreshes_by_exact_provider_account():
    suffix = uuid.uuid4().hex[:10]
    subject = f"wecom-user-{suffix}"
    old_email = f"wecom-old-{suffix}@example.com"
    new_email = f"wecom-new-{suffix}@example.com"
    old_phone = f"8618{uuid.uuid4().int % 10**9:09d}"
    new_phone = f"8617{uuid.uuid4().int % 10**9:09d}"

    async with async_session() as db:
        tenant = Tenant(name=f"WeCom {suffix}", slug=f"wecom-{suffix}")
        identity = Identity(email=old_email, phone=old_phone, email_verified=True)
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="WeCom User",
            role="member",
            is_active=True,
        )
        provider = IdentityProvider(
            provider_type="wecom",
            name=f"WeCom {suffix}",
            tenant_id=tenant.id,
            is_active=True,
            sso_login_enabled=True,
            config={},
        )
        db.add_all([user, provider])
        await db.flush()
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=subject,
            user_id=user.id,
            name="WeCom User",
            email=old_email,
            phone=old_phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        tenant_id = tenant.id
        provider_id = provider.id
        user_id = user.id
        identity_id = identity.id

    async with async_session() as db:
        provider_row = await db.get(IdentityProvider, provider_id)
        resolved, created = await _WeComProvider(provider=provider_row).find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="wecom",
                provider_user_id=subject,
                name="WeCom User",
                email=new_email,
                mobile=new_phone,
                raw_data={"userid": subject, "email": new_email, "mobile": new_phone},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        assert resolved.id == user_id
        assert resolved.identity_id == identity_id
        assert created is False

    async with async_session() as db:
        refreshed = await db.get(Identity, identity_id)
        assert refreshed.email == new_email
        assert refreshed.phone == new_phone
        assert await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        ) == 1
