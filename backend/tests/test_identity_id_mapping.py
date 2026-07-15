import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.services.channel_user_service import ChannelUserService
from app.services.channel_user_service import get_platform_user_by_org_member
from app.database import async_session, engine
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.sso_service import sso_service


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _phone() -> str:
    return f"8613{uuid.uuid4().int % 10**9:09d}"


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        tenant = Tenant(name="Channel User Tenant", slug=f"channel-user-{uuid.uuid4().hex[:10]}")
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


def test_sso_identity_lookup_chain_prioritizes_unionid_then_userid_then_openid():
    lookup_chain = sso_service._identity_lookup_chain(
        "feishu",
        "ou_open_123",
        {
            "raw_data": {
                "open_id": "ou_open_123",
                "union_id": "on_union_456",
                "user_id": "u_emp_789",
            }
        },
    )

    assert lookup_chain == [
        ("unionid", "on_union_456"),
        ("external_id", "u_emp_789"),
        ("open_id", "ou_open_123"),
    ]


def test_sso_extract_identity_ids_uses_real_union_id_not_open_id():
    union_id, open_id, external_id = sso_service._extract_identity_ids(
        "feishu",
        "ou_open_123",
        {
            "raw_data": {
                "open_id": "ou_open_123",
                "union_id": "on_union_456",
                "user_id": "u_emp_789",
            }
        },
    )

    assert union_id == "on_union_456"
    assert open_id == "ou_open_123"
    assert external_id == "u_emp_789"


def test_sso_extract_identity_ids_handles_registration_wrapped_payload():
    union_id, open_id, external_id = sso_service._extract_identity_ids(
        "dingtalk",
        "open_123",
        {
            "name": "Alice",
            "raw_data": {
                "openId": "open_123",
                "unionId": "union_456",
            },
        },
    )

    assert union_id == "union_456"
    assert open_id == "open_123"
    assert external_id is None


def test_channel_user_service_keeps_feishu_user_id_out_of_unionid():
    service = ChannelUserService()

    union_id, open_id, external_id = service._get_channel_ids(
        "feishu",
        "ou_open_123",
        {
            "external_id": "u_emp_789",
            "unionid": "on_union_456",
            "open_id": "ou_open_123",
        },
    )

    assert union_id == "on_union_456"
    assert open_id == "ou_open_123"
    assert external_id == "u_emp_789"


def test_channel_user_service_maps_generic_channels_to_dedicated_provider():
    service = ChannelUserService()

    assert service._normalize_channel_type("wechat") == "wechat"
    assert service._normalize_channel_type("slack") == "slack"
    assert service._normalize_channel_type("teams") == "teams"
    assert service._normalize_channel_type("microsoft_teams") == "teams"
    assert service._normalize_channel_type("feishu") == "feishu"


def test_channel_user_service_keeps_generic_channel_external_ids_unscoped():
    service = ChannelUserService()

    assert service._get_channel_ids("wechat", "wx_user_123", {}) == (None, None, "wx_user_123")
    assert service._get_channel_ids("slack", "U123456", {}) == (None, None, "U123456")
    assert service._get_channel_ids("teams", "29:abc", {}) == (None, None, "29:abc")


