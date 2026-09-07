"""DingTalk provisioning polling boundary and credential tests."""

from tests.test_dingtalk_provisioning import (
    UTC,
    Agent,
    ChannelConfig,
    DINGTALK_PROVISIONING_OPERATION_FORCE,
    DINGTALK_PROVISIONING_STATUS_CANCELLED,
    DINGTALK_PROVISIONING_STATUS_CONFIGURED,
    DINGTALK_PROVISIONING_STATUS_EXPIRED,
    DINGTALK_PROVISIONING_STATUS_FAILED,
    DINGTALK_PROVISIONING_STATUS_POLLING,
    DINGTALK_WELCOME_STATUS_PENDING,
    DingTalkChannelProvisioningSession,
    FakeRegistrationClient,
    _seed_digital_employee,
    db_session as db_session,
    datetime,
    dingtalk_credential_fingerprint,
    poll_dingtalk_provisioning_session,
    pytest,
    select,
    timedelta,
    uuid,
)

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
@pytest.mark.parametrize("registration_status", ["SUCCESS", "APPROVING"])
async def test_poll_ready_credentials_are_consumed_at_deadline_after_legacy_attempt_cap(
    db_session,
    registration_status,
):
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
                "status": registration_status,
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
@pytest.mark.parametrize(
    "poll_response",
    [
        {"status": "WAITING"},
        {"status": "APPROVING", "client_id": "partial-client-id"},
    ],
)
async def test_final_deadline_poll_nonready_response_expires_instead_of_rescheduling(
    db_session,
    poll_response,
):
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
    fake_client = FakeRegistrationClient(poll_responses=[poll_response])

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
@pytest.mark.parametrize("registration_status", ["SUCCESS", "APPROVING"])
async def test_force_poll_ready_credentials_overwrite_only_matching_baseline(
    db_session,
    registration_status,
):
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
                    "status": registration_status,
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
@pytest.mark.parametrize("registration_status", ["SUCCESS", "APPROVING"])
async def test_force_poll_ready_credentials_do_not_overwrite_configuration_changed_during_authorization(
    db_session,
    registration_status,
):
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
                    "status": registration_status,
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
