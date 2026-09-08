import asyncio
import json
import threading
import uuid

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import engine
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant import Tenant
from app.models.user import User
from app.services import dingtalk_stream
from app.services.dingtalk_credentials import dingtalk_credential_fingerprint
from app.services.dingtalk_stream import DingTalkStreamManager, _StreamRuntime
from app.services.dingtalk_token import DingTalkTokenManager


@pytest.fixture
async def stream_db():
    await engine.dispose()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        # Reconciliation scans every tenant; isolate rows inside this rollback.
        await connection.execute(delete(ChannelConfig))
        yield async_sessionmaker(
            connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        await transaction.rollback()
    await engine.dispose()


async def _add_agent(session, tenant, user, *, name, is_deleted=False):
    agent = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        creator_id=user.id,
        name=name,
        agent_type="native",
        status="running",
        is_deleted=is_deleted,
    )
    session.add(agent)
    await session.flush()
    return agent


@pytest.mark.asyncio
async def test_reconcile_selects_only_live_websocket_channels(monkeypatch, stream_db):
    async with stream_db() as session:
        live_tenant = Tenant(id=uuid.uuid4(), name="Live", slug=f"live-{uuid.uuid4().hex[:8]}")
        inactive_tenant = Tenant(
            id=uuid.uuid4(),
            name="Inactive",
            slug=f"inactive-{uuid.uuid4().hex[:8]}",
            is_active=False,
        )
        live_user = User(
            id=uuid.uuid4(),
            tenant_id=live_tenant.id,
            display_name="Live owner",
            role="member",
            is_active=True,
        )
        inactive_user = User(
            id=uuid.uuid4(),
            tenant_id=inactive_tenant.id,
            display_name="Inactive owner",
            role="member",
            is_active=True,
        )
        session.add_all([live_tenant, inactive_tenant, live_user, inactive_user])
        await session.flush()

        desired_agent = await _add_agent(
            session,
            live_tenant,
            live_user,
            name="Historical websocket default",
        )
        webhook_agent = await _add_agent(session, live_tenant, live_user, name="Webhook")
        deleted_agent = await _add_agent(
            session,
            live_tenant,
            live_user,
            name="Deleted",
            is_deleted=True,
        )
        inactive_agent = await _add_agent(
            session,
            inactive_tenant,
            inactive_user,
            name="Inactive tenant",
        )
        session.add_all(
            [
                ChannelConfig(
                    agent_id=desired_agent.id,
                    channel_type="dingtalk",
                    app_id="desired-key",
                    app_secret="desired-secret",
                    is_configured=True,
                    extra_config="legacy-invalid-json-shape",
                ),
                ChannelConfig(
                    agent_id=webhook_agent.id,
                    channel_type="dingtalk",
                    app_id="webhook-key",
                    app_secret="webhook-secret",
                    is_configured=True,
                    extra_config={"connection_mode": "webhook"},
                ),
                ChannelConfig(
                    agent_id=deleted_agent.id,
                    channel_type="dingtalk",
                    app_id="deleted-key",
                    app_secret="deleted-secret",
                    is_configured=True,
                    extra_config={"connection_mode": "websocket"},
                ),
                ChannelConfig(
                    agent_id=inactive_agent.id,
                    channel_type="dingtalk",
                    app_id="inactive-key",
                    app_secret="inactive-secret",
                    is_configured=True,
                    extra_config={"connection_mode": "websocket"},
                ),
            ]
        )
        await session.commit()

    manager = DingTalkStreamManager()
    stale_agent_id = uuid.uuid4()
    manager._runtimes[stale_agent_id] = object()
    starts = []
    stops = []

    async def fake_start(
        agent_id,
        app_key,
        app_secret,
        stop_existing=True,
        verify_persisted=True,
    ):
        starts.append(
            (agent_id, app_key, app_secret, stop_existing, verify_persisted)
        )

    async def fake_stop(agent_id, verify_persisted=True):
        stops.append((agent_id, verify_persisted))
        manager._runtimes.pop(agent_id, None)

    monkeypatch.setattr(dingtalk_stream, "async_session", stream_db)
    monkeypatch.setattr(manager, "start_client", fake_start)
    monkeypatch.setattr(manager, "stop_client", fake_stop)

    await manager.reconcile_once()

    assert starts == [
        (desired_agent.id, "desired-key", "desired-secret", False, False)
    ]
    assert stops == [(stale_agent_id, False)]


