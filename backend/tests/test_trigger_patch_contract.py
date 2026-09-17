"""Lossless trigger edits through tool and management API boundaries."""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from fastapi import HTTPException

from app.database import async_session
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_tools_trigger_management import (
    _handle_update_trigger,
    _handle_list_triggers,
    _handle_cancel_trigger,
    _handle_delete_trigger,
)
from app.services.agent_tools_trigger_ops import _handle_set_trigger
from app.api.triggers import (
    TriggerUpdate,
    delete_trigger as delete_trigger_api,
    list_trigger_executions,
    update_trigger,
)
from app.services.trigger_runtime.queue import enqueue_trigger_execution
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


async def test_cancelled_trigger_does_not_consume_trigger_limit():
    aid = await _make_webhook_agent({"token": "stable"})
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        agent.max_triggers = 1
        await db.commit()
    await _handle_cancel_trigger(aid, {"name": "h"})

    created = await _handle_set_trigger(
        aid,
        {"name": "replacement", "type": "webhook", "config": {}, "reason": "replacement"},
    )
    assert json.loads(created)["ok"] is True
    async with async_session() as db:
        triggers = (
            await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        ).scalars().all()
    assert len(triggers) == 2
    assert sum(trigger.is_enabled for trigger in triggers) == 1


async def test_delete_disabled_trigger_preserves_terminal_execution_history():
    aid = await _make_webhook_agent({"token": "stable"})
    async with async_session() as db:
        trigger = await db.scalar(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        execution, created = await enqueue_trigger_execution(
            db,
            trigger=trigger,
            source="manual",
            idempotency_key="delete-history",
            commit=False,
        )
        assert created and execution is not None
        execution.status = "completed"
        trigger.is_enabled = False
        trigger_id = trigger.id
        execution_id = execution.id
        agent = await db.get(Agent, aid)
        user = await db.get(User, agent.creator_id)
        tenant = Tenant(
            name="Trigger history",
            slug=f"trigger-history-{str(trigger.id)[:12]}",
        )
        db.add(tenant)
        await db.flush()
        agent.tenant_id = tenant.id
        user.tenant_id = tenant.id
        await db.flush()
        conversation = ChatSession(
            agent_id=aid,
            user_id=user.id,
            source_channel="web",
            title="Historical trigger run",
        )
        db.add(conversation)
        await db.flush()
        execution.conversation_id = conversation.id
        conversation_id = conversation.id
        legacy_execution_id = uuid.uuid4()
        await db.execute(
            text(
                """
                INSERT INTO trigger_executions (
                    id, trigger_id, agent_id, conversation_id, source, status,
                    idempotency_key, payload, payload_text
                ) VALUES (
                    :id, :trigger_id, :agent_id, :conversation_id, 'manual',
                    'completed', 'legacy-delete-history', '{}'::jsonb, ''
                )
                """
            ),
            {
                "id": legacy_execution_id,
                "trigger_id": trigger_id,
                "agent_id": aid,
                "conversation_id": conversation_id,
            },
        )
        await db.commit()

    deleted = json.loads(await _handle_delete_trigger(aid, {"id": str(trigger_id)}, user_id=user.id))
    assert deleted["ok"] is True
    async with async_session() as db:
        assert await db.get(AgentTrigger, trigger_id) is None
        history = await db.get(TriggerExecution, execution_id)
        assert history is not None
        assert history.trigger_id == trigger_id
        assert history.trigger_name == "h"
        assert history.conversation_id == conversation_id
        assert await db.get(ChatSession, conversation_id) is not None
        legacy_history = await db.get(TriggerExecution, legacy_execution_id)
        assert legacy_history is not None
        assert legacy_history.trigger_name == "h"
        assert legacy_history.conversation_id == conversation_id

    listed = await list_trigger_executions(aid, limit=100, user=user)
    assert listed[0].trigger_id == str(trigger_id)
    assert listed[0].trigger_name == "h"

    recreated = await _handle_set_trigger(
        aid,
        {"name": "h", "type": "webhook", "config": {}, "reason": "new definition"},
        user_id=user.id,
    )
    assert json.loads(recreated)["ok"] is True


async def test_api_delete_uses_shared_disabled_trigger_contract():
    aid = await _make_webhook_agent({"token": "stable"})
    trigger = await row(aid)
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        user = await db.get(User, agent.creator_id)

    with pytest.raises(HTTPException) as active:
        await delete_trigger_api(aid, trigger.id, user)
    assert active.value.status_code == 409

    await _handle_cancel_trigger(aid, {"name": "h"})
    deleted = await delete_trigger_api(aid, trigger.id, user)
    assert deleted["deleted_trigger"] == {"id": str(trigger.id), "name": "h"}
    async with async_session() as db:
        assert await db.get(AgentTrigger, trigger.id) is None


async def test_database_guards_legacy_delete_and_enqueue_paths():
    aid = await _make_webhook_agent({"token": "stable"})
    trigger = await row(aid)

    async with async_session() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text("DELETE FROM agent_triggers WHERE id = :id"),
                {"id": trigger.id},
            )
        await db.rollback()

    await _handle_cancel_trigger(aid, {"name": "h"})
    execution_id = uuid.uuid4()
    async with async_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO trigger_executions (
                    id, trigger_id, agent_id, source, status,
                    idempotency_key, payload, payload_text
                ) VALUES (
                    :id, :trigger_id, :agent_id, 'manual', 'pending',
                    'legacy-delete-guard', '{}'::jsonb, ''
                )
                """
            ),
            {"id": execution_id, "trigger_id": trigger.id, "agent_id": aid},
        )
        await db.commit()

    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        assert execution.trigger_name == "h"
        with pytest.raises(DBAPIError):
            await db.execute(
                text("DELETE FROM agent_triggers WHERE id = :id"),
                {"id": trigger.id},
            )
        await db.rollback()

    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        execution.status = "completed"
        await db.commit()
    async with async_session() as db:
        await db.execute(
            text("DELETE FROM agent_triggers WHERE id = :id"),
            {"id": trigger.id},
        )
        await db.commit()
    async with async_session() as db:
        assert await db.get(AgentTrigger, trigger.id) is None
        assert (await db.get(TriggerExecution, execution_id)).trigger_name == "h"


async def test_agent_delete_keeps_existing_trigger_cascade_behavior():
    aid = await _make_webhook_agent({"token": "stable"})
    trigger = await row(aid)
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        await db.delete(agent)
        await db.commit()
    async with async_session() as db:
        assert await db.get(AgentTrigger, trigger.id) is None


async def test_delete_trigger_rejects_active_system_and_unfinished_definitions():
    aid = await _make_webhook_agent({"token": "stable"})
    assert "Cancel the trigger" in await _handle_delete_trigger(aid, {"name": "h"})

    await _handle_cancel_trigger(aid, {"name": "h"})
    async with async_session() as db:
        trigger = await db.scalar(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        execution, _created = await enqueue_trigger_execution(
            db,
            trigger=trigger,
            source="manual",
            idempotency_key="delete-pending",
            commit=False,
        )
        await db.commit()
    assert "current run" in await _handle_delete_trigger(aid, {"name": "h"})

    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution.id)
        execution.status = "completed"
        trigger = await db.scalar(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        trigger.is_system = True
        await db.commit()
    assert "System triggers" in await _handle_delete_trigger(aid, {"name": "h"})


async def test_delete_waits_for_concurrent_enqueue_then_preserves_pending_run():
    aid = await _make_webhook_agent({"token": "stable"})
    await _handle_cancel_trigger(aid, {"name": "h"})

    async with async_session() as db:
        trigger = await db.scalar(select(AgentTrigger).where(AgentTrigger.agent_id == aid))
        execution, created = await enqueue_trigger_execution(
            db,
            trigger=trigger,
            source="manual",
            idempotency_key="delete-race",
            commit=False,
        )
        assert created and execution is not None
        deleting = asyncio.create_task(_handle_delete_trigger(aid, {"name": "h"}))
        await asyncio.sleep(0.05)
        assert not deleting.done()
        await db.commit()

    result = await asyncio.wait_for(deleting, timeout=2)
    assert "current run" in result
    async with async_session() as db:
        assert await db.get(AgentTrigger, trigger.id) is not None
        assert await db.get(TriggerExecution, execution.id) is not None


async def test_delete_trigger_is_seeded_and_visible_to_existing_standard_agent():
    from app.models.tool import AgentTool, Tool
    from app.services.agent_tools import get_agent_tools_for_llm
    from app.services.tool_seeder import seed_builtin_tools

    aid = await _make_webhook_agent({"token": "stable"})
    await seed_builtin_tools()
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "delete_trigger"))
        assert tool is not None
        assignment = await db.scalar(
            select(AgentTool).where(
                AgentTool.agent_id == aid,
                AgentTool.tool_id == tool.id,
            )
        )
        if assignment is None:
            assignment = AgentTool(agent_id=aid, tool_id=tool.id, enabled=True)
            db.add(assignment)
        else:
            assignment.enabled = True
        await db.commit()
    assert assignment.enabled

    runtime_tools = await get_agent_tools_for_llm(aid)
    runtime = {
        item["function"]["name"]: item["function"]
        for item in runtime_tools
        if item.get("type") == "function"
    }
    assert runtime["delete_trigger"]["description"] == (
        "Delete a disabled trigger you no longer need. "
        "Cancel active triggers first. Past runs remain in history."
    )
    assert runtime["delete_trigger"]["parameters"]["anyOf"] == [
        {"required": ["name"]},
        {"required": ["id"]},
    ]
