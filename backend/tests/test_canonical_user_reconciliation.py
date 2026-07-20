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


async def test_email_and_phone_are_equal_claims_and_split_identity_fails_closed():
    email = f"equal-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    async with async_session() as db:
        db.add_all(
            [
                Identity(username=f"email-{uuid.uuid4().hex[:8]}", email=email),
                Identity(username=f"phone-{uuid.uuid4().hex[:8]}", phone=phone),
            ]
        )
        await db.commit()

    async with async_session() as db:
        with pytest.raises(CanonicalIdentityConflict):
            await canonical_user_resolver.resolve_identity_claims(
                db, email=email.upper(), phone=f"+{phone[:2]} {phone[2:]}", enrich=True
            )


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
        assert provisioned.user.identity_id is None
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
        assert user.identity_id is not None


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


async def test_direct_dingtalk_oauth_repairs_legacy_split_via_scoped_unionid(
    monkeypatch,
):
    tenant_id, dingtalk_id, _oauth_id = await _tenant_and_providers(
        auto_repair=True
    )
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    phone = _phone()
    email = f"{external_id}@example.com"
    async with async_session() as db:
        target_identity = Identity(
            username=f"formal-{uuid.uuid4().hex[:8]}",
            email=email,
            email_verified=True,
        )
        source_identity = Identity(
            username=f"dingtalk_{external_id}",
            email=f"dingtalk_{external_id}@dingtalk.local",
            phone=phone,
            password_hash=None,
        )
        db.add_all([target_identity, source_identity])
        await db.flush()
        target = User(
            identity_id=target_identity.id,
            tenant_id=tenant_id,
            display_name="Formal User",
            role="member",
            source="web",
            registration_source="web",
            is_active=True,
        )
        source = User(
            identity_id=source_identity.id,
            tenant_id=tenant_id,
            display_name="Legacy User",
            role="member",
            source="dingtalk",
            registration_source="dingtalk_org_sync",
            is_active=True,
        )
        db.add_all([target, source])
        await db.flush()
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                provider_id=dingtalk_id,
                external_id=external_id,
                unionid=unionid,
                user_id=source.id,
                name=source.display_name,
                status="active",
            )
        )
        await db.commit()
        target_id = target.id
        source_id = source.id

    async def fresh(_provider, staff_id):
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
        fresh,
    )
    async with async_session() as db:
        dingtalk_row = await db.get(IdentityProvider, dingtalk_id)
        provider = DingTalkAuthProvider(provider=dingtalk_row)
        resolved, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="dingtalk",
                provider_user_id=unionid,
                provider_union_id=unionid,
                name="Formal User",
                raw_data={"unionId": unionid},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        assert created is False
        assert resolved.id == target_id
    async with async_session() as db:
        assert await db.get(User, source_id) is None


