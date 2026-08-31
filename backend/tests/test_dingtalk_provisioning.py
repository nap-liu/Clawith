import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent import Agent, AgentTemplate
from app.models.audit import ChatMessage
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
from app.services.dingtalk_credentials import dingtalk_credential_fingerprint
from app.services.dingtalk_provisioning import (
    DINGTALK_PROVISIONING_OPERATION_FORCE,
    DINGTALK_WELCOME_RETRY_DELAYS_SECONDS,
    DingTalkRegistrationClient,
    _bounded_polling_window,
    poll_dingtalk_provisioning_session,
    poll_due_dingtalk_provisioning_sessions,
    retry_due_dingtalk_welcome_messages,
    start_dingtalk_channel_provisioning,
)
from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta


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
    assert window.max_poll_attempts == 900


@pytest.mark.asyncio
async def test_registration_poll_preserves_numeric_dingtalk_agent_id(monkeypatch):
    client = DingTalkRegistrationClient()

    async def fake_post(_path, _payload):
        return {
            "status": "SUCCESS",
            "client_id": "ding-client-id",
            "client_secret": "ding-client-secret",
            "agent_id": "4806241691",
            "errmsg": "ok",
        }

    monkeypatch.setattr(client, "_post", fake_post)

    result = await client.poll("device-code")

    assert result["agent_id"] == "4806241691"


@pytest.mark.asyncio
async def test_start_provisioning_reuses_valid_flow_even_with_legacy_restart_flag(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    existing = DingTalkChannelProvisioningSession(
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
    db_session.add(existing)
    await db_session.flush()
    fake_client = FakeRegistrationClient()

    result = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        restart_existing=True,
        registration_client=fake_client,
        now=now,
    )

    assert fake_client.begin_calls == 0
    assert existing.status == DINGTALK_PROVISIONING_STATUS_WAITING
    assert result["flow_action"] == "reused"
    assert result["provisioning_id"] == str(existing.id)
    assert result["authorization_url"] == existing.authorization_url


@pytest.mark.asyncio
async def test_start_provisioning_reuses_unexpired_flow_when_restart_is_false(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    existing = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="existing-device",
        authorization_url="https://auth.example/existing",
        expires_at=now + timedelta(minutes=20),
        next_poll_at=now + timedelta(seconds=2),
        poll_interval_seconds=2,
        max_poll_attempts=900,
    )
    db_session.add(existing)
    await db_session.flush()
    fake_client = FakeRegistrationClient()

    result = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        restart_existing=False,
        registration_client=fake_client,
        now=now,
    )

    assert result["flow_action"] == "reused"
    assert result["provisioning_id"] == str(existing.id)
    assert result["authorization_url"] == existing.authorization_url
    assert existing.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert fake_client.begin_calls == 0
    assert fake_client.poll_calls == []


@pytest.mark.asyncio
async def test_start_reports_configured_channel_and_only_force_creates_new_flow(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    config = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id="configured-app-key",
        app_secret="configured-app-secret",
        is_configured=True,
        extra_config={"connection_mode": "websocket"},
    )
    db_session.add(config)
    await db_session.flush()
    fake_client = FakeRegistrationClient()

    result = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        registration_client=fake_client,
        now=now,
    )

    assert result["status"] == "already_configured"
    assert result["flow_action"] == "already_configured"
    assert result["authorization_url"] is None
    assert "无需重复配置" in result["message"]
    assert "强制重配" in result["message"]
    assert fake_client.begin_calls == 0

    forced = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        force_reconfigure=True,
        registration_client=fake_client,
        now=now,
    )

    assert forced["flow_action"] == "created"
    assert forced["authorization_url"] == "https://oapi.dingtalk.com/device/complete"
    assert fake_client.begin_calls == 1
    stored = await db_session.get(
        DingTalkChannelProvisioningSession,
        uuid.UUID(forced["provisioning_id"]),
    )
    assert stored.registration_result["operation"] == DINGTALK_PROVISIONING_OPERATION_FORCE
    assert stored.registration_result["baseline_fp"] == dingtalk_credential_fingerprint(
        "configured-app-key",
        "configured-app-secret",
    )
    assert "configured-app-secret" not in str(stored.registration_result)

@pytest.mark.asyncio
async def test_force_begin_failure_keeps_existing_force_flow_active(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    config = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id="configured-app-key",
        app_secret="configured-app-secret",
        is_configured=True,
    )
    existing = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="existing-device",
        authorization_url="https://auth.example/existing",
        expires_at=now + timedelta(minutes=20),
        next_poll_at=now + timedelta(seconds=2),
        poll_interval_seconds=2,
        max_poll_attempts=900,
        registration_result={
            "operation": DINGTALK_PROVISIONING_OPERATION_FORCE,
            "baseline_fp": "stale-baseline",
        },
    )
    db_session.add_all([config, existing])
    await db_session.flush()

    class BeginFailureClient(FakeRegistrationClient):
        async def begin(self):
            self.begin_calls += 1
            raise RuntimeError("temporary begin failure")

    fake_client = BeginFailureClient()

    with pytest.raises(RuntimeError, match="temporary begin failure"):
        await start_dingtalk_channel_provisioning(
            db_session,
            agent=agent,
            requested_by_user_id=user.id,
            force_reconfigure=True,
            registration_client=fake_client,
            now=now,
        )

    assert fake_client.poll_calls == ["existing-device"]
    assert fake_client.begin_calls == 1
    assert existing.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert existing.next_poll_at == now + timedelta(seconds=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_status", ["SUCCESS", "APPROVING"])
async def test_non_reusable_flow_polls_final_ready_credentials_before_issuing_new_link(
    db_session,
    registration_status,
):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    existing = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="just-authorized",
        authorization_url="https://auth.example/old",
        expires_at=now - timedelta(seconds=1),
        next_poll_at=now - timedelta(seconds=2),
        poll_interval_seconds=2,
        max_poll_attempts=900,
    )
    db_session.add(existing)
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": registration_status,
                "client_id": "existing-success-key",
                "client_secret": "existing-success-secret",
            }
        ]
    )

    result = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        registration_client=fake_client,
        now=now,
    )

    assert result["flow_action"] == "configured_existing"
    assert result["provisioning_id"] == str(existing.id)
    assert fake_client.poll_calls == ["just-authorized"]
    assert fake_client.begin_calls == 0
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one()
    assert config.app_id == "existing-success-key"
    assert config.app_secret == "existing-success-secret"


