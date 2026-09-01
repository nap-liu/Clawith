"""Mechanical continuation of canonical-user reconciliation tests."""

import pytest

from tests.test_canonical_user_reconciliation import (
    Agent,
    AgentPermission,
    CanonicalIdentityConflict,
    ChannelUserBinding,
    ChannelUserService,
    DingTalkAuthProvider,
    DingTalkOrgSyncAdapter,
    ExternalUser,
    ExternalUserInfo,
    HTTPException,
    Identity,
    IdentityProvider,
    MemberDailyReport,
    OAuth2AuthProvider,
    OrgMember,
    Participant,
    SimpleNamespace,
    Tenant,
    User,
    VerifiedDirectoryClaims,
    _isolate_async_engine_between_tests,
    _phone,
    _tenant_and_providers,
    asyncio,
    async_session,
    canonical_user_resolver,
    contact_provisioning,
    date,
    datetime,
    dingtalk_legacy_identity_reconciler,
    feishu_service,
    func,
    itertools,
    registration_service,
    select,
    timezone,
    uuid,
)

pytestmark = pytest.mark.asyncio

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