async def test_org_sync_channel_and_oauth_share_lock_order_without_deadlock(
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers(
        bind_oauth_directory=True,
        auto_repair=True,
    )
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    phone = _phone()
    email = f"{external_id}@example.com"
    async with async_session() as db:
        target_identity = Identity(
            username=f"formal-{uuid.uuid4().hex[:8]}",
            email=email,
            email_verified=True,
        )
        source_identity = Identity(
            username=f"dingtalk_{external_id}",
            email=f"dingtalk_{external_id}@dingtalk.local",
            phone=phone,
            password_hash=None,
        )
        db.add_all([target_identity, source_identity])
        await db.flush()
        target = User(
            identity_id=target_identity.id,
            tenant_id=tenant_id,
            display_name="Concurrent Formal",
            role="member",
            source="web",
            registration_source="web",
            is_active=True,
        )
        source = User(
            identity_id=source_identity.id,
            tenant_id=tenant_id,
            display_name="Concurrent Legacy",
            role="member",
            source="dingtalk",
            registration_source="dingtalk_org_sync",
            is_active=True,
        )
        db.add_all([target, source])
        await db.flush()
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            unionid=unionid,
            user_id=source.id,
            name=source.display_name,
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        target_id = target.id
        source_id = source.id
        member_id = member.id

    def claims() -> VerifiedDirectoryClaims:
        return VerifiedDirectoryClaims(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            observed_at=datetime.now(timezone.utc),
            raw_org_email=email,
            raw_mobile=phone,
            source="test",
        )

    async def fresh(_provider, staff_id):
        assert staff_id == external_id
        await asyncio.sleep(0)
        return claims()

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        fresh,
    )

    async def run_sync():
        async with async_session() as db:
            provider = await db.get(IdentityProvider, dingtalk_id)
            adapter = DingTalkOrgSyncAdapter(
                provider=provider,
                tenant_id=tenant_id,
            )
            await adapter._upsert_member(
                db,
                provider,
                ExternalUser(
                    external_id=external_id,
                    unionid=unionid,
                    name="Concurrent Formal",
                    email=email,
                    mobile=phone,
                    status="active",
                    raw_data={
                        "userid": external_id,
                        "unionid": unionid,
                        "email": "",
                        "org_email": email,
                        "mobile": phone,
                    },
                ),
                "",
            )
            await db.commit()

    async def run_channel():
        async with async_session() as db:
            service = ChannelUserService()
            agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
            resolved = await service.resolve_channel_user(
                db,
                agent,
                "dingtalk",
                external_id,
                {
                    "external_id": external_id,
                    "unionid": unionid,
                    "name": "Concurrent Formal",
                    "identity_verified": True,
                    "directory_name_verified": True,
                    "raw_email": "",
                    "raw_org_email": email,
                    "raw_mobile": phone,
                    "_installation_scope": f"test:{uuid.uuid4()}",
                },
            )
            await db.commit()
            assert resolved.id == target_id

    async def run_oauth():
        async with async_session() as db:
            provider_row = await db.get(IdentityProvider, oauth_id)
            provider = OAuth2AuthProvider(provider=provider_row)
            resolved, _ = await provider.find_or_create_user(
                db,
                ExternalUserInfo(
                    provider_type="oauth2",
                    provider_user_id=external_id,
                    name="Concurrent Formal",
                    raw_data={},
                ),
                tenant_id=str(tenant_id),
            )
            await db.commit()
            assert resolved.id == target_id

    await asyncio.wait_for(
        asyncio.gather(run_sync(), run_channel(), run_oauth()),
        timeout=10,
    )
    async with async_session() as db:
        assert await db.get(User, source_id) is None
        member = await db.get(OrgMember, member_id)
        assert member.user_id == target_id


