import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.audit import ChatMessage
from app.models.agent import Agent, AgentTemplate
from app.models.channel_config import ChannelConfig
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.dingtalk_provisioning import (
    DINGTALK_PROVISIONING_STATUS_CANCELLED,
    DINGTALK_PROVISIONING_STATUS_CONFIGURED,
    DINGTALK_PROVISIONING_STATUS_EXPIRED,
    DINGTALK_PROVISIONING_STATUS_FAILED,
    DINGTALK_PROVISIONING_STATUS_POLLING,
    DINGTALK_PROVISIONING_STATUS_WAITING,
    DINGTALK_WELCOME_STATUS_FAILED,
    DINGTALK_WELCOME_STATUS_PENDING,
    DINGTALK_WELCOME_STATUS_SENT,
    DingTalkChannelProvisioningSession,
)
from app.models.identity import IdentityProvider
from app.models.llm import LLMModel
from app.models.org import OrgMember
from app.models.participant import Participant
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.dingtalk_provisioning import (
    DINGTALK_WELCOME_RETRY_DELAYS_SECONDS,
    _bounded_polling_window,
    poll_dingtalk_provisioning_session,
    poll_due_dingtalk_provisioning_sessions,
    retry_due_dingtalk_welcome_messages,
    start_dingtalk_channel_provisioning,
)


class FakeRegistrationClient:
    def __init__(self, *, begin_response=None, poll_responses=None):
        self.begin_response = begin_response or {
            "device_code": "device-code-1",
            "verification_uri_complete": "https://oapi.dingtalk.com/device/complete",
            "verification_uri": "https://oapi.dingtalk.com/device",
            "expires_in": 7200,
            "interval": 1,
        }
        self.poll_responses = list(poll_responses or [])
        self.begin_calls = 0
        self.poll_calls = []

    async def begin(self):
        self.begin_calls += 1
        return dict(self.begin_response)

    async def poll(self, device_code: str):
        self.poll_calls.append(device_code)
        if self.poll_responses:
            return dict(self.poll_responses.pop(0))
        return {"status": "WAITING"}


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Identity.__table__,
        Tenant.__table__,
        User.__table__,
        LLMModel.__table__,
        AgentTemplate.__table__,
        Agent.__table__,
        ChannelConfig.__table__,
        DingTalkChannelProvisioningSession.__table__,
        IdentityProvider.__table__,
        OrgMember.__table__,
        Participant.__table__,
        ChatSession.__table__,
        ChatCompaction.__table__,
        ChatMessage.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _seed_digital_employee(db):
    tenant = Tenant(id=uuid.uuid4(), name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = User(
        id=uuid.uuid4(),
        identity_id=None,
        tenant_id=tenant.id,
        display_name="Creator",
        role="member",
        is_active=True,
    )
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        creator_id=user.id,
        name="销售数字员工",
        agent_type="native",
        status="running",
    )
    db.add_all([tenant, user, agent])
    await db.flush()
    return tenant, user, agent


def test_bounded_polling_window_caps_dingtalk_values():
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    window = _bounded_polling_window(expires_in=7200, interval=1, now=now)

    assert window.expires_at == now + timedelta(seconds=1800)
    assert window.poll_interval_seconds == 2
    assert window.next_poll_at == now + timedelta(seconds=2)
    assert window.max_poll_attempts == 180


@pytest.mark.asyncio
async def test_start_provisioning_persists_session_and_cancels_previous_pending(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    old = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="old-device",
        authorization_url="https://old.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now + timedelta(seconds=5),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add(old)
    await db_session.flush()
    fake_client = FakeRegistrationClient()

    result = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        registration_client=fake_client,
        now=now,
    )

    assert fake_client.begin_calls == 1
    assert old.status == DINGTALK_PROVISIONING_STATUS_CANCELLED
    assert result["status"] == DINGTALK_PROVISIONING_STATUS_WAITING
    assert result["authorization_url"] == "https://oapi.dingtalk.com/device/complete"
    assert result["expires_at"] == (now + timedelta(seconds=1800)).isoformat()
    assert "数字员工" in result["message"]
    assert "Agent" not in result["message"]
    assert "client_secret" not in str(result)

    stored = await db_session.get(DingTalkChannelProvisioningSession, uuid.UUID(result["provisioning_id"]))
    assert stored is not None
    assert stored.device_code == "device-code-1"
    assert stored.poll_interval_seconds == 2
    assert stored.max_poll_attempts == 180


