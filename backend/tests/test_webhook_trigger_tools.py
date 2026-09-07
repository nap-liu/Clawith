"""Persisted webhook advancement and trigger-tool tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.tool import AgentTool, Tool
from app.models.trigger import AgentTrigger
from app.models.user import Identity, User
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import seed_builtin_tools
from app.services.trigger_daemon import _advance_webhook_trigger, _merge_webhook_payloads
from tests.test_webhook_modes import _isolate, _make_persisted_webhook_trigger  # noqa: F401 - autouse fixture

pytestmark = pytest.mark.asyncio


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
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.flush()
        # late arrival 'd' appended after lock → queue now [a,b,c,d], batch_size still 3
        trig = AgentTrigger(
            agent_id=agent.id, type="webhook", name="h",
            config={"token": "x", "webhook_mode": "merge",
                    "_webhook_queue": ["a", "b", "c", "d"],
                    "_webhook_batch_size": 3, "_webhook_active": True},
            reason="r", is_enabled=True,
        )
        db.add(trig)
        await db.commit()
        _advance_webhook_trigger(db, trig, reply="ok")  # sync helper, mutates in place
        await db.commit()
        await db.refresh(trig)
        assert trig.config["_webhook_queue"] == ["d"]  # exactly the 3 rendered were deleted; 'd' survives
        assert trig.config.get("_webhook_active") in (False, None)


async def test_advance_noop_for_legacy_mode():
    """Defensive: advance must not touch a legacy trigger if ever passed one."""
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
        trig = AgentTrigger(
            agent_id=agent.id, type="webhook", name="h",
            config={"token": "x", "_webhook_pending": True, "_webhook_payload": "p"},
            reason="r", is_enabled=True,
        )
        db.add(trig)
        await db.commit()
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


# --- C: update_trigger can switch an existing hook's mode without clobbering token/queue ---


async def _make_webhook_agent(cfg):
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
        db.add(AgentTrigger(agent_id=agent.id, type="webhook", name="h", config=cfg, reason="r", is_enabled=True))
        await db.commit()
        return agent.id


async def test_webhook_guidance_reaches_seeded_llm_runtime():
    aid = await _make_webhook_agent({"token": "runtime-guidance", "webhook_mode": "queue"})
    await seed_builtin_tools()

    expected_names = {"set_trigger", "update_trigger"}
    async with async_session() as db:
        tool_rows = (
            await db.execute(select(Tool).where(Tool.name.in_(expected_names)))
        ).scalars().all()
        assert {tool.name for tool in tool_rows} == expected_names
        assignments = {
            assignment.tool_id: assignment
            for assignment in (
                await db.execute(select(AgentTool).where(AgentTool.agent_id == aid))
            ).scalars().all()
        }
        for tool in tool_rows:
            assignment = assignments.get(tool.id)
            if assignment is None:
                db.add(AgentTool(agent_id=aid, tool_id=tool.id, enabled=True))
            else:
                assignment.enabled = True
        await db.commit()

    runtime_by_name = {
        tool["function"]["name"]: tool["function"]
        for tool in await get_agent_tools_for_llm(aid)
        if tool["function"]["name"] in expected_names
    }
    assert set(runtime_by_name) == expected_names
    for tool in tool_rows:
        runtime = runtime_by_name[tool.name]
        assert runtime["parameters"] == tool.parameters_schema
        mode = runtime["parameters"]["properties"]["webhook_mode"]
        assert mode["type"] == "string"
        assert mode["enum"] == ["legacy", "queue", "merge"]

    assert "webhook" in runtime_by_name["set_trigger"]["parameters"]["properties"]["type"]["enum"]
    assert "webhook_mode" in runtime_by_name["set_trigger"]["description"]
    set_mode_description = runtime_by_name["set_trigger"]["parameters"]["properties"][
        "webhook_mode"
    ]["description"]
    update_properties = runtime_by_name["update_trigger"]["parameters"]["properties"]
    assert "FIFO" in set_mode_description and "once per event" in set_mode_description
    assert "batch captured when execution starts" in set_mode_description
    assert "stored byte-for-byte" in set_mode_description
    assert "read the referenced file" in set_mode_description
    assert "does not convert or drain in-flight work" in update_properties["webhook_mode"][
        "description"
    ]
    assert "partial patch" in update_properties["config"]["description"]


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


async def test_update_trigger_partial_config_preserves_webhook_identity_and_mode():
    from app.services.agent_tools import _handle_update_trigger

    aid = await _make_webhook_agent(
        {
            "token": "tok123",
            "secret": "old",
            "webhook_mode": "queue",
            "_webhook_queue": [{"event_id": 7}],
        }
    )

    result = await _handle_update_trigger(
        aid,
        {"name": "h", "config": {"secret": "new"}},
    )

    assert "✅" in result
    async with async_session() as db:
        trigger = (
            await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        ).scalar_one()
        assert trigger.config["token"] == "tok123"
        assert trigger.config["secret"] == "new"
        assert trigger.config["webhook_mode"] == "queue"
        assert trigger.config["_webhook_queue"] == [{"event_id": 7}]


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
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.flush()
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
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="A", role_description="", creator_id=user.id, agent_type="native")
        db.add(agent)
        await db.commit()
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
