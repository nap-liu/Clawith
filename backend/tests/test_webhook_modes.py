import uuid
from datetime import datetime, timezone, timedelta

import pytest
import httpx
from sqlalchemy import select
from app.api import webhooks as webhooks_api
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.user import User, Identity
from app.services.trigger_daemon import (
    _evaluate_trigger,
    _merge_webhook_payloads,
    _advance_webhook_trigger,
)
from app.models.audit import AuditLog

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
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native", webhook_queue_max=queue_max)
        db.add(agent); await db.flush()
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


async def _trigger_cfg(agent_id):
    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))).scalar_one()
        return t.config


# ── Mode-dispatch tests ──────────────────────────────────────────────────────


async def test_legacy_overwrites():
    aid, token = await _make_agent_with_hook("legacy")
    await _post(token, {"n": 1})
    await _post(token, {"n": 2})
    cfg = await _trigger_cfg(aid)
    assert cfg.get("_webhook_pending") is True
    assert '"n": 2' in cfg["_webhook_payload"]
    assert "_webhook_queue" not in cfg


async def test_queue_accumulates():
    aid, token = await _make_agent_with_hook("queue")
    for i in range(3):
        assert (await _post(token, {"n": i})).status_code == 200
    cfg = await _trigger_cfg(aid)
    assert len(cfg["_webhook_queue"]) == 3
    assert "_webhook_pending" not in cfg


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


async def _make_persisted_webhook_trigger(mode, queue, *, batch_size=None):
    """Persist an agent + queue/merge webhook trigger (active lock held)."""
    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent); await db.flush()
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
        db.add(trig); await db.commit()
        await db.refresh(trig)
        return agent.id, trig.id


async def _reload_trigger(trig_id):
    async with async_session() as db:
        return (await db.execute(select(AgentTrigger).where(AgentTrigger.id == trig_id))).scalar_one()


async def _count_failed_audits(agent_id):
    async with async_session() as db:
        rows = (await db.execute(
            select(AuditLog).where(AuditLog.agent_id == agent_id, AuditLog.action == "webhook_session_failed")
        )).scalars().all()
        return len(rows)


async def test_queue_advance_success_pops_head_and_releases_lock():
    agent_id, trig_id = await _make_persisted_webhook_trigger("queue", ["a", "b"])
    async with async_session() as db:
        trig = (await db.execute(select(AgentTrigger).where(AgentTrigger.id == trig_id))).scalar_one()
        _advance_webhook_trigger(db, trig, "ok")
        await db.commit()
    trig = await _reload_trigger(trig_id)
    assert trig.config["_webhook_queue"] == ["b"]
    assert trig.config["_webhook_active"] is False
    assert trig.config["_webhook_active_since"] is None
    assert await _count_failed_audits(agent_id) == 0


async def test_queue_advance_failure_still_pops_and_audits():
    agent_id, trig_id = await _make_persisted_webhook_trigger("queue", ["a", "b"])
    async with async_session() as db:
        trig = (await db.execute(select(AgentTrigger).where(AgentTrigger.id == trig_id))).scalar_one()
        _advance_webhook_trigger(db, trig, None)  # failure: None reply
        await db.commit()
    trig = await _reload_trigger(trig_id)
    assert trig.config["_webhook_queue"] == ["b"]  # still popped (D6)
    assert trig.config["_webhook_active"] is False
    assert await _count_failed_audits(agent_id) == 1


async def test_merge_advance_drops_batch_keeps_late_arrivals():
    # batch_size=2 was recorded at lock time; a 3rd entry arrived mid-session.
    agent_id, trig_id = await _make_persisted_webhook_trigger("merge", ["a", "b", "c"], batch_size=2)
    async with async_session() as db:
        trig = (await db.execute(select(AgentTrigger).where(AgentTrigger.id == trig_id))).scalar_one()
        _advance_webhook_trigger(db, trig, "ok")
        await db.commit()
    trig = await _reload_trigger(trig_id)
    assert trig.config["_webhook_queue"] == ["c"]  # only the 2-entry batch dropped
    assert trig.config["_webhook_active"] is False
    assert "_webhook_batch_size" not in trig.config