@pytest.mark.asyncio
async def test_start_is_idempotent_and_secret_change_stops_old_runner_first(monkeypatch):
    manager = DingTalkStreamManager()
    agent_id = uuid.uuid4()
    events = []
    first_started = threading.Event()
    second_started = threading.Event()

    def fake_runner(
        runner_agent_id,
        app_key,
        app_secret,
        stop_event,
        generation,
        fingerprint,
    ):
        events.append(("start", generation, app_key, app_secret))
        (first_started if generation == 1 else second_started).set()
        stop_event.wait(2)
        events.append(("stop", generation))

    async def fake_set_connected(_agent_id, _connected):
        return None

    monkeypatch.setattr(dingtalk_stream, "_connector_role_enabled", lambda: True)
    monkeypatch.setattr(manager, "_run_client_thread", fake_runner)
    monkeypatch.setattr(manager, "_set_persisted_connected", fake_set_connected)

    await manager.start_client(
        agent_id,
        "same-key",
        "secret-v1",
        verify_persisted=False,
    )
    assert await asyncio.to_thread(first_started.wait, 1)
    first_generation = manager._runtimes[agent_id].generation

    await manager.start_client(
        agent_id,
        "same-key",
        "secret-v1",
        stop_existing=False,
        verify_persisted=False,
    )
    assert manager._runtimes[agent_id].generation == first_generation
    assert [event for event in events if event[0] == "start"] == [
        ("start", 1, "same-key", "secret-v1")
    ]

    await manager.start_client(
        agent_id,
        "same-key",
        "secret-v2",
        stop_existing=False,
        verify_persisted=False,
    )
    assert await asyncio.to_thread(second_started.wait, 1)
    assert events[:3] == [
        ("start", 1, "same-key", "secret-v1"),
        ("stop", 1),
        ("start", 2, "same-key", "secret-v2"),
    ]
    assert manager._runtimes[agent_id].generation == 2

    await manager.stop_client(agent_id, verify_persisted=False)
    assert events[-1] == ("stop", 2)
    assert agent_id not in manager._runtimes


@pytest.mark.asyncio
async def test_api_role_direct_start_and_stop_are_noops(monkeypatch):
    manager = DingTalkStreamManager()
    agent_id = uuid.uuid4()
    monkeypatch.setattr(dingtalk_stream, "_connector_role_enabled", lambda: False)

    await manager.start_client(agent_id, "app-key", "app-secret")
    await manager.stop_client(agent_id)

    assert manager._runtimes == {}


@pytest.mark.asyncio
async def test_direct_side_effects_wait_for_committed_desired_state(monkeypatch, stream_db):
    async with stream_db() as session:
        tenant = Tenant(id=uuid.uuid4(), name="Commit", slug=f"commit-{uuid.uuid4().hex[:8]}")
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            display_name="Commit owner",
            role="member",
            is_active=True,
        )
        session.add_all([tenant, user])
        await session.flush()
        agent = await _add_agent(session, tenant, user, name="Commit Agent")
        config = ChannelConfig(
            agent_id=agent.id,
            channel_type="dingtalk",
            app_id="committed-key",
            app_secret="committed-secret",
            is_configured=True,
            extra_config={"connection_mode": "websocket"},
        )
        session.add(config)
        await session.commit()

    manager = DingTalkStreamManager()
    runtime = _StreamRuntime(
        generation=1,
        fingerprint=dingtalk_credential_fingerprint(
            "committed-key",
            "committed-secret",
        ),
        app_key="committed-key",
        thread=threading.Thread(),
        stop_event=threading.Event(),
    )
    manager._runtimes[agent.id] = runtime
    monkeypatch.setattr(dingtalk_stream, "async_session", stream_db)
    monkeypatch.setattr(dingtalk_stream, "_connector_role_enabled", lambda: True)

    # A direct POST task carrying uncommitted replacement credentials must not
    # switch away from the committed runtime.
    await manager.start_client(agent.id, "uncommitted-key", "uncommitted-secret")
    assert manager._runtimes[agent.id] is runtime

    # A direct DELETE task scheduled before commit must not stop the committed
    # runtime. Once deletion commits, the same action is effective.
    await manager.stop_client(agent.id)
    assert manager._runtimes[agent.id] is runtime
    async with stream_db() as session:
        persisted = (
            await session.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent.id)
            )
        ).scalar_one()
        await session.delete(persisted)
        await session.commit()

    await manager.stop_client(agent.id)
    assert agent.id not in manager._runtimes