async def test_feishu_login_never_uses_open_id_as_local_password():
    async with async_session() as db:
        tenant = Tenant(name="Feishu Passwordless", slug=f"fs-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.flush()
        user, _token = await feishu_service.login_or_register(
            db,
            {
                "open_id": f"ou_{uuid.uuid4().hex}",
                "user_id": f"fs_{uuid.uuid4().hex}",
                "union_id": f"on_{uuid.uuid4().hex}",
                "name": "Feishu Passwordless",
                "email": f"feishu-{uuid.uuid4().hex[:8]}@example.com",
            },
            tenant_id=tenant.id,
        )
        await db.commit()
        identity_id = user.identity_id
    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        assert identity.password_hash is None


async def test_enterprise_oauth_preserves_existing_local_password():
    tenant_id, _dingtalk_id, oauth_id = await _tenant_and_providers()
    email = f"oauth-existing-{uuid.uuid4().hex[:8]}@example.com"
    password_hash = "existing-password-hash"

    async with async_session() as db:
        identity = Identity(
            username=f"existing-{uuid.uuid4().hex[:8]}",
            email=email,
            password_hash=password_hash,
        )
        db.add(identity)
        await db.flush()
        db.add(
            User(
                identity_id=identity.id,
                tenant_id=tenant_id,
                display_name="Existing Local User",
                role="member",
                source="web",
                registration_source="web",
                is_active=True,
            )
        )
        await db.commit()
        identity_id = identity.id

    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=f"public-subject-{uuid.uuid4().hex}",
                name="Existing Local User",
                email=email,
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()

    async with async_session() as db:
        identity = await db.get(Identity, identity_id)
        assert identity.password_hash == password_hash


async def test_legacy_dingtalk_oauth_fails_closed_without_fresh_directory_claims(
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers(
        bind_oauth_directory=True,
        auto_repair=True,
    )
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    phone = _phone()

    async def unavailable(_provider, _external_id):
        return None

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        unavailable,
    )

    async with async_session() as db:
        identity = Identity(
            username=f"dingtalk_{external_id}",
            email=f"dingtalk_{external_id}@dingtalk.local",
            phone=phone,
            password_hash=None,
        )
        db.add(identity)
        await db.flush()
        source = User(
            identity_id=identity.id,
            tenant_id=tenant_id,
            display_name="Legacy OAuth",
            role="member",
            source="dingtalk",
            registration_source="dingtalk_org_sync",
            is_active=True,
        )
        db.add(source)
        await db.flush()
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                provider_id=dingtalk_id,
                external_id=external_id,
                name="Legacy OAuth",
                phone=phone,
                user_id=source.id,
                status="active",
            )
        )
        await db.commit()
        source_id = source.id
        identity_id = identity.id

    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        with pytest.raises(HTTPException) as exc_info:
            await provider.find_or_create_user(
                db,
                ExternalUserInfo(
                    provider_type="oauth2",
                    provider_user_id=external_id,
                    name="Legacy OAuth",
                    mobile=phone,
                    raw_data={},
                ),
                tenant_id=str(tenant_id),
            )
        await db.rollback()

    assert exc_info.value.status_code == 409
    async with async_session() as db:
        source = await db.get(User, source_id)
        identity = await db.get(Identity, identity_id)
        assert source.identity_id == identity_id
        assert identity.username == f"dingtalk_{external_id}"
        count = await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )
        assert count == 1


async def test_enterprise_oauth_keeps_exact_user_when_contact_is_temporarily_missing():
    tenant_id, _dingtalk_id, oauth_id = await _tenant_and_providers()
    phone = _phone()
    email = f"oauth-exact-{uuid.uuid4().hex[:8]}@example.com"
    provider_user_id = f"oauth-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        first, _ = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=provider_user_id,
                name="Exact OAuth",
                email=email,
                mobile=phone,
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        first_id = first.id

    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        repeated, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=provider_user_id,
                name="Exact OAuth",
                email=None,
                mobile=None,
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        assert created is False
        assert repeated.id == first_id
        count = await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )
        assert count == 1


async def test_im_without_contact_oauth_then_verified_im_converges_physical_user():
    tenant_id, _dingtalk_id, oauth_id = await _tenant_and_providers()
    phone = _phone()
    email = f"im-first-{uuid.uuid4().hex[:8]}@example.com"
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    scope = f"test:dingtalk:{uuid.uuid4()}"
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    service = ChannelUserService()

    async with async_session() as db:
        provisional = await service.resolve_channel_user(
            db,
            agent,
            "dingtalk",
            external_id,
            {"unionid": unionid, "_installation_scope": scope},
        )
        await db.commit()
        provisional_id = provisional.id
        assert provisional.identity_id is None

    async with async_session() as db:
        oauth_row = await db.get(IdentityProvider, oauth_id)
        provider = OAuth2AuthProvider(provider=oauth_row)
        canonical, created = await provider.find_or_create_user(
            db,
            ExternalUserInfo(
                provider_type="oauth2",
                provider_user_id=f"oauth-{uuid.uuid4().hex[:8]}",
                name="IM First",
                email=email,
                mobile=phone,
                raw_data={},
            ),
            tenant_id=str(tenant_id),
        )
        await db.commit()
        canonical_id = canonical.id
        assert created is True
        assert canonical_id != provisional_id

    async with async_session() as db:
        resolved = await service.resolve_channel_user(
            db,
            agent,
            "dingtalk",
            external_id,
            {
                "unionid": unionid,
                "name": "IM First",
                "email": email,
                "mobile": phone,
                "identity_verified": True,
                "directory_name_verified": True,
                "_installation_scope": scope,
            },
        )
        await db.commit()
        assert resolved.id == canonical_id
        assert await db.get(User, provisional_id) is None
        count = await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )
        assert count == 1
        binding_user_ids = set(
            (
                await db.execute(
                    select(ChannelUserBinding.user_id).where(
                        ChannelUserBinding.installation_scope == scope
                    )
                )
            ).scalars().all()
        )
        assert binding_user_ids == {canonical_id}
        member_user_ids = set(
            (
                await db.execute(
                    select(OrgMember.user_id).where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.provider_id.is_not(None),
                    )
                )
            ).scalars().all()
        )
        assert member_user_ids == {canonical_id}
        participant_count = await db.scalar(
            select(func.count(Participant.id)).where(
                Participant.type == "user", Participant.ref_id == canonical_id
            )
        )
        assert participant_count == 1


