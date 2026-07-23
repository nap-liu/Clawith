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
async def test_non_reusable_flow_polls_final_success_before_issuing_new_link(db_session):
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
                "status": "SUCCESS",
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
@pytest.mark.parametrize("registration_status", ["APPROVING", "PUBLISHING"])
async def test_poll_dingtalk_application_build_states_keep_session_active(
    db_session,
    registration_status,
):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
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


@pytest.mark.asyncio
async def test_unknown_nonterminal_dingtalk_status_keeps_session_active(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-unknown-status",
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
            poll_responses=[{"status": "UNKNOWN_STAGE", "message": "ok"}]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert session.next_poll_at == now + timedelta(seconds=2)
    assert session.last_error is None
    assert session.registration_result["last_poll_status"] == "UNKNOWN_STAGE"


@pytest.mark.asyncio
async def test_unknown_dingtalk_status_expires_at_authorization_deadline(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-unknown-deadline",
        authorization_url="https://auth.example",
        expires_at=now,
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
            poll_responses=[{"status": "FUTURE_PENDING_STAGE", "message": "ok"}]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_EXPIRED
    assert session.next_poll_at is None
    assert "FUTURE_PENDING_STAGE" in session.last_error


@pytest.mark.asyncio
async def test_explicit_dingtalk_fail_is_terminal_and_does_not_report_ok_as_error(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="device-explicit-fail",
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
            poll_responses=[{"status": "FAIL", "message": "ok"}]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_FAILED
    assert session.last_error == "钉钉授权失败: FAIL"


@pytest.mark.asyncio
async def test_poll_transport_errors_retry_past_legacy_attempt_cap_until_deadline(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="transport-error",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(seconds=10),
        next_poll_at=now,
        poll_interval_seconds=2,
        poll_attempt_count=180,
        max_poll_attempts=180,
    )
    db_session.add(session)
    await db_session.flush()

    class PollFailureClient(FakeRegistrationClient):
        async def poll(self, device_code: str):
            self.poll_calls.append(device_code)
            raise RuntimeError("temporary poll failure")

    fake_client = PollFailureClient()

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_POLLING
    assert session.poll_attempt_count == 181
    assert session.next_poll_at == now + timedelta(seconds=2)
    assert "RuntimeError" in session.last_error

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        now=now + timedelta(seconds=10),
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_EXPIRED
    assert session.poll_attempt_count == 182
    assert session.next_poll_at is None


@pytest.mark.asyncio
async def test_poll_success_is_consumed_at_deadline_after_legacy_attempt_cap(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="late-success",
        authorization_url="https://auth.example",
        expires_at=now,
        next_poll_at=now,
        last_poll_at=now - timedelta(seconds=2),
        poll_interval_seconds=2,
        poll_attempt_count=180,
        max_poll_attempts=180,
    )
    db_session.add(session)
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            {
                "status": "SUCCESS",
                "client_id": "late-client-id",
                "client_secret": "late-client-secret",
            }
        ]
    )

    async def fake_stream_starter(agent_id, app_key, app_secret):
        return None

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=fake_client,
        stream_starter=fake_stream_starter,
        now=now,
    )

    assert fake_client.poll_calls == ["late-success"]
    assert session.poll_attempt_count == 181
    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one()
    assert config.app_id == "late-client-id"


@pytest.mark.asyncio
async def test_final_deadline_poll_waiting_expires_instead_of_rescheduling(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="deadline-waiting",
        authorization_url="https://auth.example",
        expires_at=now,
        next_poll_at=now,
        poll_interval_seconds=2,
        poll_attempt_count=899,
        max_poll_attempts=900,
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

    assert fake_client.poll_calls == ["deadline-waiting"]
    assert session.poll_attempt_count == 900
    assert session.status == DINGTALK_PROVISIONING_STATUS_EXPIRED
    assert session.next_poll_at is None


@pytest.mark.asyncio
async def test_full_30_minute_poll_window_consumes_success_on_attempt_900(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    started_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_WAITING,
        device_code="full-window-success",
        authorization_url="https://auth.example",
        expires_at=started_at + timedelta(minutes=30),
        next_poll_at=started_at + timedelta(seconds=2),
        poll_interval_seconds=2,
        max_poll_attempts=900,
    )
    db_session.add(session)
    await db_session.flush()
    fake_client = FakeRegistrationClient(
        poll_responses=[
            *({"status": "WAITING"} for _ in range(899)),
            {
                "status": "SUCCESS",
                "client_id": "attempt-900-client-id",
                "client_secret": "attempt-900-client-secret",
            },
        ]
    )

    async def fake_stream_starter(agent_id, app_key, app_secret):
        return None

    for attempt in range(1, 901):
        await poll_dingtalk_provisioning_session(
            db_session,
            session,
            registration_client=fake_client,
            stream_starter=fake_stream_starter,
            now=started_at + timedelta(seconds=attempt * 2),
        )

    assert session.poll_attempt_count == 900
    assert len(fake_client.poll_calls) == 900
    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent.id,
                ChannelConfig.channel_type == "dingtalk",
            )
        )
    ).scalar_one()
    assert config.app_id == "attempt-900-client-id"