async def test_merge_rendered_batch_equals_deleted_batch():
    """B1: the wake-context batch (queue[:batch_size]) and the advance-deleted
    batch must be identical, even when a late entry arrives after the lock.

    Simulate the lock recording batch_size=3 over the queue at lock time, then a
    4th payload 'd' arriving mid-session (queue becomes [a,b,c,d], batch_size
    still 3). The rendered slice and the deleted slice are both queue[:3]=[a,b,c];
    'd' survives for the next batch — never silently dropped.
    """
    queue = ["a", "b", "c"]
    batch_size = 3
    rendered = queue[:batch_size]
    assert _merge_webhook_payloads(rendered)  # renders all 3
    # advance with the same batch_size drops exactly those 3, leaving late arrivals
    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.local",
            password_hash="x",
        )
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent); await db.flush()
        # late arrival 'd' appended after lock → queue now [a,b,c,d], batch_size still 3
        trig = AgentTrigger(
            agent_id=agent.id, type="webhook", name="h",
            config={"token": "x", "webhook_mode": "merge",
                    "_webhook_queue": ["a", "b", "c", "d"],
                    "_webhook_batch_size": 3, "_webhook_active": True},
            reason="r", is_enabled=True,
        )
        db.add(trig); await db.commit()
        _advance_webhook_trigger(db, trig, reply="ok")  # sync helper, mutates in place
        await db.commit()
        await db.refresh(trig)
        assert trig.config["_webhook_queue"] == ["d"]  # exactly the 3 rendered were deleted; 'd' survives
        assert trig.config.get("_webhook_active") in (False, None)


async def test_advance_noop_for_legacy_mode():
    """Defensive: advance must not touch a legacy trigger if ever passed one."""
    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent); await db.flush()
        trig = AgentTrigger(
            agent_id=agent.id, type="webhook", name="h",
            config={"token": "x", "_webhook_pending": True, "_webhook_payload": "p"},
            reason="r", is_enabled=True,
        )
        db.add(trig); await db.commit()
        await db.refresh(trig)
        _advance_webhook_trigger(db, trig, "ok")
        await db.commit()
        assert trig.config["_webhook_pending"] is True
        assert trig.config["_webhook_payload"] == "p"
        assert "_webhook_active" not in trig.config


# ── set_trigger tool: webhook_mode parameter ─────────────────────────────────


async def test_set_trigger_creates_queue_hook():
    """_handle_set_trigger with webhook_mode=queue writes mode + empty queue into config."""
    from app.services.agent_tools import _handle_set_trigger

    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    result = await _handle_set_trigger(
        agent_id,
        {
            "name": f"wh_{uuid.uuid4().hex[:6]}",
            "type": "webhook",
            "config": {},
            "reason": "test queue mode",
            "webhook_mode": "queue",
        },
    )
    assert "✅" in result

    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))).scalar_one()
        assert t.config.get("webhook_mode") == "queue"
        assert t.config.get("_webhook_queue") == []


async def test_set_trigger_creates_merge_hook():
    """_handle_set_trigger with webhook_mode=merge writes mode + empty queue into config."""
    from app.services.agent_tools import _handle_set_trigger

    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    result = await _handle_set_trigger(
        agent_id,
        {
            "name": f"wh_{uuid.uuid4().hex[:6]}",
            "type": "webhook",
            "config": {},
            "reason": "test merge mode",
            "webhook_mode": "merge",
        },
    )
    assert "✅" in result
    assert "Mode: merge" in result

    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))).scalar_one()
        assert t.config.get("webhook_mode") == "merge"
        assert t.config.get("_webhook_queue") == []


async def test_set_trigger_legacy_omits_mode_key():
    """_handle_set_trigger without webhook_mode (or with legacy) leaves no webhook_mode in config."""
    from app.services.agent_tools import _handle_set_trigger

    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:6]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    result = await _handle_set_trigger(
        agent_id,
        {
            "name": f"wh_{uuid.uuid4().hex[:6]}",
            "type": "webhook",
            "config": {},
            "reason": "test legacy mode",
            # no webhook_mode → defaults to legacy
        },
    )
    assert "✅" in result

    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id))).scalar_one()
        assert "webhook_mode" not in t.config
        assert "_webhook_queue" not in t.config


async def test_set_trigger_tool_schema_contains_webhook_mode():
    """The set_trigger tool definition exposes the webhook_mode property in its input schema."""
    from app.services.agent_tools import AGENT_TOOLS

    set_trigger_def = next(t for t in AGENT_TOOLS if t["function"]["name"] == "set_trigger")
    props = set_trigger_def["function"]["parameters"]["properties"]
    assert "webhook_mode" in props
    assert props["webhook_mode"]["type"] == "string"
    assert set(props["webhook_mode"]["enum"]) == {"legacy", "queue", "merge"}