async def test_concurrent_identity_and_tenant_user_creation_converges():
    tenant_id, _dingtalk_id, _oauth_id = await _tenant_and_providers()
    phone = _phone()
    email = f"concurrent-{uuid.uuid4().hex[:8]}@example.com"

    async def create_once(index: int) -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session() as db:
            identity = await registration_service.find_or_create_identity(
                db,
                email=email.upper() if index % 2 else email,
                phone=f"+{phone[:2]} {phone[2:]}" if index % 3 else phone,
                username="concurrent-user",
            )
            user, _created = await canonical_user_resolver.get_or_create_tenant_user(
                db,
                tenant_id=tenant_id,
                identity=identity,
                display_name="Concurrent User",
                avatar_url=None,
                registration_source="oauth2",
            )
            await db.commit()
            return identity.id, user.id

    results = await asyncio.gather(*(create_once(i) for i in range(50)))
    assert len({identity_id for identity_id, _ in results}) == 1
    assert len({user_id for _, user_id in results}) == 1

    async with async_session() as db:
        identity_count = await db.scalar(
            select(func.count(Identity.id)).where(
                func.lower(func.btrim(Identity.email)) == email,
                Identity.phone == phone,
            )
        )
        user_count = await db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        )
        assert identity_count == 1
        assert user_count == 1


async def test_dingtalk_identity_conflict_still_routes_message_to_identityless_user():
    tenant_id, _dingtalk_id, _oauth_id = await _tenant_and_providers()
    email = f"conflict-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()
    async with async_session() as db:
        email_identity = Identity(
            username=f"email-{uuid.uuid4().hex[:8]}", email=email
        )
        phone_identity = Identity(
            username=f"phone-{uuid.uuid4().hex[:8]}", phone=phone
        )
        db.add_all([email_identity, phone_identity])
        await db.flush()
        db.add_all(
            [
                User(
                    identity_id=email_identity.id,
                    tenant_id=tenant_id,
                    display_name="Email Owner",
                    role="member",
                    is_active=True,
                ),
                User(
                    identity_id=phone_identity.id,
                    tenant_id=tenant_id,
                    display_name="Phone Owner",
                    role="member",
                    is_active=True,
                ),
            ]
        )
        await db.commit()

    service = ChannelUserService()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    async with async_session() as db:
        routed = await service.resolve_channel_user(
            db,
            agent,
            "dingtalk",
            f"staff-{uuid.uuid4().hex[:8]}",
            {
                "unionid": f"union-{uuid.uuid4().hex[:8]}",
                "email": email,
                "mobile": phone,
                "identity_verified": True,
                "_installation_scope": f"test:dingtalk:{uuid.uuid4()}",
            },
        )
        await db.commit()
        assert routed.identity_id is None


