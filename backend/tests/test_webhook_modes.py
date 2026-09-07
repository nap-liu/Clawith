import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import httpx
from sqlalchemy import select
from app.api import webhooks as webhooks_api
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User, Identity
from app.services.trigger_daemon import (
    _finalize_invocation_executions,
    _link_invocation_executions,
    _evaluate_trigger,
    _merge_webhook_payloads,
)
from app.services.trigger_runtime.dispatch import enqueue_due_trigger
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.webhook_inbox import format_webhook_inbox_context

pytestmark = pytest.mark.asyncio


class _FakeRedisZSetPipeline:
    """In-memory stand-in for the redis pipeline used by webhook rate limiting.

    Mirrors the zremrangebyscore/zadd/zcard/expire sequence in
    ``webhooks._record_and_count_hits`` and returns the four results from
    ``execute()`` in order, so the rolling-60s hit count stays deterministic.
    """

    def __init__(self, parent):
        self._parent = parent
        self._results = []

    def zremrangebyscore(self, key, low, high):
        z = self._parent._data.setdefault(key, {})
        removed = [m for m, s in z.items() if low <= s <= high]
        for m in removed:
            z.pop(m, None)
        self._results.append(len(removed))

    def zadd(self, key, mapping):
        z = self._parent._data.setdefault(key, {})
        z.update(mapping)
        self._results.append(len(mapping))

    def zcard(self, key):
        self._results.append(len(self._parent._data.get(key, {})))

    def expire(self, key, ttl):
        self._results.append(True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self):
        return list(self._results)


class _FakeRedis:
    """Minimal in-memory redis exposing only ``pipeline`` for rate limiting.

    A fresh instance is bound per test (function-scoped event loop), avoiding
    the module-cached real client that otherwise leaks across loops and raises
    ``RuntimeError: Event loop is closed``.
    """

    def __init__(self):
        self._data = {}

    def pipeline(self, transaction=True):
        return _FakeRedisZSetPipeline(self)


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch):
    await engine.dispose()
    fake_redis = _FakeRedis()

    async def _fake_get_redis():
        return fake_redis

    # receive_webhook's rate limiter calls get_redis() (imported into the
    # webhooks module). Patch it to a per-test in-memory fake so no real redis
    # client is cached against a dying event loop.
    monkeypatch.setattr(webhooks_api, "get_redis", _fake_get_redis)
    yield
    await engine.dispose()


async def test_agent_has_webhook_queue_max_default_1000():
    async with async_session() as db:
        tenant = Tenant(name="T", slug=f"t_{uuid.uuid4().hex[:6]}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant.id)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native", tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        assert agent.webhook_queue_max == 1000


# ── Helpers for mode-dispatch tests ─────────────────────────────────────────


async def _make_agent_with_hook(mode: str, queue_max: int = 1000):
    """Create an isolated agent + webhook trigger; returns (agent_id, token)."""
    token = f"tk_{uuid.uuid4().hex}"  # globally unique — no cross-test collisions
    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native", webhook_queue_max=queue_max)
        db.add(agent)
        await db.flush()
        cfg = {"token": token}
        if mode != "legacy":
            cfg["webhook_mode"] = mode
        db.add(AgentTrigger(agent_id=agent.id, type="webhook", name="h", config=cfg, reason="r", is_enabled=True))
        await db.commit()
        return agent.id, token


async def _post(token, body):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(f"/api/webhooks/t/{token}", json=body)


async def _post_raw(token, body: bytes):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(
            f"/api/webhooks/t/{token}",
            content=body,
            headers={"content-type": "application/json"},
        )


async def _read_event_payload(agent_id, event_ref):
    key = normalize_storage_key(f"{agent_id}/{event_ref['path']}")
    return await get_storage_backend().read_bytes(key)


async def _trigger_cfg(agent_id):
    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))).scalar_one()
        return t.config


