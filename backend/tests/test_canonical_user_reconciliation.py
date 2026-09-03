from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.agent import Agent, AgentPermission
from app.models.okr import MemberDailyReport
from app.models.org import ChannelUserBinding, OrgMember
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.auth_provider import (
    DingTalkAuthProvider,
    ExternalUserInfo,
    OAuth2AuthProvider,
)
from app.services.canonical_user_resolver import (
    CanonicalIdentityConflict,
    canonical_user_resolver,
)
from app.services.channel_user_service import ChannelUserService
from app.services.contact_provisioning import contact_provisioning
from app.services.directory_identity_claims import VerifiedDirectoryClaims
from app.services.dingtalk_identity_reconciliation import (
    dingtalk_legacy_identity_reconciler,
)
from app.services.feishu_service import feishu_service
from app.services.org_sync_adapter import DingTalkOrgSyncAdapter, ExternalUser
from app.services.registration_service import registration_service


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _phone() -> str:
    return f"8618{uuid.uuid4().int % 10**9:09d}"


async def _tenant_and_providers(
    *,
    bind_oauth_directory: bool = False,
    auto_repair: bool = False,
):
    async with async_session() as db:
        tenant = Tenant(name="Canonical", slug=f"canonical-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.flush()
        dingtalk = IdentityProvider(
            provider_type="dingtalk",
            name="DingTalk Directory",
            is_active=True,
            config={"auto_repair_legacy_identity_split": True} if auto_repair else {},
            tenant_id=tenant.id,
        )
        db.add(dingtalk)
        await db.flush()
        oauth = IdentityProvider(
            provider_type="oauth2",
            name="Enterprise OAuth",
            is_active=True,
            sso_login_enabled=True,
            config=(
                {"directory_provider_id": str(dingtalk.id)}
                if bind_oauth_directory
                else {}
            ),
            tenant_id=tenant.id,
        )
        db.add(oauth)
        await db.commit()
        return tenant.id, dingtalk.id, oauth.id


async def test_split_claims_choose_configured_default_phone_and_flag_email():
    email = f"equal-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    async with async_session() as db:
        email_identity = Identity(username=f"email-{uuid.uuid4().hex[:8]}", email=email)
        phone_identity = Identity(username=f"phone-{uuid.uuid4().hex[:8]}", phone=phone)
        db.add_all([email_identity, phone_identity])
        await db.commit()
        phone_identity_id = phone_identity.id

    async with async_session() as db:
        claims = await canonical_user_resolver.resolve_identity_claims(
            db, email=email.upper(), phone=f"+{phone[:2]} {phone[2:]}", enrich=True
        )
        assert claims.identity.id == phone_identity_id
        assert claims.matched_by == "phone"
        assert claims.conflicting_fields == ("email",)


async def test_legacy_auto_repair_rejects_phone_only_fresh_claims():
    tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        outcome = await dingtalk_legacy_identity_reconciler.reconcile(
            db,
            provider=SimpleNamespace(
                id=provider_id,
                tenant_id=tenant_id,
                provider_type="dingtalk",
            ),
            org_member=SimpleNamespace(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id=external_id,
            ),
            claims=VerifiedDirectoryClaims(
                tenant_id=tenant_id,
                provider_id=provider_id,
                external_id=external_id,
                observed_at=datetime.now(timezone.utc),
                raw_mobile=_phone(),
                source="test",
            ),
            apply=True,
        )
        assert outcome.status == "not_applicable"
        assert outcome.reason == "missing_fresh_corporate_email"


async def test_single_email_match_safely_enriches_phone():
    email = f"enrich-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    async with async_session() as db:
        identity = Identity(username=f"enrich-{uuid.uuid4().hex[:8]}", email=email)
        db.add(identity)
        await db.commit()
        identity_id = identity.id

    async with async_session() as db:
        resolved = await registration_service.find_or_create_identity(
            db,
            email=email.upper(),
            phone=f"+{phone[:2]} {phone[2:]}",
            username="unused",
        )
        await db.commit()
        assert resolved.id == identity_id
        assert resolved.email == email
        assert resolved.phone == phone


async def test_sso_identity_creation_honors_provider_email_first_policy():
    email = f"email-first-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    provider = SimpleNamespace(
        id=uuid.uuid4(),
        provider_type="oauth2",
        config={"identity_match_policy": {"ordered_fields": ["email", "phone"]}},
    )
    async with async_session() as db:
        email_identity = Identity(username=f"email-{uuid.uuid4().hex[:8]}", email=email)
        phone_identity = Identity(username=f"phone-{uuid.uuid4().hex[:8]}", phone=phone)
        db.add_all([email_identity, phone_identity])
        await db.commit()
        expected_id = email_identity.id

    async with async_session() as db:
        resolved = await registration_service.find_or_create_identity(
            db,
            email=email,
            phone=phone,
            provider=provider,
        )
        assert resolved.id == expected_id