@pytest.mark.asyncio
async def test_poll_waiting_continues_past_observability_attempt_count_until_deadline(db_session):
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

    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert session.poll_attempt_count == 2
    assert fake_client.poll_calls == ["device-wait", "device-wait"]
    assert session.last_error is None
    assert session.next_poll_at == now + timedelta(seconds=6)


@pytest.mark.asyncio
async def test_poll_dingtalk_publishing_with_credentials_keeps_session_active(
    db_session,
):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    registration_status = "PUBLISHING"
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code=f"device-{registration_status.lower()}",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=150,
    )
    db_session.add(session)
    await db_session.flush()

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=FakeRegistrationClient(
            poll_responses=[
                {
                    "status": registration_status,
                    "client_id": "pending-client-id",
                    "client_secret": "pending-client-secret",
                    "message": "ok",
                }
            ]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert session.next_poll_at == now + timedelta(seconds=2)
    assert session.last_error is None
    assert session.registration_result["last_poll_status"] == registration_status
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one_or_none()
    assert config is None
    assert "pending-client-secret" not in str(session.registration_result)


@pytest.mark.asyncio
async def test_poll_dingtalk_approving_with_complete_credentials_configures_channel(
    db_session,
):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 24, 3, 42, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-approving-ready",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=30),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=900,
    )
    db_session.add(session)
    await db_session.flush()

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=FakeRegistrationClient(
            poll_responses=[
                {
                    "status": "APPROVING",
                    "client_id": "approving-client-id",
                    "client_secret": "approving-client-secret",
                    "agent_id": "4806241691",
                    "message": "ok",
                }
            ]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert session.next_poll_at is None
    assert session.last_error is None
    assert session.registration_result == {
        "client_id": "approving-client-id",
        "agent_id": "4806241691",
        "last_poll_status": "APPROVING",
        "completion_reason": "credentials_ready_while_approving",
    }
    assert "approving-client-secret" not in str(session.registration_result)
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one()
    assert config.app_id == "approving-client-id"
    assert config.app_secret == "approving-client-secret"
    assert config.is_configured is True
    assert config.is_connected is False
    assert config.extra_config["connection_mode"] == "websocket"
    assert config.extra_config["agent_id"] == "4806241691"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poll_response",
    [
        {"status": "APPROVING", "message": "ok"},
        {
            "status": "APPROVING",
            "client_id": "partial-client-id",
            "message": "ok",
        },
        {
            "status": "APPROVING",
            "client_secret": "partial-client-secret",
            "message": "ok",
        },
    ],
)
async def test_poll_dingtalk_approving_without_complete_credentials_keeps_session_active(
    db_session,
    poll_response,
):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 24, 3, 42, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-approving-incomplete",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=30),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=900,
    )
    db_session.add(session)
    await db_session.flush()

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=FakeRegistrationClient(poll_responses=[poll_response]),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert session.next_poll_at == now + timedelta(seconds=2)
    assert session.last_error is None
    assert session.registration_result["last_poll_status"] == "APPROVING"
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one_or_none()
    assert config is None
    assert "partial-client-secret" not in str(session.registration_result)


@pytest.mark.asyncio
async def test_retry_after_approving_reuses_flow_without_creating_duplicate_app(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="device-approving-reuse",
        authorization_url="https://auth.example/original",
        expires_at=now + timedelta(minutes=30),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=900,
    )
    db_session.add(session)
    await db_session.flush()
    approving_client = FakeRegistrationClient(
        poll_responses=[{"status": "APPROVING", "message": "ok"}]
    )

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=approving_client,
        now=now,
    )

    retry_client = FakeRegistrationClient()
    result = await start_dingtalk_channel_provisioning(
        db_session,
        agent=agent,
        requested_by_user_id=user.id,
        force_reconfigure=True,
        registration_client=retry_client,
        now=now + timedelta(seconds=1),
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert result["flow_action"] == "reused"
    assert result["provisioning_id"] == str(session.id)
    assert result["authorization_url"] == "https://auth.example/original"
    assert retry_client.begin_calls == 0


@pytest.mark.asyncio
async def test_poll_approving_publishing_then_success_configures_same_dingtalk_flow(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-publishing-success",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=150,
    )
    db_session.add(session)
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {"status": "APPROVING", "message": "ok"},
            {"status": "PUBLISHING", "message": "ok"},
            {
                "status": "SUCCESS",
                "client_id": "published-client-id",
                "client_secret": "published-client-secret",
                "agent_id": "4806241691",
                "message": "ok",
            },
        ]
    )

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now,
    )
    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now + timedelta(seconds=2),
    )
    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now + timedelta(seconds=4),
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert fake_client.poll_calls == [
        "device-publishing-success",
        "device-publishing-success",
        "device-publishing-success",
    ]
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one()
    assert config.app_id == "published-client-id"
    assert config.app_secret == "published-client-secret"
    assert config.extra_config["agent_id"] == "4806241691"
    assert session.registration_result["agent_id"] == "4806241691"