@pytest.mark.asyncio
async def test_channel_user_service_uses_feishu_open_id_for_existing_member_lookup():
    service = ChannelUserService()
    db = AsyncMock()
    expected_member = SimpleNamespace(id="member-1")
    result = Mock()
    result.scalars.return_value.all.return_value = [expected_member]
    db.execute = AsyncMock(return_value=result)

    member = await service._find_org_member(
        db,
        provider_id="provider-1",
        channel_type="feishu",
        external_user_id=None,
        extra_info={"open_id": "ou_open_123"},
    )

    assert member is expected_member
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_user_service_accepts_scoped_feishu_open_id_binding():
    service = ChannelUserService()
    db = AsyncMock()
    db.get.return_value = None
    agent = SimpleNamespace(id="agent-1", tenant_id="tenant-1")

    provider = SimpleNamespace(
        id="provider-1", tenant_id="tenant-1", config={}, provider_type="feishu"
    )
    created_user = SimpleNamespace(id="user-1")
    service._ensure_provider = AsyncMock(return_value=provider)
    service._find_bound_user = AsyncMock(return_value=None)
    service._find_org_member = AsyncMock(return_value=None)
    service._create_channel_user = AsyncMock(return_value=created_user)
    service._ensure_bindings = AsyncMock(return_value=(created_user, True))
    service._create_org_member_shell = AsyncMock()

    user = await service.resolve_channel_user(
        db=db,
        agent=agent,
        channel_type="feishu",
        external_user_id=None,
        extra_info={"open_id": "ou_open_123", "_installation_scope": "test:feishu"},
    )

    assert user is created_user
    service._ensure_bindings.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_user_service_skips_dingtalk_lookup_when_ids_missing():
    service = ChannelUserService()
    db = AsyncMock()

    member = await service._find_org_member(
        db,
        provider_id="provider-1",
        channel_type="dingtalk",
        external_user_id=None,
        extra_info={},
    )

    assert member is None
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_user_service_uses_wechat_external_id_for_existing_member_lookup():
    service = ChannelUserService()
    db = AsyncMock()
    expected_member = SimpleNamespace(
        id="member-wechat-1",
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    member_result = Mock()
    member_result.scalars.return_value.all.return_value = [expected_member]
    installation_result = Mock()
    installation_result.scalar_one.return_value = 1
    db.execute = AsyncMock(side_effect=[member_result, installation_result])

    member = await service._find_org_member(
        db,
        provider_id="provider-1",
        channel_type="wechat",
        external_user_id="wx_user_123",
        extra_info={"external_id": "wx_user_123"},
    )

    assert member is expected_member
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_channel_user_service_creates_wechat_org_member_shell_for_lazy_registration():
    service = ChannelUserService()
    db = AsyncMock()
    db.get.return_value = None
    agent = SimpleNamespace(id="agent-1", tenant_id="tenant-1")
    provider = SimpleNamespace(
        id="provider-1", tenant_id="tenant-1", config={}, provider_type="wechat"
    )
    created_user = SimpleNamespace(id="user-1")

    service._ensure_provider = AsyncMock(return_value=provider)
    service._find_bound_user = AsyncMock(return_value=None)
    service._find_org_member = AsyncMock(return_value=None)
    service._create_channel_user = AsyncMock(return_value=created_user)
    service._ensure_bindings = AsyncMock(return_value=(created_user, True))
    service._create_org_member_shell = AsyncMock()

    user = await service.resolve_channel_user(
        db=db,
        agent=agent,
        channel_type="wechat",
        external_user_id="wx_user_123",
        extra_info={"external_id": "wx_user_123", "_installation_scope": "test:wechat"},
    )

    assert user is created_user
    service._create_org_member_shell.assert_awaited_once_with(
        db,
        provider,
        "wechat",
        "wx_user_123",
        {"external_id": "wx_user_123", "_installation_scope": "test:wechat"},
        linked_user_id="user-1",
    )


@pytest.mark.asyncio
async def test_get_platform_user_by_org_member_uses_contact_provisioning():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    phone = _phone()

    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=f"dt_{uuid.uuid4().hex[:8]}",
            unionid=f"union_{uuid.uuid4().hex[:8]}",
            name="主动联系对象",
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    async with async_session() as db:
        member = await db.get(OrgMember, member_id)
        user = await get_platform_user_by_org_member(db, member, agent_tenant_id=tenant.id)
        await db.commit()

        assert user.tenant_id == tenant.id
        assert user.registration_source == "dingtalk_org_sync"
        assert member.user_id == user.id

        participant_count = (
            await db.execute(
                select(func.count())
                .select_from(Participant)
                .where(Participant.type == "user", Participant.ref_id == user.id)
            )
        ).scalar_one()
        assert participant_count == 1


@pytest.mark.asyncio
async def test_channel_user_service_uses_org_member_provisioning_before_lazy_registration():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    phone = _phone()
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=external_id,
            unionid=unionid,
            name="钉钉未关联成员",
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    service = ChannelUserService()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant.id)

    async with async_session() as db:
        user = await service.resolve_channel_user(
            db=db,
            agent=agent,
            channel_type="dingtalk",
            external_user_id=external_id,
            extra_info={
                "unionid": unionid,
                "name": "钉钉未关联成员",
                "_installation_scope": "test:dingtalk",
            },
        )
        await db.commit()

        member = await db.get(OrgMember, member_id)
        assert member.user_id == user.id
        assert user.registration_source == "dingtalk_org_sync"
        assert user.identity_id is None


@pytest.mark.asyncio
async def test_channel_user_service_unverified_contact_payload_does_not_merge_email():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    phone = _phone()
    shared_email = f"shared-{uuid.uuid4().hex[:8]}@example.com"

    async with async_session() as db:
        identity = Identity(
            username=f"wrong_{uuid.uuid4().hex[:8]}",
            email=shared_email,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        email_identity_user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Wrong Email Match",
            role="member",
            is_active=True,
        )
        db.add(email_identity_user)
        await db.commit()
        wrong_user_id = email_identity_user.id

    service = ChannelUserService()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant.id)
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        user = await service.resolve_channel_user(
            db=db,
            agent=agent,
            channel_type="dingtalk",
            external_user_id=external_id,
            extra_info={
                "unionid": unionid,
                "name": "手机号优先消息人",
                "mobile": phone,
                "email": shared_email,
                "_installation_scope": "test:dingtalk",
            },
        )
        await db.commit()

        assert user.id != wrong_user_id
        assert user.identity_id is None
        assert user.registration_source == "dingtalk_channel"
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.provider_id == provider.id,
                    OrgMember.external_id == external_id,
                )
            )
        ).scalar_one()
        assert member.user_id == user.id