async def _make_persisted_webhook_trigger(mode, queue, *, batch_size=None):
    """Persist an agent + queue/merge webhook trigger (active lock held)."""
    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.flush()
        cfg = {
            "token": "x",
            "webhook_mode": mode,
            "_webhook_queue": list(queue),
            "_webhook_active": True,
            "_webhook_active_since": datetime.now(timezone.utc).isoformat(),
        }
        if batch_size is not None:
            cfg["_webhook_batch_size"] = batch_size
        trig = AgentTrigger(agent_id=agent.id, type="webhook", name="h", config=cfg, reason="r", is_enabled=True)
        db.add(trig)
        await db.commit()
        await db.refresh(trig)
        return agent.id, trig.id


# ── Mode-dispatch tests ──────────────────────────────────────────────────────


async def test_legacy_overwrites():
    aid, token = await _make_agent_with_hook("legacy")
    response = await _post(token, {"n": 1})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    first = (await _trigger_cfg(aid))["_webhook_event"]
    assert first["event_id"] > 0
    response = await _post(token, {"n": 2})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cfg = await _trigger_cfg(aid)
    assert cfg.get("_webhook_pending") is True
    second = cfg["_webhook_event"]
    assert second["event_id"] > first["event_id"]
    assert json.loads(await _read_event_payload(aid, first)) == {"n": 1}
    assert json.loads(await _read_event_payload(aid, second)) == {"n": 2}
    assert cfg["_webhook_payload"] is None
    assert "_webhook_queue" not in cfg


async def test_queue_accumulates():
    aid, token = await _make_agent_with_hook("queue")
    for i in range(3):
        assert (await _post(token, {"n": i})).status_code == 200
    cfg = await _trigger_cfg(aid)
    assert len(cfg["_webhook_queue"]) == 3
    event_ids = [item["event_id"] for item in cfg["_webhook_queue"]]
    assert event_ids == sorted(event_ids)
    assert len(set(event_ids)) == 3
    for expected, event_ref in enumerate(cfg["_webhook_queue"]):
        assert json.loads(await _read_event_payload(aid, event_ref)) == {"n": expected}
    assert "_webhook_pending" not in cfg