@pytest.mark.asyncio
async def test_poll_waiting_reschedules_without_unbounded_attempts(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="device-wait",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=3,
        max_poll_attempts=1,
    )
    db_session.add(session)
    await db_session.flush()
    fake_client = FakeRegistrationClient(poll_responses=[{"status": "WAITING"}])

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now,
    )

    assert fake_client.poll_calls == ["device-wait"]
    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert session.poll_attempt_count == 1
    assert session.last_poll_at == now
    assert session.next_poll_at == now + timedelta(seconds=3)

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now + timedelta(seconds=3),
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_FAILED
    assert len(fake_client.poll_calls) == 1
    assert "最大轮询次数" in session.last_error


@pytest.mark.asyncio
async def test_poll_success_configures_dingtalk_channel_and_starts_stream(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-success",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add(session)
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": "SUCCESS",
                "client_id": "ding-client-id",
                "client_secret": "ding-client-secret",
            }
        ]
    )
    stream_starts = []

    async def fake_stream_starter(agent_id, app_key, app_secret):
        stream_starts.append((agent_id, app_key, app_secret))

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        stream_starter=fake_stream_starter,
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert session.registration_result == {"client_id": "ding-client-id"}
    assert "ding-client-secret" not in str(session.registration_result)

    result = await db_session.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent.id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one()
    assert config.app_id == "ding-client-id"
    assert config.app_secret == "ding-client-secret"
    assert config.is_configured is True
    assert config.extra_config["connection_mode"] == "websocket"
    assert config.extra_config["agent_id"] == "ding-client-id"
    assert config.extra_config["provisioning_session_id"] == str(session.id)
    assert stream_starts == [(agent.id, "ding-client-id", "ding-client-secret")]


@pytest.mark.asyncio
async def test_poll_success_replaces_existing_dingtalk_robot_binding_on_other_digital_employee(db_session):
    tenant, user, new_agent = await _seed_digital_employee(db_session)
    old_agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        creator_id=user.id,
        name="旧数字员工",
        agent_type="native",
        status="running",
    )
    old_config = ChannelConfig(
        agent_id=old_agent.id,
        channel_type="dingtalk",
        app_id="ding-client-id",
        app_secret="old-secret",
        is_configured=True,
        is_connected=True,
        extra_config={"connection_mode": "websocket", "agent_id": "ding-client-id"},
    )
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=new_agent.id,
        tenant_id=new_agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-success",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add_all([old_agent, old_config, session])
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": "SUCCESS",
                "client_id": "ding-client-id",
                "client_secret": "new-secret",
            }
        ]
    )
    stream_starts = []
    stream_stops = []

    async def fake_stream_starter(agent_id, app_key, app_secret):
        stream_starts.append((agent_id, app_key, app_secret))

    async def fake_stream_stopper(agent_id):
        stream_stops.append(agent_id)

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        stream_starter=fake_stream_starter,
        stream_stopper=fake_stream_stopper,
        now=now,
    )

    assert old_config.is_configured is False
    assert old_config.is_connected is False
    assert old_config.app_id is None
    assert old_config.app_secret is None
    assert old_config.extra_config["replaced_by_agent_id"] == str(new_agent.id)
    assert old_config.extra_config["replaced_by_provisioning_session_id"] == str(session.id)
    assert stream_stops == [old_agent.id]

    new_config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == new_agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one()
    assert new_config.app_id == "ding-client-id"
    assert new_config.app_secret == "new-secret"
    assert new_config.is_configured is True
    assert stream_starts == [(new_agent.id, "ding-client-id", "new-secret")]