@pytest.mark.asyncio
async def test_channel_user_service_dingtalk_without_mobile_does_not_bind_by_email():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id)
    shared_email = f"dingtalk-no-phone-{uuid.uuid4().hex[:8]}@example.com"
    external_id = f"dt_{uuid.uuid4().hex[:8]}"
    unionid = f"union_{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        identity = Identity(
            username=f"email_only_{uuid.uuid4().hex[:8]}",
            email=shared_email,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        email_user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Email Only User",
            role="member",
            is_active=True,
        )
        db.add(email_user)
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=external_id,
            unionid=unionid,
            name="钉钉无手机号",
            email=shared_email,
            phone=None,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    service = ChannelUserService()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant.id)

    async with async_session() as db:
        user = await service.resolve_channel_user(
            db=db,
            agent=agent,
            channel_type="dingtalk",
            external_user_id=external_id,
            extra_info={
                "unionid": unionid,
                "email": shared_email,
                "_installation_scope": "test:dingtalk",
            },
        )
        await db.commit()
        member = await db.get(OrgMember, member_id)
        assert member.user_id == user.id
        assert user.id != email_user.id
        assert user.identity_id is None


@pytest.mark.asyncio
async def test_get_platform_user_by_org_member_does_not_merge_unverified_email():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id, provider_type="wechat")
    email = f"wechat-{uuid.uuid4().hex[:8]}@example.com"

    async with async_session() as db:
        identity = Identity(
            username=f"wechat_email_{uuid.uuid4().hex[:8]}",
            email=email,
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        existing_user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Wechat Existing",
            role="member",
            is_active=True,
        )
        db.add(existing_user)
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=f"wx_{uuid.uuid4().hex[:8]}",
            name="微信联系人",
            email=email,
            phone=None,
            status="active",
        )
        db.add(member)
        await db.commit()
        user_id = existing_user.id
        member_id = member.id

    async with async_session() as db:
        member = await db.get(OrgMember, member_id)
        user = await get_platform_user_by_org_member(db, member, agent_tenant_id=tenant.id)
        await db.commit()

        assert user.id != user_id
        assert member.user_id == user.id
        assert user.identity_id is None


@pytest.mark.asyncio
async def test_get_platform_user_by_org_member_creates_external_only_user():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id, provider_type="wechat")
    email = f"wechat-create-{uuid.uuid4().hex[:8]}@example.com"

    async with async_session() as db:
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=f"wx_{uuid.uuid4().hex[:8]}",
            name="微信新联系人",
            email=email,
            phone=None,
            status="active",
        )
        db.add(member)
        await db.commit()
        member_id = member.id

    async with async_session() as db:
        member = await db.get(OrgMember, member_id)
        user = await get_platform_user_by_org_member(db, member, agent_tenant_id=tenant.id)
        await db.commit()

        assert user.tenant_id == tenant.id
        assert user.registration_source == "wechat_org_sync"
        assert user.identity_id is None
        assert member.user_id == user.id


@pytest.mark.asyncio
async def test_get_platform_user_by_org_member_does_not_guess_email_or_phone_identity():
    tenant = await _seed_tenant()
    provider = await _seed_provider(tenant.id, provider_type="wechat")
    email = f"wechat-priority-{uuid.uuid4().hex[:8]}@example.com"
    phone = _phone()

    async with async_session() as db:
        email_identity = Identity(
            username=f"wechat_email_priority_{uuid.uuid4().hex[:8]}",
            email=email,
            password_hash="x",
        )
        phone_identity = Identity(
            username=f"wechat_phone_priority_{uuid.uuid4().hex[:8]}",
            email=f"wechat-phone-{uuid.uuid4().hex[:8]}@example.com",
            phone=phone,
            password_hash="x",
        )
        db.add_all([email_identity, phone_identity])
        await db.flush()
        email_user = User(
            identity_id=email_identity.id,
            tenant_id=tenant.id,
            display_name="Email Priority User",
            role="member",
            is_active=True,
        )
        phone_user = User(
            identity_id=phone_identity.id,
            tenant_id=tenant.id,
            display_name="Phone Priority User",
            role="member",
            is_active=True,
        )
        db.add_all([email_user, phone_user])
        member = OrgMember(
            tenant_id=tenant.id,
            provider_id=provider.id,
            external_id=f"wx_{uuid.uuid4().hex[:8]}",
            name="微信联系人优先级",
            email=email,
            phone=phone,
            status="active",
        )
        db.add(member)
        await db.commit()
        email_user_id = email_user.id
        phone_user_id = phone_user.id
        member_id = member.id

    async with async_session() as db:
        member = await db.get(OrgMember, member_id)
        user = await get_platform_user_by_org_member(db, member, agent_tenant_id=tenant.id)
        await db.commit()

        assert user.id != email_user_id
        assert user.id != phone_user_id
        assert member.user_id == user.id
        assert user.identity_id is None