async def test_provider_policy_can_disable_lower_priority_email_fallback():
    email = f"phone-only-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session() as db:
        email_identity = Identity(username=f"email-{uuid.uuid4().hex[:8]}", email=email)
        db.add(email_identity)
        await db.commit()

    async with async_session() as db:
        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=email,
            phone=None,
            enrich=False,
            ordered_fields=["phone"],
        )
        assert claims.identity is None
        assert claims.matched_by is None


async def test_provider_policy_can_use_email_as_its_only_match_field():
    email = f"email-only-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    async with async_session() as db:
        email_identity = Identity(username=f"email-{uuid.uuid4().hex[:8]}", email=email)
        phone_identity = Identity(username=f"phone-{uuid.uuid4().hex[:8]}", phone=phone)
        db.add_all([email_identity, phone_identity])
        await db.commit()
        expected_id = email_identity.id

    async with async_session() as db:
        claims = await canonical_user_resolver.resolve_identity_claims(
            db,
            email=email,
            phone=phone,
            enrich=False,
            ordered_fields=["email"],
        )
        assert claims.identity is not None
        assert claims.identity.id == expected_id
        assert claims.matched_by == "email"
        assert claims.conflicting_fields == ()


async def test_fresh_org_email_repairs_strict_legacy_dingtalk_split():
    tenant_id, dingtalk_id, _oauth_id = await _tenant_and_providers()
    email = f"legacy-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    scope = f"provider:{dingtalk_id}"

    async with async_session() as db:
        provider = await db.get(IdentityProvider, dingtalk_id)
        provider.config = {
            **(provider.config or {}),
            "auto_repair_legacy_identity_split": True,
        }
        source_identity = Identity(
            username=f"dingtalk_{external_id}",
            email=f"dingtalk_{external_id}@dingtalk.local",
            phone=phone,
            password_hash=None,
        )
        target_identity = Identity(
            username=f"formal-{uuid.uuid4().hex[:8]}",
            email=email,
            phone=None,
            password_hash="existing-local-password",
        )
        db.add_all([source_identity, target_identity])
        await db.flush()
        source = User(
            identity_id=source_identity.id,
            tenant_id=tenant_id,
            display_name="Legacy DingTalk",
            role="member",
            source="dingtalk",
            registration_source="dingtalk_org_sync",
            is_active=True,
            quota_message_limit=50,
            quota_message_period="permanent",
            quota_messages_used=0,
            quota_max_agents=2,
            quota_agent_ttl_hours=0,
        )
        target = User(
            identity_id=target_identity.id,
            tenant_id=tenant_id,
            display_name="Formal User",
            role="member",
            source="web",
            registration_source="web",
            is_active=True,
        )
        db.add_all([source, target])
        await db.flush()
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            name="Formal User",
            email=email,
            phone=phone,
            user_id=source.id,
            status="active",
        )
        db.add(member)
        db.add(
            ChannelUserBinding(
                tenant_id=tenant_id,
                provider_id=dingtalk_id,
                installation_scope=scope,
                channel_type="dingtalk",
                id_type="staff_id",
                subject=external_id,
                user_id=source.id,
            )
        )
        await db.flush()
        source_id = source.id
        source_identity_id = source_identity.id
        target_id = target.id
        target_identity_id = target_identity.id

        result = await contact_provisioning.ensure_user_for_org_member(
            db,
            member,
            provider=provider,
            fresh_claims=VerifiedDirectoryClaims(
                tenant_id=tenant_id,
                provider_id=dingtalk_id,
                external_id=external_id,
                observed_at=datetime.now(timezone.utc),
                raw_email="",
                raw_org_email=email.upper(),
                raw_mobile=f"+{phone[:2]} {phone[2:]}",
                source="test",
            ),
        )
        await db.commit()

    assert result.legacy_split_repaired is True
    assert result.user.id == target_id
    async with async_session() as db:
        assert await db.get(User, source_id) is None
        assert await db.get(Identity, source_identity_id) is None
        target = await db.get(User, target_id)
        target_identity = await db.get(Identity, target_identity_id)
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == dingtalk_id,
                    OrgMember.external_id == external_id,
                )
            )
        ).scalar_one()
        binding = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.installation_scope == scope,
                    ChannelUserBinding.subject == external_id,
                )
            )
        ).scalar_one()

        assert target.identity_id == target_identity_id
        assert target_identity.email == email
        assert target_identity.phone == phone
        assert member.user_id == target_id
        assert binding.user_id == target_id