async def test_dingtalk_bound_user_keeps_routing_when_new_contact_conflicts():
    tenant_id, _dingtalk_id, _oauth_id = await _tenant_and_providers()
    service = ChannelUserService()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    scope = f"test:dingtalk:{uuid.uuid4()}"

    async with async_session() as db:
        first = await service.resolve_channel_user(
            db,
            agent,
            "dingtalk",
            external_id,
            {"unionid": unionid, "_installation_scope": scope},
        )
        await db.commit()
        first_id = first.id

    conflicting_phone = _phone()
    async with async_session() as db:
        bound_identity = Identity(
            username=f"bound-{uuid.uuid4().hex[:8]}",
            email=f"bound-{uuid.uuid4().hex[:8]}@example.com",
        )
        identity = Identity(
            username=f"owner-{uuid.uuid4().hex[:8]}", phone=conflicting_phone
        )
        db.add_all([bound_identity, identity])
        await db.flush()
        bound_user = await db.get(User, first_id)
        bound_user.identity_id = bound_identity.id
        db.add(
            User(
                identity_id=identity.id,
                tenant_id=tenant_id,
                display_name="Other Owner",
                role="member",
                is_active=True,
            )
        )
        await db.commit()

    async with async_session() as db:
        routed = await service.resolve_channel_user(
            db,
            agent,
            "dingtalk",
            external_id,
            {
                "unionid": unionid,
                "mobile": conflicting_phone,
                "identity_verified": True,
                "_installation_scope": scope,
            },
        )
        await db.commit()
        assert routed.id == first_id


async def test_dingtalk_ambiguous_directory_rows_still_route_exact_sender():
    tenant_id, dingtalk_id, _oauth_id = await _tenant_and_providers()
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        db.add_all(
            [
                OrgMember(
                    tenant_id=tenant_id,
                    provider_id=dingtalk_id,
                    external_id=external_id,
                    name="Duplicate A",
                    status="active",
                ),
                OrgMember(
                    tenant_id=tenant_id,
                    provider_id=dingtalk_id,
                    external_id=external_id,
                    name="Duplicate B",
                    status="active",
                ),
            ]
        )
        await db.commit()

    service = ChannelUserService()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    scope = f"test:dingtalk:{uuid.uuid4()}"
    async with async_session() as db:
        routed = await service.resolve_channel_user(
            db,
            agent,
            "dingtalk",
            external_id,
            {"unionid": unionid, "_installation_scope": scope},
        )
        await db.commit()
        assert routed.identity_id is None
        binding = (
            await db.execute(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.installation_scope == scope,
                    ChannelUserBinding.id_type == "staff_id",
                    ChannelUserBinding.subject == external_id,
                )
            )
        ).scalar_one()
        assert binding.user_id == routed.id


async def test_identityless_merge_preserves_permissions_and_daily_report_content():
    tenant_id, _dingtalk_id, _oauth_id = await _tenant_and_providers()
    async with async_session() as db:
        identity = Identity(
            username=f"canonical-{uuid.uuid4().hex[:8]}",
            email=f"canonical-{uuid.uuid4().hex[:8]}@example.com",
        )
        db.add(identity)
        await db.flush()
        target = User(
            identity_id=identity.id,
            tenant_id=tenant_id,
            display_name="Canonical",
            role="member",
            is_active=True,
        )
        source = User(
            identity_id=None,
            tenant_id=tenant_id,
            display_name="Provisional",
            role="member",
            is_active=True,
        )
        db.add_all([target, source])
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=target.id,
            name=f"Agent {uuid.uuid4().hex[:8]}",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        db.add_all(
            [
                AgentPermission(
                    agent_id=agent.id,
                    scope_type="user",
                    scope_id=target.id,
                    access_level="use",
                ),
                AgentPermission(
                    agent_id=agent.id,
                    scope_type="user",
                    scope_id=source.id,
                    access_level="manage",
                ),
                MemberDailyReport(
                    tenant_id=tenant_id,
                    member_type="user",
                    member_id=target.id,
                    user_id=target.id,
                    report_date=date(2026, 7, 17),
                    content="canonical report",
                ),
                MemberDailyReport(
                    tenant_id=tenant_id,
                    member_type="user",
                    member_id=source.id,
                    user_id=source.id,
                    report_date=date(2026, 7, 17),
                    content="provisional report",
                ),
            ]
        )
        await db.commit()
        source_id = source.id
        target_id = target.id

    async with async_session() as db:
        source = await db.get(User, source_id)
        target = await db.get(User, target_id)
        merged = await canonical_user_resolver.merge_identityless_user(
            db, source=source, target=target
        )
        await db.commit()
        assert merged.id == target_id
        assert await db.get(User, source_id) is None
        permissions = (
            await db.execute(
                select(AgentPermission).where(
                    AgentPermission.scope_type == "user",
                    AgentPermission.scope_id == target_id,
                )
            )
        ).scalars().all()
        assert len(permissions) == 1
        assert permissions[0].access_level == "manage"
        reports = (
            await db.execute(
                select(MemberDailyReport).where(
                    MemberDailyReport.tenant_id == tenant_id,
                    MemberDailyReport.user_id == target_id,
                    MemberDailyReport.report_date == date(2026, 7, 17),
                )
            )
        ).scalars().all()
        assert len(reports) == 1
        assert "canonical report" in reports[0].content
        assert "provisional report" in reports[0].content


