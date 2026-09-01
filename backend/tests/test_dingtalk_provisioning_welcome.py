"""DingTalk provisioning welcome delivery and resumption tests."""

from tests.test_dingtalk_provisioning import (
    UTC,
    Agent,
    ChatMessage,
    ChatSession,
    ChannelConfig,
    DINGTALK_PROVISIONING_STATUS_CONFIGURED,
    DINGTALK_PROVISIONING_STATUS_EXPIRED,
    DINGTALK_PROVISIONING_STATUS_POLLING,
    DINGTALK_PROVISIONING_STATUS_WAITING,
    DINGTALK_WELCOME_RETRY_DELAYS_SECONDS,
    DINGTALK_WELCOME_STATUS_FAILED,
    DINGTALK_WELCOME_STATUS_PENDING,
    DINGTALK_WELCOME_STATUS_SENT,
    DingTalkChannelProvisioningSession,
    FakeRegistrationClient,
    IMDeliveryResult,
    IdentityProvider,
    OrgMember,
    _seed_digital_employee,
    attach_delivery_to_meta,
    db_session,
    datetime,
    poll_dingtalk_provisioning_session,
    poll_due_dingtalk_provisioning_sessions,
    pytest,
    retry_due_dingtalk_welcome_messages,
    select,
    timedelta,
    uuid,
)

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_name",
    ["销售数字员工", "【cLaWiTh】销售数字员工"],
    ids=["ordinary-name", "mixed-case-forbidden-keyword"],
)
async def test_poll_success_sanitizes_and_sends_welcome_message_to_bound_dingtalk_user(
    db_session,
    agent_name,
):
    _, user, agent = await _seed_digital_employee(db_session)
    agent.name = agent_name
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
    pending_receipts = []

    async def fake_welcome_sender(app_id, app_secret, user_id, message):
        pending_row = (
            await db_session.execute(
                select(ChatMessage).where(
                    ChatMessage.external_event_key
                    == f"dingtalk-provisioning-welcome:{session.id}"
                )
            )
        ).scalar_one()
        pending_receipts.append(dict(pending_row.message_meta["delivery"]))
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
    assert len(pending_receipts) == 1
    assert pending_receipts[0]["status"] == "pending"
    assert pending_receipts[0]["attempt_id"]
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
    assert chat_message.message_meta["delivery"]["status"] == "sent"
    assert "attempt_id" not in chat_message.message_meta["delivery"]
    assert chat_message.message_meta["delivery"]["parts"] == [
        {
            "part_id": "0",
            "transport": "dingtalk_openapi_oto",
            "artifact_role": "channel_welcome",
            "provider_message_id": "welcome-process-key",
            "conversation_ref": "632277911",
            "send_status": "sent",
            "recall_status": "available",
            "metadata": {},
        }
    ]


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
async def test_welcome_timeout_is_unknown_and_is_never_sent_again(db_session):
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
        device_code="device-timeout",
        authorization_url="https://auth.example",
        registration_result={"client_id": "ding-client-id"},
        expires_at=now + timedelta(minutes=5),
        poll_interval_seconds=2,
        max_poll_attempts=10,
        welcome_status=DINGTALK_WELCOME_STATUS_PENDING,
        welcome_next_retry_at=now,
    )
    channel = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id="ding-client-id",
        app_secret="ding-client-secret",
        is_configured=True,
    )
    db_session.add_all([provider, member, provisioning, channel])
    await db_session.commit()
    send_count = 0

    async def timeout_sender(app_id, app_secret, user_id, message):
        nonlocal send_count
        send_count += 1
        raise TimeoutError

    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=timeout_sender,
        now=now,
    )

    assert count == 1
    assert send_count == 1
    assert provisioning.welcome_status == DINGTALK_WELCOME_STATUS_FAILED
    assert provisioning.welcome_next_retry_at is None
    assert provisioning.welcome_attempt_count == 1
    message = (
        await db_session.execute(
            select(ChatMessage).where(
                ChatMessage.external_event_key
                == f"dingtalk-provisioning-welcome:{provisioning.id}"
            )
        )
    ).scalar_one()
    assert message.message_meta["delivery"]["status"] == "unknown"

    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=timeout_sender,
        now=now + timedelta(minutes=5),
    )
    assert count == 0
    assert send_count == 1


@pytest.mark.asyncio
async def test_expired_pending_welcome_lease_becomes_unknown_without_duplicate_send(db_session):
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
        device_code="device-crashed-send",
        authorization_url="https://auth.example",
        registration_result={"client_id": "ding-client-id"},
        expires_at=now + timedelta(minutes=5),
        poll_interval_seconds=2,
        max_poll_attempts=10,
        welcome_status=DINGTALK_WELCOME_STATUS_PENDING,
        welcome_next_retry_at=now,
    )
    channel = ChannelConfig(
        agent_id=agent.id,
        channel_type="dingtalk",
        app_id="ding-client-id",
        app_secret="ding-client-secret",
        is_configured=True,
    )
    chat_session = ChatSession(
        agent_id=agent.id,
        user_id=user.id,
        title="欢迎消息",
        source_channel="dingtalk",
        external_conv_id="dingtalk_p2p_632277911",
    )
    db_session.add_all([provider, member, provisioning, channel, chat_session])
    await db_session.flush()
    pending_meta = attach_delivery_to_meta(
        {"artifact_role": "channel_welcome"},
        IMDeliveryResult.pending("dingtalk"),
    )
    pending_meta["delivery"]["attempt_id"] = str(uuid.uuid4())
    pending_meta["delivery"]["attempt_started_at"] = (
        now - timedelta(minutes=3)
    ).isoformat()
    message = ChatMessage(
        agent_id=agent.id,
        user_id=user.id,
        role="assistant",
        content="你好，我是销售数字员工。钉钉通道已配置完成，之后可以直接在这里和我对话。",
        conversation_id=str(chat_session.id),
        external_event_key=f"dingtalk-provisioning-welcome:{provisioning.id}",
        message_meta=pending_meta,
    )
    db_session.add(message)
    await db_session.commit()
    send_count = 0

    async def must_not_send(app_id, app_secret, user_id, text):
        nonlocal send_count
        send_count += 1
        return {"errcode": 0, "processQueryKey": "duplicate"}

    count = await retry_due_dingtalk_welcome_messages(
        db_session,
        welcome_sender=must_not_send,
        now=now,
    )

    assert count == 1
    assert send_count == 0
    assert provisioning.welcome_status == DINGTALK_WELCOME_STATUS_FAILED
    assert provisioning.welcome_next_retry_at is None
    assert provisioning.registration_result["welcome_message"]["error_code"] == "delivery_unknown"
    assert message.message_meta["delivery"]["status"] == "unknown"


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
