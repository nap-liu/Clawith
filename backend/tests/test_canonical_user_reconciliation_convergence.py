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