async def test_concurrent_attach_and_create_converges_to_one_tenant_user():
    tenant_id, _dingtalk_id, _oauth_id = await _tenant_and_providers()
    async with async_session() as db:
        identity = Identity(
            username=f"attach-{uuid.uuid4().hex[:8]}",
            email=f"attach-{uuid.uuid4().hex[:8]}@example.com",
        )
        provisional = User(
            tenant_id=tenant_id,
            display_name="Attach Candidate",
            role="member",
            is_active=True,
        )
        db.add_all([identity, provisional])
        await db.commit()
        identity_id = identity.id
        provisional_id = provisional.id

    async def attach_once():
        async with async_session() as db:
            identity = await db.get(Identity, identity_id)
            candidate = await db.get(User, provisional_id)
            user = await canonical_user_resolver.reconcile_identity_user(
                db,
                tenant_id=tenant_id,
                identity=identity,
                candidate_user=candidate,
            )
            await db.commit()
            return user.id

    async def create_once():
        async with async_session() as db:
            identity = await db.get(Identity, identity_id)
            user, _ = await canonical_user_resolver.get_or_create_tenant_user(
                db,
                tenant_id=tenant_id,
                identity=identity,
                display_name="Concurrent Winner",
                avatar_url=None,
                registration_source="oauth2",
            )
            await db.commit()
            return user.id

    results = await asyncio.gather(
        *(attach_once() if index % 2 else create_once() for index in range(30))
    )
    assert len(set(results)) == 1
    async with async_session() as db:
        rows = (
            await db.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.identity_id == identity_id,
                )
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_concurrent_org_member_provisioning_creates_one_identityless_user():
    tenant_id, dingtalk_id, _oauth_id = await _tenant_and_providers()
    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=f"staff-{uuid.uuid4().hex[:8]}",
            name="Concurrent Directory User",
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    async def provision_once() -> uuid.UUID:
        async with async_session() as db:
            member = await db.get(OrgMember, member_id)
            result = await contact_provisioning.ensure_user_for_org_member(db, member)
            await db.commit()
            assert result.user is not None
            return result.user.id

    user_ids = await asyncio.gather(*(provision_once() for _ in range(30)))
    assert len(set(user_ids)) == 1
    async with async_session() as db:
        users = (
            await db.execute(select(User).where(User.tenant_id == tenant_id))
        ).scalars().all()
        assert len(users) == 1
        participants = await db.scalar(
            select(func.count(Participant.id)).where(
                Participant.type == "user",
                Participant.ref_id == users[0].id,
            )
        )
        assert participants == 1