async def test_queue_preserves_payload_beyond_legacy_text_limits():
    aid, token = await _make_agent_with_hook("queue")
    body = json.dumps(
        {"prefix": "start", "content": "中" * 12_000, "tail": "END-OF-PAYLOAD"},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    response = await _post_raw(token, body)

    assert response.status_code == 200
    event_ref = (await _trigger_cfg(aid))["_webhook_queue"][0]
    assert event_ref["size"] == len(body)
    assert len(str(event_ref["received_at_ms"])) == 13
    assert f"{event_ref['received_at_ms']}_{event_ref['event_id']:020d}" in event_ref["path"]
    assert await _read_event_payload(aid, event_ref) == body


async def test_streamed_webhook_preserves_hmac_verification():
    aid, token = await _make_agent_with_hook("queue")
    secret = "test-signing-secret"
    async with async_session() as db:
        trigger = (
            await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        ).scalar_one()
        trigger.config = {**trigger.config, "secret": secret}
        await db.commit()

    body = b'{"event":"signed","tail":"complete"}'
    valid_signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(
            f"/api/webhooks/t/{token}",
            content=body,
            headers={"content-type": "application/json", "x-hub-signature-256": "sha256=bad"},
        )
        valid = await client.post(
            f"/api/webhooks/t/{token}",
            content=body,
            headers={"content-type": "application/json", "x-hub-signature-256": valid_signature},
        )

    assert invalid.status_code == 200
    assert valid.status_code == 200
    queue = (await _trigger_cfg(aid))["_webhook_queue"]
    assert len(queue) == 1
    assert await _read_event_payload(aid, queue[0]) == body


async def test_streamed_webhook_rejects_oversize_body_without_partial_event(monkeypatch):
    aid, token = await _make_agent_with_hook("queue")
    monkeypatch.setattr(webhooks_api, "MAX_PAYLOAD_SIZE", 10)

    response = await _post_raw(token, b'{"long":true}')

    assert response.status_code == 413
    assert (await _trigger_cfg(aid)).get("_webhook_queue") in (None, [])


async def test_queue_backpressure_when_full():
    aid, token = await _make_agent_with_hook("queue", queue_max=2)
    assert (await _post(token, {"n": 1})).status_code == 200
    assert (await _post(token, {"n": 2})).status_code == 200
    assert (await _post(token, {"n": 3})).status_code == 503
    cfg = await _trigger_cfg(aid)
    assert len(cfg["_webhook_queue"]) == 2


async def test_merge_accumulates():
    aid, token = await _make_agent_with_hook("merge")
    for i in range(4):
        assert (await _post(token, {"n": i})).status_code == 200
    cfg = await _trigger_cfg(aid)
    assert len(cfg["_webhook_queue"]) == 4


# ── Daemon evaluation tests (queue/merge serial lock + cooldown bypass) ───────


def _mk_trigger(mode, *, queue=None, active=False, pending=False, last_fired=None, cooldown=60, active_since=None):
    cfg = {"token": "x"}
    if mode != "legacy":
        cfg["webhook_mode"] = mode
    if queue is not None:
        cfg["_webhook_queue"] = queue
    if active:
        cfg["_webhook_active"] = True
        cfg["_webhook_active_since"] = (active_since or datetime.now(timezone.utc)).isoformat()
    if pending:
        cfg["_webhook_pending"] = True
    t = AgentTrigger(agent_id=uuid.uuid4(), type="webhook", name="h", config=cfg, reason="r",
                     is_enabled=True, cooldown_seconds=cooldown, fire_count=0)
    t.last_fired_at = last_fired
    return t


async def test_queue_fires_when_queue_nonempty_and_not_active():
    now = datetime.now(timezone.utc)
    assert await _evaluate_trigger(_mk_trigger("queue", queue=["a", "b"], active=False), now) is True


async def test_queue_skips_when_active():
    now = datetime.now(timezone.utc)
    assert await _evaluate_trigger(_mk_trigger("queue", queue=["a"], active=True), now) is False


async def test_queue_empty_does_not_fire():
    now = datetime.now(timezone.utc)
    assert await _evaluate_trigger(_mk_trigger("queue", queue=[], active=False), now) is False


async def test_queue_bypasses_cooldown():
    now = datetime.now(timezone.utc)
    # 刚 fire 过(cooldown 内), queue 模式应绕过
    t = _mk_trigger("queue", queue=["a"], active=False, last_fired=now - timedelta(seconds=5), cooldown=60)
    assert await _evaluate_trigger(t, now) is True


async def test_queue_lock_timeout_forces_refire():
    now = datetime.now(timezone.utc)
    # active 但持锁 > 10min → 强制重处理
    t = _mk_trigger("queue", queue=["a"], active=True, active_since=now - timedelta(minutes=11))
    assert await _evaluate_trigger(t, now) is True


async def test_merge_fires_when_queue_nonempty():
    now = datetime.now(timezone.utc)
    assert await _evaluate_trigger(_mk_trigger("merge", queue=["a", "b", "c"], active=False), now) is True


async def test_merge_due_claim_is_atomic_and_enqueues_one_execution():
    """Repeated daemon ticks must not enqueue one queued payload twice."""
    _agent_id, trigger_id = await _make_persisted_webhook_trigger("merge", ["a"])
    async with async_session() as db:
        trigger = (
            await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
        ).scalar_one()
        trigger.config = {
            **trigger.config,
            "_webhook_active": False,
            "_webhook_active_since": None,
        }
        await db.commit()
        await db.refresh(trigger)
        db.expunge(trigger)

    now = datetime.now(timezone.utc)
    await enqueue_due_trigger(trigger, now)
    # Simulate the next daemon tick reusing an old detached snapshot while the
    # first LLM turn is still active.  The DB row lock/fresh state is decisive.
    await enqueue_due_trigger(trigger, now + timedelta(seconds=15))

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        executions = (
            await db.execute(
                select(TriggerExecution).where(TriggerExecution.trigger_id == trigger_id)
            )
        ).scalars().all()
        assert stored.config["_webhook_active"] is True
        assert stored.config["_webhook_batch_size"] == 1
        assert len(executions) == 1
        assert executions[0].payload["_webhook_batch"] == ["a"]


async def test_stale_webhook_lock_with_unfinished_execution_does_not_duplicate():
    """The 10-minute recovery path defers to the durable execution lease."""
    _agent_id, trigger_id = await _make_persisted_webhook_trigger("queue", ["a"])
    async with async_session() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        stale = datetime.now(timezone.utc) - timedelta(minutes=11)
        trigger.config = {
            **trigger.config,
            "_webhook_active": True,
            "_webhook_active_since": stale.isoformat(),
        }
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=trigger.agent_id,
            source="webhook",
            status="processing",
            idempotency_key=f"existing:{uuid.uuid4()}",
            payload={},
            payload_text="",
            scheduled_at=stale,
            started_at=stale,
        )
        db.add(execution)
        await db.commit()
        await db.refresh(trigger)
        db.expunge(trigger)

    await enqueue_due_trigger(trigger, datetime.now(timezone.utc))

    async with async_session() as db:
        count = len(
            (
                await db.execute(
                    select(TriggerExecution).where(TriggerExecution.trigger_id == trigger_id)
                )
            ).scalars().all()
        )
        assert count == 1


