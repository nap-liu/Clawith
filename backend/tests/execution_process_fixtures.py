"""Importable behaviours for real process-boundary tests."""

import asyncio
import os
import re

from app.services.agent_execution.runtime import isolate_agent_execution
from app.services.conversation_execution_lock import serialize_conversation_execution


async def echo(value, callback=None):
    if callback:
        await callback(value)
    return {"pid": os.getpid(), "value": value}


async def nested_callback(callback):
    events = []

    async def before_injection():
        events.append("durable")

    result = await callback(2, before_injection=before_injection)
    return events, result


async def block(callback):
    await callback(os.getpid())
    re.search("出库单.*看板", ("出库单" + "x" * 30) * 200000)


async def wait(callback):
    await callback(os.getpid())
    await asyncio.Event().wait()


async def typed_failure():
    from app.services.llm.failure_outcome import model_response_idle_timeout_failure

    return model_response_idle_timeout_failure()


async def provider_wait(model, callback):
    from app.services.llm.provider_retry import _provider_slot

    async with _provider_slot(model):
        await callback("entered")
        await asyncio.sleep(0.2)
    return "complete"


@serialize_conversation_execution
@isolate_agent_execution
async def conversation(agent_id, session_id, callback, peer_id=None):
    await callback(os.getpid())
    if peer_id:
        return await conversation(peer_id, session_id, callback)
    return "complete"


@isolate_agent_execution
async def cancellable_turn(agent_id, user_id, session_id, callback):
    await callback(os.getpid())
    await asyncio.Event().wait()


@isolate_agent_execution
async def create_todo(agent_id, user_id, session_id):
    from app.services.agent_tools_task_contact_ops import _manage_tasks
    from app.services.agent_runtime_workspace import standard_agent_runtime_workspace

    return await _manage_tasks(agent_id, user_id, standard_agent_runtime_workspace(agent_id).local_root,
                               {"action": "create", "title": "Independent job", "type": "todo"})


@isolate_agent_execution
async def admit_nested(agent_id, user_id, session_id, turn_anchor_id,
                       new_session_id, new_anchor_id, callback):
    from app.database import async_session
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.services.active_turns import commit_current_turn_anchor
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    async with async_session() as db:
        db.add(ChatSession(id=new_session_id, agent_id=agent_id, user_id=user_id, source_channel="web"))
        db.add(ChatMessage(id=new_anchor_id, agent_id=agent_id, user_id=user_id,
                           conversation_id=str(new_session_id), role="user", content="Nested request"))
        await db.flush()
        await transition_conversation_turn(db, agent_id=agent_id, conversation_id=str(new_session_id),
                                           turn_anchor_id=new_anchor_id, status="running")

        async def commit():
            await callback("before_commit", os.getpid())
            await db.commit()

        await commit_current_turn_anchor(commit, agent_id=agent_id, session_id=str(new_session_id),
                                         message_id=new_anchor_id)
    await asyncio.Event().wait()