@pytest.mark.parametrize(
    "order",
    list(itertools.permutations(("directory", "oauth", "im"))),
)
@pytest.mark.parametrize("shared_claim", ("email", "phone"))
async def test_all_entry_orders_converge_on_either_shared_claim(
    order,
    shared_claim,
    monkeypatch,
):
    tenant_id, dingtalk_id, oauth_id = await _tenant_and_providers(
        bind_oauth_directory=True,
        auto_repair=True,
    )
    phone = _phone()
    email = f"order-{uuid.uuid4().hex[:8]}@example.com"
    external_id = f"staff-{uuid.uuid4().hex[:8]}"
    unionid = f"union-{uuid.uuid4().hex[:8]}"
    scope = f"test:dingtalk:{uuid.uuid4()}"
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    service = ChannelUserService()

    def fresh_claims() -> VerifiedDirectoryClaims:
        return VerifiedDirectoryClaims(
            tenant_id=tenant_id,
            provider_id=dingtalk_id,
            external_id=external_id,
            observed_at=datetime.now(timezone.utc),
            raw_org_email=email if shared_claim == "email" else None,
            raw_mobile=phone if shared_claim == "phone" else None,
            source="test",
        )

    async def fake_fetch(_provider, staff_id):
        assert staff_id == external_id
        return fresh_claims()

    monkeypatch.setattr(
        "app.services.dingtalk_identity_reconciliation.fetch_fresh_dingtalk_claims",
        fake_fetch,
    )

    async def run_directory() -> None:
        async with async_session() as db:
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.provider_id == dingtalk_id,
                        OrgMember.external_id == external_id,
                    )
                )
            ).scalar_one_or_none()
            if member is None:
                member = OrgMember(
                    tenant_id=tenant_id,
                    provider_id=dingtalk_id,
                    external_id=external_id,
                    unionid=unionid,
                    name="Order User",
                    status="active",
                )
                db.add(member)
            member.email = email if shared_claim == "email" else None
            member.phone = phone if shared_claim == "phone" else None
            await db.flush()
            provider = await db.get(IdentityProvider, dingtalk_id)
            await contact_provisioning.ensure_user_for_org_member(
                db,
                member,
                provider=provider,
                fresh_claims=fresh_claims(),
            )
            await db.commit()

    async def run_oauth() -> None:
        async with async_session() as db:
            provider_row = await db.get(IdentityProvider, oauth_id)
            provider = OAuth2AuthProvider(provider=provider_row)
            await provider.find_or_create_user(
                db,
                ExternalUserInfo(
                    provider_type="oauth2",
                    provider_user_id=external_id,
                    name="Order User",
                    email=email,
                    mobile=phone,
                    raw_data={},
                ),
                tenant_id=str(tenant_id),
            )
            await db.commit()

    async def run_im() -> None:
        async with async_session() as db:
            info = {
                "unionid": unionid,
                "name": "Order User",
                "identity_verified": True,
                "directory_name_verified": True,
                "_installation_scope": scope,
            }
            info["email" if shared_claim == "email" else "mobile"] = (
                email if shared_claim == "email" else phone
            )
            await service.resolve_channel_user(
                db, agent, "dingtalk", external_id, info
            )
            await db.commit()

    actions = {
        "directory": run_directory,
        "oauth": run_oauth,
        "im": run_im,
    }
    for action in order:
        await actions[action]()

    async with async_session() as db:
        users = (
            await db.execute(select(User).where(User.tenant_id == tenant_id))
        ).scalars().all()
        assert len(users) == 1
        assert users[0].identity_id is not None
        binding_user_ids = set(
            (
                await db.execute(
                    select(ChannelUserBinding.user_id).where(
                        ChannelUserBinding.installation_scope == scope
                    )
                )
            ).scalars().all()
        )
        assert binding_user_ids == {users[0].id}
        member_user_ids = set(
            (
                await db.execute(
                    select(OrgMember.user_id).where(
                        OrgMember.tenant_id == tenant_id,
                        OrgMember.provider_id == dingtalk_id,
                    )
                )
            ).scalars().all()
        )
        assert member_user_ids == {users[0].id}