async def test_ingress_and_claim_share_one_row_lock_without_losing_active_state():
    """A concurrent append cannot clear the daemon's active batch lock."""
    agent_id, token = await _make_agent_with_hook("merge")
    assert (await _post(token, {"n": 1})).status_code == 200
    async with async_session() as db:
        trigger = (
            await db.execute(
                select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
            )
        ).scalar_one()
        trigger_id = trigger.id
        db.expunge(trigger)

    response, _ = await asyncio.gather(
        _post(token, {"n": 2}),
        enqueue_due_trigger(trigger, datetime.now(timezone.utc)),
    )
    assert response.status_code == 200

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        executions = (
            await db.execute(
                select(TriggerExecution).where(TriggerExecution.trigger_id == trigger_id)
            )
        ).scalars().all()
        assert stored.config["_webhook_active"] is True
        assert stored.config["_webhook_batch_size"] in {1, 2}
        assert len(stored.config["_webhook_queue"]) == 2
        assert len(executions) == 1
        batch_size = stored.config["_webhook_batch_size"]
        assert executions[0].payload["_webhook_batch"] == stored.config["_webhook_queue"][:batch_size]


async def test_ingress_and_advance_preserve_late_payload_and_finalize_atomically():
    """A payload arriving during advance survives as the next merge batch."""
    agent_id, token = await _make_agent_with_hook("merge")
    assert (await _post(token, {"n": 1})).status_code == 200
    async with async_session() as db:
        trigger = (
            await db.execute(
                select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
            )
        ).scalar_one()
        trigger_id = trigger.id
        db.expunge(trigger)

    await enqueue_due_trigger(trigger, datetime.now(timezone.utc))
    async with async_session() as db:
        execution = (
            await db.execute(
                select(TriggerExecution).where(TriggerExecution.trigger_id == trigger_id)
            )
        ).scalar_one()
        execution_id = execution.id
        runtime_trigger = await db.get(AgentTrigger, trigger_id)
        db.expunge(runtime_trigger)

    response, _ = await asyncio.gather(
        _post(token, {"n": 2}),
        _finalize_invocation_executions(
            [execution_id],
            [runtime_trigger],
            "ok",
            None,
            False,
            None,
        ),
    )
    assert response.status_code == 200

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        execution = await db.get(TriggerExecution, execution_id)
        assert len(stored.config["_webhook_queue"]) == 1
        remaining = stored.config["_webhook_queue"][0]
        assert json.loads(await _read_event_payload(agent_id, remaining)) == {"n": 2}
        assert stored.config["_webhook_active"] is False
        assert execution.status == "completed"