@pytest.mark.asyncio
async def test_managed_client_reconnects_and_marks_ready_only_after_socket_open(monkeypatch):
    manager = DingTalkStreamManager()
    stop_event = asyncio.Event()
    states = []
    open_attempts = 0

    class FakeWebSocket:
        async def close(self):
            return None

    class FakeConnection:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    def fake_connect(_uri):
        return FakeConnection()

    class FakeClient:
        websocket = None

        def __init__(self):
            self.pre_start_calls = 0
            self.open_calls = 0

        def pre_start(self):
            self.pre_start_calls += 1

        async def keepalive(self, websocket):
            await asyncio.Event().wait()

    async def fake_open(_client, _http_client):
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 1:
            raise OSError("temporary gateway failure")
        return {"endpoint": "wss://example.invalid/connect", "ticket": "a ticket"}

    async def fake_publish(agent_id, generation, *, ready):
        states.append(ready)
        if ready:
            stop_event.set()

    import websockets

    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(manager, "_open_connection", fake_open)
    monkeypatch.setattr(manager, "_publish_connection_state", fake_publish)
    client = FakeClient()

    await manager._run_managed_client(
        agent_id=uuid.uuid4(),
        generation=1,
        client=client,
        async_stop_event=stop_event,
        retry_delays=[0],
    )

    assert client.pre_start_calls == 1
    assert open_attempts == 2
    assert states == [False, True, False]


@pytest.mark.asyncio
async def test_gateway_open_is_cancelled_immediately_when_stop_is_requested(monkeypatch):
    manager = DingTalkStreamManager()
    stop_event = asyncio.Event()
    open_started = asyncio.Event()
    open_cancelled = asyncio.Event()

    async def blocked_open(_client, _http_client):
        open_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            open_cancelled.set()

    monkeypatch.setattr(manager, "_open_connection", blocked_open)
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            manager._open_connection_or_stop(object(), client, stop_event)
        )
        await open_started.wait()
        stop_event.set()
        result = await asyncio.wait_for(task, timeout=1)

    assert result is None
    assert open_cancelled.is_set()


@pytest.mark.asyncio
async def test_first_false_state_clears_stale_persisted_connected(monkeypatch, stream_db):
    async with stream_db() as session:
        tenant = Tenant(id=uuid.uuid4(), name="State", slug=f"state-{uuid.uuid4().hex[:8]}")
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            display_name="State owner",
            role="member",
            is_active=True,
        )
        session.add_all([tenant, user])
        await session.flush()
        agent = await _add_agent(session, tenant, user, name="State Agent")
        session.add(
            ChannelConfig(
                agent_id=agent.id,
                channel_type="dingtalk",
                app_id="state-key",
                app_secret="state-secret",
                is_configured=True,
                is_connected=True,
                extra_config={"connection_mode": "websocket"},
            )
        )
        await session.commit()

    manager = DingTalkStreamManager()
    runtime = _StreamRuntime(
        generation=1,
        fingerprint=dingtalk_credential_fingerprint("state-key", "state-secret"),
        app_key="state-key",
        thread=threading.Thread(),
        stop_event=threading.Event(),
    )
    manager._runtimes[agent.id] = runtime
    monkeypatch.setattr(dingtalk_stream, "async_session", stream_db)

    await manager._set_connection_state(agent.id, 1, ready=False)

    async with stream_db() as session:
        config = (
            await session.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent.id)
            )
        ).scalar_one()
        assert config.is_connected is False
    assert runtime.ready is False
    assert runtime.persisted_ready is False


@pytest.mark.asyncio
async def test_connection_state_db_failure_does_not_escape_into_runner(monkeypatch):
    manager = DingTalkStreamManager()
    agent_id = uuid.uuid4()
    runtime = _StreamRuntime(
        generation=1,
        fingerprint="fingerprint",
        app_key="app-key",
        thread=threading.Thread(),
        stop_event=threading.Event(),
    )
    manager._runtimes[agent_id] = runtime

    class FailingSession:
        async def execute(self, _statement):
            raise RuntimeError("database unavailable")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(dingtalk_stream, "async_session", lambda: FailingSession())

    await manager._set_connection_state(agent_id, 1, ready=True)

    assert runtime.ready is True
    assert runtime.persisted_ready is None


@pytest.mark.asyncio
async def test_gateway_adapter_matches_pinned_sdk_contract():
    import dingtalk_stream as sdk

    manager = DingTalkStreamManager()
    client = sdk.DingTalkStreamClient(
        credential=sdk.Credential(
            client_id="contract-key",
            client_secret="contract-secret",
        )
    )
    captured = {}

    async def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"endpoint": "wss://example.invalid", "ticket": "ticket"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=1) as http_client:
        result = await manager._open_connection(client, http_client)

    assert result["ticket"] == "ticket"
    assert captured["clientId"] == "contract-key"
    assert captured["clientSecret"] == "contract-secret"
    assert isinstance(captured["subscriptions"], list)


def test_token_cache_key_changes_when_only_secret_changes():
    manager = DingTalkTokenManager()

    first = manager._cache_key("same-app-key", "secret-v1")
    second = manager._cache_key("same-app-key", "secret-v2")

    assert first != second
    assert "secret-v1" not in first
    assert "secret-v2" not in second