@pytest.mark.asyncio
async def test_poll_success_sends_welcome_message_to_bound_dingtalk_user(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        provider_type="dingtalk",
        name="DingTalk",
        is_active=True,
    )
    member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        provider_id=provider.id,
        user_id=user.id,
        external_id="632277911",
        name="刘喜",
        status="active",
    )
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-success",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add_all([provider, member, session])
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": "SUCCESS",
                "client_id": "ding-client-id",
                "client_secret": "ding-client-secret",
            }
        ]
    )
    sent_messages = []

    async def fake_welcome_sender(app_id, app_secret, user_id, message):
        sent_messages.append((app_id, app_secret, user_id, message))
        return {"errcode": 0, "processQueryKey": "welcome-process-key"}

    async def fake_stream_starter(agent_id, app_key, app_secret):
        return None

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        stream_starter=fake_stream_starter,
        welcome_sender=fake_welcome_sender,
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert session.welcome_status == DINGTALK_WELCOME_STATUS_SENT
    assert session.welcome_attempt_count == 1
    assert session.welcome_next_retry_at is None
    assert session.welcome_last_error is None
    assert session.welcome_sent_at == now
    assert sent_messages == [
        (
            "ding-client-id",
            "ding-client-secret",
            "632277911",
            "你好，我是销售数字员工。钉钉通道已配置完成，之后可以直接在这里和我对话。",
        )
    ]
    assert session.registration_result == {
        "client_id": "ding-client-id",
        "welcome_message": {
            "status": "sent",
            "user_id": "632277911",
            "process_query_key": "welcome-process-key",
        },
    }

    chat_session = (
        await db_session.execute(
            select(ChatSession).where(
                ChatSession.agent_id == agent.id,
                ChatSession.external_conv_id == "dingtalk_p2p_632277911",
            )
        )
    ).scalar_one()
    assert chat_session.user_id == user.id
    assert chat_session.source_channel == "dingtalk"

    chat_message = (
        await db_session.execute(
            select(ChatMessage).where(
                ChatMessage.agent_id == agent.id,
                ChatMessage.conversation_id == str(chat_session.id),
            )
        )
    ).scalar_one()
    assert chat_message.role == "assistant"
    assert chat_message.content == "你好，我是销售数字员工。钉钉通道已配置完成，之后可以直接在这里和我对话。"


@pytest.mark.asyncio
async def test_poll_success_retries_welcome_after_permission_propagates(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        provider_type="dingtalk",
        name="DingTalk",
        is_active=True,
    )
    member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        provider_id=provider.id,
        user_id=user.id,
        external_id="632277911",
        name="刘喜",
        status="active",
    )
    provisioning = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-permission-race",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=10,
    )
    db_session.add_all([provider, member, provisioning])
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": "SUCCESS",
                "client_id": "ding-client-id",
                "client_secret": "ding-client-secret",
            }
        ]
    )
    send_attempts = []

    async def permission_race_sender(app_id, app_secret, user_id, message):
        send_attempts.append((app_id, user_id, message))
        if len(send_attempts) == 1:
            return {
                "errcode": 403,
                "errmsg": (
                    "Forbidden.AccessDenied.AccessTokenPermissionDenied: "
                    "应用尚未开通所需的权限：[qyapi_robot_sendmsg]"
                ),
            }
        return {"errcode": 0, "processQueryKey": "retry-process-key"}

    async def fake_stream_starter(agent_id, app_key, app_secret):
        return None

    await poll_dingtalk_provisioning_session(
        db_session,
        provisioning,
        registration_client=fake_client,
        stream_starter=fake_stream_starter,
        welcome_sender=permission_race_sender,
        now=now,
    )

    assert provisioning.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert provisioning.welcome_status == DINGTALK_WELCOME_STATUS_PENDING
    assert provisioning.welcome_attempt_count == 1
    assert provisioning.welcome_next_retry_at == now + timedelta(seconds=2)
    assert "正在自动重试" in provisioning.last_error

    # A connector tick before the persisted deadline must not send early.
    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=permission_race_sender,
        now=now + timedelta(seconds=1),
    )
    assert count == 0
    assert len(send_attempts) == 1

    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=permission_race_sender,
        now=now + timedelta(seconds=2),
    )
    assert count == 1
    assert len(send_attempts) == 2
    assert provisioning.welcome_status == DINGTALK_WELCOME_STATUS_SENT
    assert provisioning.welcome_attempt_count == 2
    assert provisioning.welcome_next_retry_at is None
    assert provisioning.welcome_last_error is None
    assert provisioning.welcome_sent_at == now + timedelta(seconds=2)
    assert provisioning.last_error is None
    assert provisioning.registration_result["welcome_message"] == {
        "status": "sent",
        "user_id": "632277911",
        "process_query_key": "retry-process-key",
    }

    messages = (
        await db_session.execute(
            select(ChatMessage).where(ChatMessage.agent_id == agent.id, ChatMessage.role == "assistant")
        )
    ).scalars().all()
    assert [message.content for message in messages] == [
        "你好，我是销售数字员工。钉钉通道已配置完成，之后可以直接在这里和我对话。"
    ]