async def test_merge_burst_and_parallel_ticks_create_one_complete_batch():
    """A burst plus many stale daemon snapshots still produces one execution."""
    agent_id, token = await _make_agent_with_hook("merge")
    responses = await asyncio.gather(*[_post(token, {"n": i}) for i in range(30)])
    assert all(response.status_code == 200 for response in responses)

    async with async_session() as db:
        trigger = (
            await db.execute(
                select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
            )
        ).scalar_one()
        trigger_id = trigger.id
        db.expunge(trigger)

    now = datetime.now(timezone.utc)
    await asyncio.gather(
        *[enqueue_due_trigger(trigger, now + timedelta(milliseconds=i)) for i in range(20)]
    )

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        executions = (
            await db.execute(
                select(TriggerExecution).where(TriggerExecution.trigger_id == trigger_id)
            )
        ).scalars().all()
        assert stored.config["_webhook_batch_size"] == 30
        assert len(executions) == 1
        execution_id = executions[0].id
        db.expunge(stored)

    await _finalize_invocation_executions(
        [execution_id],
        [stored],
        "ok",
        None,
        False,
        None,
    )

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        execution = await db.get(TriggerExecution, execution_id)
        assert stored.config["_webhook_queue"] == []
        assert stored.config["_webhook_active"] is False
        assert execution.status == "completed"


async def test_early_skip_advances_webhook_and_completes_execution():
    """Agent/model early-return semantics consume the batch exactly once."""
    _agent_id, trigger_id = await _make_persisted_webhook_trigger("queue", ["a"])
    async with async_session() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=trigger.agent_id,
            source="webhook",
            status="processing",
            idempotency_key=f"early:{uuid.uuid4()}",
            payload={},
            payload_text="",
        )
        db.add(execution)
        await db.commit()
        execution_id = execution.id
        db.expunge(trigger)

    await _finalize_invocation_executions(
        [execution_id],
        [trigger],
        None,
        None,
        False,
        None,
    )

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        execution = await db.get(TriggerExecution, execution_id)
        assert stored.config["_webhook_queue"] == []
        assert stored.config["_webhook_active"] is False
        assert execution.status == "completed"


async def test_finalize_stale_conversation_still_reaches_terminal_state():
    """A deleted conversation must not strand a claimed execution forever."""
    _agent_id, trigger_id = await _make_persisted_webhook_trigger("queue", ["a"])
    async with async_session() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=trigger.agent_id,
            source="webhook",
            status="processing",
            idempotency_key=f"stale-conversation:{uuid.uuid4()}",
            payload={},
            payload_text="",
        )
        db.add(execution)
        await db.commit()
        execution_id = execution.id
        db.expunge(trigger)

    await _finalize_invocation_executions(
        [execution_id],
        [trigger],
        None,
        "origin conversation disappeared",
        False,
        uuid.uuid4(),
    )

    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        assert execution.status == "failed"
        assert execution.conversation_id is None
        assert execution.lease_owner is None
        assert execution.lease_expires_at is None


async def test_link_execution_validates_origin_and_agent_ownership():
    """Only a durable conversation owned by the execution's agent may be linked."""
    agent_id, trigger_id = await _make_persisted_webhook_trigger("queue", ["a"])
    async with async_session() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        agent = await db.get(Agent, agent_id)
        owner = await db.get(User, agent.creator_id)
        tenant = Tenant(name=f"origin-{uuid.uuid4().hex[:8]}", slug=f"origin-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        owner.tenant_id = tenant.id
        agent.tenant_id = tenant.id
        await db.flush()
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent_id,
            source="on_message",
            status="processing",
            idempotency_key=f"origin-link:{uuid.uuid4()}",
            payload={},
            payload_text="",
        )
        session = ChatSession(
            agent_id=agent_id,
            user_id=agent.creator_id,
            source_channel="web",
            title="origin",
        )
        db.add_all([execution, session])
        await db.commit()
        execution_id = execution.id
        session_id = session.id

    with pytest.raises(RuntimeError, match="no longer exists"):
        await _link_invocation_executions([execution_id], uuid.uuid4())

    await _link_invocation_executions([execution_id], session_id)
    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        assert execution.conversation_id == session_id


