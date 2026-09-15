"""Lossless trigger edits through tool and management API boundaries."""

import asyncio
import json

import pytest
from sqlalchemy import select
from fastapi import HTTPException

from app.database import async_session
from app.models.trigger import AgentTrigger
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_tools_trigger_management import (
    _handle_update_trigger,
    _handle_list_triggers,
    _handle_cancel_trigger,
)
from app.services.agent_tools_trigger_ops import _handle_set_trigger
from app.api.triggers import update_trigger, TriggerUpdate
from tests.test_webhook_trigger_tools import _make_webhook_agent
from tests.test_webhook_modes import _isolate  # noqa: F401

pytestmark = pytest.mark.asyncio


async def row(aid):
    async with async_session() as db:
        return (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))).scalar_one()


async def test_tool_patch_clear_full_read_and_atomic_failure():
    long_value = "完整内容" * 3000 + "END_SENTINEL"
    aid = await _make_webhook_agent(
        {
            "token": "stable",
            "secret": "remove",
            "headers": {"A": "a", "B": "b"},
            "payload": long_value,
            "_webhook_queue": ["pending"],
        }
    )
    result = await _handle_update_trigger(
        aid,
        {
            "name": "h",
            "config": {"headers": {"A": "changed"}, "nullable": None},
            "reason": long_value,
            "clear_fields": ["/config/secret", "/config/headers/B", "/max_fires"],
        },
    )
    data = json.loads(result)["trigger"]
    assert data["config"]["headers"] == {"A": "changed"}
    assert data["config"]["nullable"] is None and "secret" not in data["config"]
    assert data["reason"] == long_value and data["config"]["payload"] == long_value
    before = await row(aid)
    assert before.config["_webhook_queue"] == ["pending"]
    failed = await _handle_update_trigger(
        aid,
        {
            "name": "h",
            "reason": "must roll back",
            "config": {"headers": {"A": "x"}},
            "clear_fields": ["/config/headers"],
        },
    )
    assert "❌" in failed
    empty_conflict = await _handle_update_trigger(aid, {"name": "h", "config": {"payload": {}}, "clear_fields": ["/config/payload"]})
    assert "❌" in empty_conflict
    after = await row(aid)
    assert after.config == before.config and after.reason == before.reason
    listed = json.loads(await _handle_list_triggers(aid, {"id": str(after.id)}))
    assert listed["triggers"][0]["config"]["payload"] == long_value
    assert listed["triggers"][0]["reason"] == long_value
    assert "_webhook_queue" not in listed["triggers"][0]["config"]


async def test_concurrent_edits_and_reenable_preserve_history():
    aid = await _make_webhook_agent({"token": "stable", "headers": {"A": "a", "B": "b"}})
    async with async_session() as db:
        trigger = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))).scalar_one()
        trigger.fire_count = 7
        trigger.max_fires = 7
        trigger.soul = False
        await db.commit()
    replies = await asyncio.gather(
        *[_handle_update_trigger(aid, {"name": "h", "config": {"headers": {key: key * 2}}}) for key in ["A", "B"]]
    )
    assert all("✅" in reply for reply in replies)
    await _handle_cancel_trigger(aid, {"name": "h"})
    result = await _handle_set_trigger(aid, {"name": "h", "clear_fields": ["/max_fires"]})
    assert json.loads(result)["ok"]
    trigger = await row(aid)
    assert trigger.config["headers"] == {"A": "AA", "B": "BB"}
    assert trigger.fire_count == 7 and trigger.max_fires is None and trigger.soul is False
    assert trigger.is_enabled


async def test_api_uses_same_patch_and_preserves_private_fields():
    aid = await _make_webhook_agent(
        {"token": "stable", "secret": "private", "headers": {"A": "a", "B": "b"}, "_webhook_queue": ["pending"]}
    )
    trigger = await row(aid)
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        user = await db.get(User, agent.creator_id)
    result = await update_trigger(
        aid,
        trigger.id,
        TriggerUpdate(config={"headers": {"A": "new"}}, clear_fields=["/config/headers/B", "/expires_at"]),
        user,
    )
    assert result["ok"]
    assert result["trigger"]["config"]["headers"] == {"A": "new"}
    assert "token" not in result["trigger"]["config"]
    after = await row(aid)
    assert after.config["secret"] == "private" and after.config["_webhook_queue"] == ["pending"]
    with pytest.raises(HTTPException):
        await update_trigger(
            aid, trigger.id, TriggerUpdate(reason="must roll back", clear_fields=["/config/secret"]), user
        )
    assert (await row(aid)).reason == after.reason


async def test_on_message_actor_switch_clears_only_obsolete_correlation():
    from app.models.org import AgentRelationship
    aid = await _make_webhook_agent({'token': 'unused'})
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        from app.models.tenant import Tenant
        import uuid
        tenant = Tenant(name="Trigger patch", slug=f"trigger-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        user = await db.get(User, agent.creator_id)
        agent.tenant_id = tenant.id
        user.tenant_id = tenant.id
        await db.flush()
        db.add(AgentRelationship(agent_id=aid, user_id=agent.creator_id))
        trigger = (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))).scalar_one()
        trigger.type = 'on_message'
        trigger.config = {'from_agent_id': str(aid), '_watch_session_id': 'old', '_origin_session_id': 'origin', 'keyword': 'keep'}
        await db.commit()
        user_id = str(agent.creator_id)
    result = await _handle_update_trigger(aid, {'name': 'h', 'config': {'from_user_id': user_id}, 'clear_fields': ['/config/from_agent_id']})
    assert '✅' in result, result
    config = (await row(aid)).config
    assert config['from_user_id'] == user_id and 'from_agent_id' not in config
    assert config['keyword'] == 'keep' and config['_origin_session_id'] == 'origin'
    assert '_watch_session_id' not in config


async def test_api_private_parent_protection_and_system_clear():
    aid = await _make_webhook_agent({'token': 'stable', 'headers': {'api_key': 'hidden'}})
    trigger = await row(aid)
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        user = await db.get(User, agent.creator_id)
    for patch in [TriggerUpdate(config={'headers': None}), TriggerUpdate(clear_fields=['/config/headers']), TriggerUpdate(config={'headers': {'api_key': {}}})]:
        with pytest.raises(HTTPException):
            await update_trigger(aid, trigger.id, patch, user)
    assert (await row(aid)).config == trigger.config
    async with async_session() as db:
        existing = await db.get(AgentTrigger, trigger.id)
        existing.is_system = True
        existing.temperature = 1.2
        await db.commit()
    result = await update_trigger(aid, trigger.id, TriggerUpdate(clear_fields=['/temperature']), user)
    assert result['trigger']['temperature'] is None


async def test_reenable_cannot_exceed_agent_limit():
    aid = await _make_webhook_agent({'token': 'stable'})
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        agent.max_triggers = 1
        db.add(AgentTrigger(agent_id=aid, name='disabled', type='interval', config={'minutes': 5}, reason='r', is_enabled=False))
        await db.commit()
    result = await _handle_set_trigger(aid, {'name': 'disabled'})
    assert '❌' in result and 'limit' in result