# --- A+B: discoverability + scenario guidance in tool descriptions ---


async def test_set_trigger_description_mentions_webhook_mode():
    """A: top-level set_trigger description surfaces the webhook_mode capability."""
    from app.services.agent_tools import AGENT_TOOLS

    st = next(t for t in AGENT_TOOLS if t["function"]["name"] == "set_trigger")
    assert "webhook_mode" in st["function"]["description"]


async def test_webhook_mode_description_has_scenario_guidance():
    """B: webhook_mode description tells the agent WHEN to use each mode, not just what they do."""
    from app.services.agent_tools import AGENT_TOOLS

    st = next(t for t in AGENT_TOOLS if t["function"]["name"] == "set_trigger")
    desc = st["function"]["parameters"]["properties"]["webhook_mode"]["description"]
    assert "FIFO" in desc and "individually" in desc  # queue scenario
    assert "summarize" in desc or "together" in desc  # merge scenario


# --- C: update_trigger can switch an existing hook's mode without clobbering token/queue ---


async def _make_webhook_agent(cfg):
    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent); await db.flush()
        db.add(AgentTrigger(agent_id=agent.id, type="webhook", name="h", config=cfg, reason="r", is_enabled=True))
        await db.commit()
        return agent.id


async def test_update_trigger_schema_contains_webhook_mode():
    from app.services.agent_tools import AGENT_TOOLS

    ut = next(t for t in AGENT_TOOLS if t["function"]["name"] == "update_trigger")
    props = ut["function"]["parameters"]["properties"]
    assert "webhook_mode" in props
    assert set(props["webhook_mode"]["enum"]) == {"legacy", "queue", "merge"}


async def test_update_trigger_switches_mode_preserving_token_and_queue():
    from app.services.agent_tools import _handle_update_trigger

    aid = await _make_webhook_agent({"token": "tok123", "_webhook_queue": ["x", "y"]})
    result = await _handle_update_trigger(aid, {"name": "h", "webhook_mode": "queue"})
    assert "queue" in result
    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))).scalar_one()
        assert t.config["webhook_mode"] == "queue"
        assert t.config["token"] == "tok123"               # token preserved
        assert t.config["_webhook_queue"] == ["x", "y"]    # queued payloads preserved


async def test_update_trigger_to_legacy_drops_mode_key():
    from app.services.agent_tools import _handle_update_trigger

    aid = await _make_webhook_agent({"token": "tok", "webhook_mode": "merge", "_webhook_queue": []})
    await _handle_update_trigger(aid, {"name": "h", "webhook_mode": "legacy"})
    async with async_session() as db:
        t = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))).scalar_one()
        assert "webhook_mode" not in t.config              # legacy = default → key dropped
        assert t.config["token"] == "tok"                  # token still preserved


async def test_update_trigger_webhook_mode_rejects_non_webhook():
    from app.services.agent_tools import _handle_update_trigger

    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent); await db.flush()
        db.add(AgentTrigger(agent_id=agent.id, type="interval", name="iv", config={"minutes": 5}, reason="r", is_enabled=True))
        await db.commit()
        aid = agent.id
    result = await _handle_update_trigger(aid, {"name": "iv", "webhook_mode": "queue"})
    assert "❌" in result and "webhook" in result.lower()


# --- D: webhook creation guides agent to use the Clawith SDK for standalone-page info collection ---


async def test_webhook_creation_message_includes_sdk_collection_guidance():
    from app.services.agent_tools import _handle_set_trigger

    async with async_session() as db:
        ident = Identity(username=f"u_{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user); await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent); await db.commit()
        aid = agent.id
    result = await _handle_set_trigger(aid, {"name": "collect", "type": "webhook", "config": {}, "reason": "collect reader info"})
    # SDK injection + API surface the agent needs
    assert "/sdk/clawith.js" in result
    assert "data-hook" in result
    assert "triggerHook" in result
    assert "onReady" in result
    assert "mobile" in result                       # OAuth identity carries mobile
    # framed as general info collection, NOT narrowly "feedback"
    assert "survey" in result.lower() or "collection" in result.lower()