async def test_concurrent_legacy_split_repair_is_idempotent_and_deadlock_free():
    tenant_id, dingtalk_id, _oauth_id = await _tenant_and_providers()
    email = f"concurrent-legacy-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    external_id = f"staff-{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        provider = await db.get(IdentityProvider, dingtalk_id)
        provider.config = {"auto_repair_legacy_identity_split": True}
        source_identity = Identity(
            username=f"dingtalk_{external_id}",
            email=f"dingtalk_{external_id}@dingtalk.local",
            phone=phone,
            password_hash=None,
        )
        target_identity = Identity(
            username=f"formal-{uuid.uuid4().hex[:8]}",
            email=email,
        )
        db.add_all([source_identity, target_identity])
        await db.flush()
        source = User(
            identity_id=source_identity.id,
            tenant_id=tenant_id,
            display_name="Concurrent Legacy",
            role="member",
            source="dingtalk",
            registration_source="dingtalk_org_sync",
            is_active=True,
            quota_message_limit=50,
            quota_message_period="permanent",
            quota_messages_used=0,
            quota_max_agents=2,
            quota_agent_ttl_hours=0,
        )
        target = User(
            identity_id=target_identity.id,
            tenant_id=tenant_id,
            display_name="Concurrent Formal",
            role="member",
            source="web",
            registration_source="web",
            is_active=True,
        )
        db.add_all([source, target])
        await db.flush()
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            name="Concurrent Formal",
            email=email,
            phone=phone,
            user_id=source.id,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id
        target_id = target.id

    claims = VerifiedDirectoryClaims(
        tenant_id=tenant_id,
        provider_id=dingtalk_id,
        external_id=external_id,
        observed_at=datetime.now(timezone.utc),
        raw_org_email=email,
        raw_mobile=phone,
        source="concurrency-test",
    )

    async def repair_once() -> uuid.UUID:
        async with async_session() as db:
            provider = await db.get(IdentityProvider, dingtalk_id)
            member = await db.get(OrgMember, member_id)
            result = await contact_provisioning.ensure_user_for_org_member(
                db,
                member,
                provider=provider,
                fresh_claims=claims,
            )
            await db.commit()
            return result.user.id

    results = await asyncio.wait_for(
        asyncio.gather(repair_once(), repair_once(), repair_once()),
        timeout=10,
    )

    assert results == [target_id, target_id, target_id]
    async with async_session() as db:
        users = (
            await db.execute(select(User).where(User.tenant_id == tenant_id))
        ).scalars().all()
        identities = (
            await db.execute(
                select(Identity)
                .join(User, User.identity_id == Identity.id)
                .where(User.tenant_id == tenant_id)
            )
        ).scalars().all()
        assert [user.id for user in users] == [target_id]
        assert len(identities) == 1
        assert identities[0].email == email
        assert identities[0].phone == phone


async def test_email_match_trims_and_normalizes_historical_value():
    email = f"whitespace-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session() as db:
        identity = Identity(
            username=f"whitespace-{uuid.uuid4().hex[:8]}",
            email=f"  {email.upper()}  ",
        )
        db.add(identity)
        await db.commit()
        identity_id = identity.id

    async with async_session() as db:
        resolved = await registration_service.find_or_create_identity(
            db,
            email=email,
            phone=None,
            username="unused",
        )
        await db.commit()
        assert resolved.id == identity_id
        assert resolved.email == email


async def test_phone_match_normalizes_historical_format():
    phone = _phone()
    historical = f"+{phone[:2]} {phone[2:5]}-{phone[5:]}"
    async with async_session() as db:
        identity = Identity(
            username=f"historical-phone-{uuid.uuid4().hex[:8]}",
            phone=historical,
        )
        db.add(identity)
        await db.commit()
        identity_id = identity.id

    async with async_session() as db:
        resolved = await registration_service.find_or_create_identity(
            db,
            email=None,
            phone=phone,
            username="unused",
        )
        await db.commit()
        assert resolved.id == identity_id
        assert resolved.phone == phone


async def test_directory_then_oauth_attaches_identity_to_same_tenant_user(
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers(
        bind_oauth_directory=True
    )
    phone = _phone()
    email = f"directory-first-{uuid.uuid4().hex[:8]}@example.com"
    external_id = f"staff-{uuid.uuid4().hex[:8]}"

    async def fake_fetch(_provider, staff_id):
        assert staff_id == external_id
        return VerifiedDirectoryClaims(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            observed_at=datetime.now(timezone.utc),
            raw_org_email=email,
            raw_mobile=phone,
            source="test",
        )

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        fake_fetch,
    )

    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            unionid=f"union-{uuid.uuid4().hex[:8]}",
            name="Directory First",
            phone=f"+{phone[:2]} {phone[2:]}",
            email=email,
            status="active",
        )
        db.add(member)
        await db.flush()
        provider = await db.get(IdentityProvider, dingtalk_id)
        provisioned = await contact_provisioning.ensure_user_for_org_member(
            db,
            member,
            provider=provider,
            fresh_claims=await fake_fetch(provider, external_id),
        )
        assert provisioned.user.identity_id is not None
        original_identity_id = provisioned.user.identity_id
        original_user_id = provisioned.user.id
        await db.commit()

    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        user, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=external_id,
                name="Directory First",
                email=email.upper(),
                mobile=phone,
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        assert created is False
        assert user.id == original_user_id
        assert user.identity_id == original_identity_id