async def test_advance_failure_rolls_back_execution_terminal_state(monkeypatch):
    """Advance and execution completion succeed or roll back together."""
    from app.services import trigger_daemon as daemon

    _agent_id, trigger_id = await _make_persisted_webhook_trigger("queue", ["a"])
    async with async_session() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=trigger.agent_id,
            source="webhook",
            status="processing",
            idempotency_key=f"rollback:{uuid.uuid4()}",
            payload={},
            payload_text="",
        )
        db.add(execution)
        await db.commit()
        execution_id = execution.id
        db.expunge(trigger)

    original_advance = daemon._advance_webhook_trigger

    def fail_after_advance(db, stored_trigger, reply):
        original_advance(db, stored_trigger, reply)
        raise RuntimeError("forced advance failure")

    monkeypatch.setattr(daemon, "_advance_webhook_trigger", fail_after_advance)
    with pytest.raises(RuntimeError, match="forced advance failure"):
        await _finalize_invocation_executions(
            [execution_id],
            [trigger],
            "ok",
            None,
            False,
            None,
        )

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger_id)
        execution = await db.get(TriggerExecution, execution_id)
        assert stored.config["_webhook_queue"] == ["a"]
        assert stored.config["_webhook_active"] is True
        assert execution.status == "processing"


async def test_legacy_still_respects_cooldown():
    now = datetime.now(timezone.utc)
    # legacy + pending=True 但在 cooldown 内 → 不 fire(现状不变)
    t = _mk_trigger("legacy", pending=True, last_fired=now - timedelta(seconds=5), cooldown=60)
    assert await _evaluate_trigger(t, now) is False


async def test_legacy_fires_after_cooldown():
    now = datetime.now(timezone.utc)
    t = _mk_trigger("legacy", pending=True, last_fired=now - timedelta(seconds=120), cooldown=60)
    assert await _evaluate_trigger(t, now) is True


# ── Fire path: merge wake-context format + queue/merge advance ────────────────


async def test_merge_wake_context_format():
    """merge join is numbered, and the (merged, N entries) header counts entries."""
    merged = _merge_webhook_payloads(["p1", "p2"])
    assert merged == "--- [1] ---\np1\n--- [2] ---\np2"
    # The header the wake-context builder wraps it with:
    header = f"Webhook Payload (merged, {len(['p1', 'p2'])} entries):\n{merged}"
    assert "(merged, 2 entries)" in header


async def test_webhook_inbox_context_contains_references_not_payload_bytes():
    refs = [
        {
            "kind": "webhook_inbox_event_v1",
            "event_id": 21,
            "received_at_ms": 1787635812345,
            "event_key": "1787635812345_00000000000000000021",
            "path": "webhook/t/20260825/21/payload.json",
            "size": 18001,
            "sha256": "a" * 64,
            "content_type": "application/json",
        },
        {
            "kind": "webhook_inbox_event_v1",
            "event_id": 22,
            "received_at_ms": 1787635812346,
            "event_key": "1787635812346_00000000000000000022",
            "path": "webhook/t/20260825/22/payload.json",
            "size": 19002,
            "sha256": "b" * 64,
            "content_type": "application/json",
        },
    ]

    context = format_webhook_inbox_context({"_webhook_batch": refs})

    assert "Event ID: 21" in context
    assert "Event ID: 22" in context
    assert refs[0]["path"] in context
    assert refs[1]["path"] in context
    assert "read_file" in context