@pytest.mark.asyncio
async def test_welcome_retry_is_bounded_and_does_not_reconfigure_channel(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    provider = IdentityProvider(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        provider_type="dingtalk",
        name="DingTalk",
        is_active=True,
    )
    member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        provider_id=provider.id,
        user_id=user.id,
        external_id="632277911",
        name="刘喜",
        status="active",
    )
    provisioning = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_CONFIGURED,
        device_code="device-bounded-retry",
        authorization_url="https://auth.example",
        registration_result={"client_id": "ding-client-id"},
        expires_at=now + timedelta(minutes=5),
        poll_interval_seconds=2,
        max_poll_attempts=10,
        welcome_status=DINGTALK_WELCOME_STATUS_PENDING,
        welcome_attempt_count=len(DINGTALK_WELCOME_RETRY_DELAYS_SECONDS),
        welcome_next_retry_at=now,
    )
    channel = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id="ding-client-id",
        app_secret="ding-client-secret",
        is_configured=True,
        extra_config={"connection_mode": "websocket"},
    )
    db_session.add_all([provider, member, provisioning, channel])
    await db_session.flush()

    async def always_permission_denied(app_id, app_secret, user_id, message):
        return {
            "errcode": 403,
            "errmsg": "AccessTokenPermissionDenied: qyapi_robot_sendmsg",
        }

    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=always_permission_denied,
        now=now,
    )

    assert count == 1
    assert provisioning.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert provisioning.welcome_status == DINGTALK_WELCOME_STATUS_FAILED
    assert provisioning.welcome_attempt_count == len(DINGTALK_WELCOME_RETRY_DELAYS_SECONDS) + 1
    assert provisioning.welcome_next_retry_at is None
    assert "自动重试已停止" in provisioning.last_error
    assert channel.is_configured is True
    assert channel.app_id == "ding-client-id"


@pytest.mark.asyncio
async def test_poll_success_does_not_write_internal_notes_to_origin_web_session(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    web_session = ChatSession(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        title="配置钉钉通道",
        source_channel="web",
    )
    provisioning = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-success",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add_all([web_session, provisioning])
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": "SUCCESS",
                "client_id": "ding-client-id",
                "client_secret": "ding-client-secret",
            }
        ]
    )
    async def fake_stream_starter(agent_id, app_key, app_secret):
        return None

    await poll_dingtalk_provisioning_session(
        db_session,
        provisioning,
        registration_client=fake_client,
        stream_starter=fake_stream_starter,
        now=now,
    )

    assert provisioning.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert web_session.last_message_at is None

    result = await db_session.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(web_session.id))
        .order_by(ChatMessage.created_at.asc())
    )
    messages = result.scalars().all()
    assert messages == []


@pytest.mark.asyncio
async def test_poll_terminal_expired_status_stops_session(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="device-expired",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now,
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add(session)
    await db_session.flush()
    fake_client = FakeRegistrationClient(poll_responses=[{"status": "EXPIRED", "message": "expired"}])

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_EXPIRED
    assert session.next_poll_at is None
    assert "expired" in session.last_error


@pytest.mark.asyncio
async def test_poll_due_sessions_resumes_only_unexpired_due_sessions(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    due = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="due",
        authorization_url="https://auth.example/due",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    future = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="future",
        authorization_url="https://auth.example/future",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now + timedelta(minutes=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    expired = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="expired",
        authorization_url="https://auth.example/expired",
        expires_at=now - timedelta(seconds=1),
        next_poll_at=now - timedelta(seconds=1),
        poll_interval_seconds=5,
        max_poll_attempts=10,
    )
    db_session.add_all([due, future, expired])
    await db_session.flush()
    fake_client = FakeRegistrationClient(poll_responses=[{"status": "WAITING"}])

    count = await poll_due_dingtalk_provisioning_sessions(
        db_session,
        registration_client=fake_client,
        now=now,
        limit=10,
    )

    assert count == 1
    assert fake_client.poll_calls == ["due"]
    assert due.poll_attempt_count == 1
    assert future.poll_attempt_count == 0
    assert expired.status == DINGTALK_PROVISIONING_STATUS_EXPIRED