async def test_enterprise_oauth_never_uses_provider_subject_as_local_password():
    tenant_id, _dingtalk_id, oauth_id = await _tenant_and_providers()
    email = f"oauth-password-{uuid.uuid4().hex[:8]}@example.com"

    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        user, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=f"public-subject-{uuid.uuid4().hex}",
                name="OAuth Passwordless",
                email=email,
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        identity_id = user.identity_id

    assert created is True
    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        assert identity.password_hash is None


async def test_generic_oauth_subject_cannot_route_to_dingtalk_without_explicit_binding(
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers()
    external_id = f"collision-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            name="Unrelated Directory Principal",
            status="active",
        )
        db.add(member)
        await db.flush()
        user = User(
            identity_id=None,
            tenant_id=tenant_id,
            display_name=member.name,
            role="member",
            source="dingtalk",
            registration_source="dingtalk_org_sync",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        member.user_id = user.id
        await db.commit()

    async def unexpected_fetch(*_args):
        raise AssertionError("unbound generic OAuth must not call DingTalk")

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        unexpected_fetch,
    )
    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        routed = await provider._repair_legacy_dingtalk_oauth_user(
            db,
            tenant_id=tenant_id,
            user_info=ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=external_id,
                raw_data={},
            ),
        )
        assert routed is None


async def test_explicit_oauth_directory_route_for_formal_user_does_not_require_fresh_api(
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers(
        bind_oauth_directory=True
    )
    external_id = f"formal-{uuid.uuid4().hex[:8]}"
    email = f"{external_id}@example.com"
    async with async_session() as db:
        identity = Identity(username=external_id, email=email, email_verified=True)
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant_id,
            display_name="Formal Directory User",
            role="member",
            source="web",
            registration_source="web",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                provider_id=dingtalk_id,
                external_id=external_id,
                user_id=user.id,
                name=user.display_name,
                status="active",
            )
        )
        await db.commit()
        user_id = user.id

    async def unexpected_fetch(*_args):
        raise AssertionError("formal exact route must not require fresh DingTalk")

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        unexpected_fetch,
    )
    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        resolved, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=external_id,
                name="Formal Directory User",
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        assert created is False
        assert resolved.id == user_id


@pytest.mark.parametrize("route_type", ("oauth2", "dingtalk"))
async def test_exact_directory_oauth_route_rejects_disabled_user(
    route_type,
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers(
        bind_oauth_directory=True
    )
    external_id = f"disabled-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        identity = Identity(
            username=external_id,
            email=f"{external_id}@example.com",
            email_verified=True,
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant_id,
            display_name="Disabled Directory User",
            role="member",
            source="web",
            registration_source="web",
            is_active=False,
        )
        db.add(user)
        await db.flush()
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                provider_id=dingtalk_id,
                external_id=external_id,
                unionid=unionid,
                user_id=user.id,
                name=user.display_name,
                status="active",
            )
        )
        await db.commit()

    async def unexpected_fetch(*_args):
        raise AssertionError("formal disabled route must not call DingTalk")

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        unexpected_fetch,
    )
    async with async_session() as db:
        if route_type == "oauth2":
            provider_row = await db.get(IdentityProvider, oauth_id)
            provider = OAuth2AuthProvider(provider=provider_row)
            info = ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=external_id,
                raw_data={},
            )
        else:
            provider_row = await db.get(IdentityProvider, dingtalk_id)
            provider = DingTalkAuthProvider(provider=provider_row)
            info = ExternalUserInfo(
                provider_type="dingtalk",
                provider_user_id=unionid,
                provider_union_id=unionid,
                raw_data={"unionId": unionid},
            )
        with pytest.raises(HTTPException) as exc_info:
            await provider.find_or_create_user(
                db,
                info,
                tenant_id=str(tenant_id),
            )
        assert exc_info.value.status_code == 403
        await db.rollback()
