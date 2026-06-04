import uuid
import pytest
import httpx
from sqlalchemy import select
from app.database import async_session, engine
from app.main import app
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.user import User, Identity

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
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