@pytest.mark.asyncio
async def test_poll_success_configures_channel_and_defers_stream_to_reconciler(db_session):
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
    assert session.welcome_status == DINGTALK_WELCOME_STATUS_PENDING
    assert session.welcome_attempt_count == 0
    assert session.welcome_next_retry_at == now
    assert stream_starts == []


@pytest.mark.asyncio
async def test_force_poll_success_overwrites_only_matching_baseline(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    old_key = "old-app-key"
    old_secret = "old-app-secret"
    config = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id=old_key,
        app_secret=old_secret,
        is_configured=True,
        is_connected=True,
        extra_config={"connection_mode": "websocket"},
    )
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="force-success",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=10,
        registration_result={
            "operation": DINGTALK_PROVISIONING_OPERATION_FORCE,
            "baseline_fp": dingtalk_credential_fingerprint(old_key, old_secret),
        },
    )
    db_session.add_all([config, session])
    await db_session.flush()

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=FakeRegistrationClient(
            poll_responses=[
                {
                    "status": "SUCCESS",
                    "client_id": "new-app-key",
                    "client_secret": "new-app-secret",
                }
            ]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_CONFIGURED
    assert config.app_id == "new-app-key"
    assert config.app_secret == "new-app-secret"
    assert config.is_configured is True


@pytest.mark.asyncio
async def test_force_poll_does_not_overwrite_configuration_changed_during_authorization(db_session):
    _, user, agent = await _seed_digital_employee(db_session)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    config = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id="current-app-key",
        app_secret="manually-updated-secret",
        is_configured=True,
    )
    session = DingTalkChannelProvisioningSession(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        requested_by_user_id=user.id,
        status=DINGTALK_PROVISIONING_STATUS_POLLING,
        device_code="force-stale",
        authorization_url="https://auth.example",
        expires_at=now + timedelta(minutes=5),
        next_poll_at=now,
        poll_interval_seconds=2,
        max_poll_attempts=10,
        registration_result={
            "operation": DINGTALK_PROVISIONING_OPERATION_FORCE,
            "baseline_fp": dingtalk_credential_fingerprint(
                "current-app-key",
                "original-secret",
            ),
        },
    )
    db_session.add_all([config, session])
    await db_session.flush()

    await poll_dingtalk_provisioning_session(
        db_session,
        session,
        registration_client=FakeRegistrationClient(
            poll_responses=[
                {
                    "status": "SUCCESS",
                    "client_id": "new-app-key",
                    "client_secret": "new-app-secret",
                }
            ]
        ),
        now=now,
    )

    assert session.status == DINGTALK_PROVISIONING_STATUS_CANCELLED
    assert session.registration_result["completion_reason"] == "configuration_changed"
    assert config.app_id == "current-app-key"
    assert config.app_secret == "manually-updated-secret"


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
    assert stream_stops == []

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
    assert stream_starts == []


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
    assert session.welcome_status == DINGTALK_WELCOME_STATUS_PENDING
    assert session.welcome_attempt_count == 0
    assert sent_messages == []

    # Provisioning credentials commit before any external welcome-message side
    # effect. The connector's post-commit retry loop performs the send.
    await db_session.commit()
    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=fake_welcome_sender,
        now=now,
    )

    assert count == 1
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
    assert provisioning.welcome_attempt_count == 0
    assert provisioning.welcome_next_retry_at == now
    assert provisioning.last_error is None
    assert send_attempts == []

    await db_session.commit()
    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=permission_race_sender,
        now=now,
    )
    assert count == 1
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
    tenant, user, agent = await _seed_digital_employee(db_session)
    future_agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        creator_id=user.id,
        name="未来授权数字员工",
        agent_type="native",
        status="running",
    )
    expired_agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        creator_id=user.id,
        name="过期授权数字员工",
        agent_type="native",
        status="running",
    )
    db_session.add_all([future_agent, expired_agent])
    await db_session.flush()
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
        agent_id=future_agent.id,
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
        agent_id=expired_agent.id,
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

    assert count == 2
    assert sorted(fake_client.poll_calls) == ["due", "expired"]
    assert due.poll_attempt_count == 1
    assert future.poll_attempt_count == 0
    assert expired.status == DINGTALK_PROVISIONING_STATUS_EXPIRED
