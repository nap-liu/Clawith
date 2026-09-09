"""Background admission, STOP and business finalization through the real runner."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, text, update

from app.database import async_session, engine
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.notification import Notification
from app.models.schedule import AgentSchedule
from app.models.task import Task, TaskLog
from app.services import background_turns
from app.services.conversation_turn_lifecycle import cancel_current_conversation_turn
from app.services.heartbeat import run_agent_oneshot
from app.services.scheduler import _claim_due_schedules_for_scope, prepare_schedule_turn
from app.services.task_executor import prepare_task_turn
from app.services.turn_recovery_dispatch import cancel_recovery_dispatch, dispatch_recovery_candidates
from execution_provider_fixture import provider
from test_turn_recovery import _make_agent_with_model

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolate_background_runtime(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    await engine.dispose()
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.execute(update(Agent).values(heartbeat_enabled=False))
        await db.commit()
    yield
    await cancel_recovery_dispatch()
    await engine.dispose()


async def runtime(url):
    agent_id, user_id = await _make_agent_with_model(context_window_size=20)
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        model = await db.get(LLMModel, agent.primary_model_id)
        model.base_url = url
        model.provider = "openai"
        model.model = "test-model"
        model.api_key_encrypted = "test-key"
        model.request_timeout = 10
        agent.status = "running"
        await db.commit()
    return agent_id, user_id


async def admit_task(agent_id, user_id):
    async with async_session() as db:
        task = Task(agent_id=agent_id, created_by=user_id, execution_user_id=user_id,
                    title="Prepare durable report", description="Keep original input",
                    soul=False, memory=False)
        db.add(task)
        await db.commit()
        anchor = await prepare_task_turn(db, task.id, agent_id, user_id)
        await db.commit()
    return task.id, anchor


async def admit_schedule(agent_id, user_id):
    now = datetime.now(UTC)
    async with async_session() as db:
        schedule = AgentSchedule(
            agent_id=agent_id, created_by=user_id, execution_user_id=user_id,
            name="Durable schedule", instruction="Report current state", cron_expr="* * * * *",
            next_run_at=now - timedelta(seconds=1), soul=False, memory=False,
        )
        db.add(schedule)
        await db.commit()
    return schedule.id, now


async def own_dispatched(anchor_id):
    tasks = await dispatch_recovery_candidates()
    return next(task for task in tasks if task.get_name() == f"turn_recovery:{anchor_id}")


async def wait_for_requests(requests, count=1):
    async with asyncio.timeout(20):
        while len(requests) < count:
            await asyncio.sleep(.01)


async def terminal_rows(anchor):
    async with async_session() as db:
        return list(await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == anchor.conversation_id,
            ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_status"].as_string().in_(("completed", "failed")),
        )))


async def test_committed_task_is_discovered_and_concurrent_resume_finalizes_once():
    gate = asyncio.Event()
    async with provider([{"content": "Task completed", "_wait_for": gate}]) as (url, requests):
        agent_id, user_id = await runtime(url)
        task_id, anchor = await admit_task(agent_id, user_id)
        async with async_session() as db:
            assert (await db.get(Task, task_id)).status == "doing"
            assert anchor.user_id == user_id and anchor.sender_user_id is None
            assert await db.scalar(select(func.count()).select_from(TaskLog).where(TaskLog.task_id == task_id)) == 1
        assert requests == []
        dispatched = await own_dispatched(anchor.id)
        await wait_for_requests(requests)
        async with async_session() as db:
            assert await db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
            )) == 0
        duplicate = asyncio.create_task(background_turns.run_background_turn(anchor.id))
        gate.set()
        async with asyncio.timeout(20):
            assert (await dispatched).resumed == 1
            assert await duplicate == "Task completed"
        assert await background_turns.run_background_turn(anchor.id) == "Task completed"
        assert len(requests) == 1
        assert "Keep original input" in str(requests[0]["messages"])
        assert len(await terminal_rows(anchor)) == 1
        async with async_session() as db:
            task = await db.get(Task, task_id)
            assert task.status == "done" and task.completed_at is not None
            assert await db.scalar(select(func.count()).select_from(TaskLog).where(TaskLog.task_id == task_id)) == 2
            assert await db.scalar(select(func.count()).select_from(AgentActivityLog).where(
                AgentActivityLog.related_id == task_id, AgentActivityLog.action_type == "task_updated")) == 1


async def test_schedule_occurrences_are_claimed_once_and_manual_count_is_finalized_once():
    async with provider([{"content": "Schedule completed"}]) as (url, requests):
        agent_id, user_id = await runtime(url)
        schedule_id, now = await admit_schedule(agent_id, user_id)
        first, second = await asyncio.gather(*(
            _claim_due_schedules_for_scope(now, project_agents=False) for _ in range(2)
        ))
        claimed = [item for item in (*first, *second) if item.id == schedule_id]
        assert len(claimed) == 1
        first_anchor_id = claimed[0].anchor_id
        async with async_session() as db:
            schedule = await db.get(AgentSchedule, schedule_id)
            assert schedule.run_count == 1 and schedule.next_run_at > now
            next_due = schedule.next_run_at
        subsequent = await _claim_due_schedules_for_scope(next_due, project_agents=False)
        next_anchor_id = next(item.anchor_id for item in subsequent if item.id == schedule_id)
        assert next_anchor_id != first_anchor_id
        assert requests == []
        dispatched = await dispatch_recovery_candidates()
        own_ids = {f"turn_recovery:{first_anchor_id}", f"turn_recovery:{next_anchor_id}"}
        async with asyncio.timeout(25):
            results = await asyncio.gather(*(task for task in dispatched if task.get_name() in own_ids))
        assert len(results) == 2 and all(result.resumed == 1 for result in results)
        async with async_session() as db:
            assert (await db.get(AgentSchedule, schedule_id)).run_count == 2
            manual = await prepare_schedule_turn(db, schedule_id, agent_id, "Manual report", user_id,
                                                 soul=False, memory=False)
            await db.commit()
        async with asyncio.timeout(25):
            replies = await asyncio.gather(*(background_turns.run_background_turn(manual.id) for _ in range(2)))
        assert replies == ["Schedule completed"] * 2
        assert len(requests) == 3
        async with async_session() as db:
            assert (await db.get(AgentSchedule, schedule_id)).run_count == 3
            activities = list(await db.scalars(select(AgentActivityLog).where(
                AgentActivityLog.related_id == schedule_id, AgentActivityLog.action_type == "schedule_run")))
            assert len(activities) == 3
            assert len({item.detail_json["execution_id"] for item in activities}) == 3


@pytest.mark.parametrize("kind", ["task", "schedule"])
async def test_stop_wins_against_provider_completion_and_duplicate_recovery(kind):
    gate = asyncio.Event()
    async with provider([{"content": "Late result", "_wait_for": gate}]) as (url, requests):
        agent_id, user_id = await runtime(url)
        if kind == "task":
            resource_id, anchor = await admit_task(agent_id, user_id)
        else:
            resource_id, now = await admit_schedule(agent_id, user_id)
            claimed = await _claim_due_schedules_for_scope(now, project_agents=False)
            anchor_id = next(item.anchor_id for item in claimed if item.id == resource_id)
            async with async_session() as db:
                anchor = await db.get(ChatMessage, anchor_id)
        running = asyncio.create_task(background_turns.run_background_turn(anchor.id))
        await wait_for_requests(requests)
        async with async_session() as db:
            cancelled = await cancel_current_conversation_turn(
                db, agent_id=agent_id, conversation_id=anchor.conversation_id,
            )
            assert cancelled.status == "cancelled"
            await db.commit()
        duplicate = asyncio.create_task(background_turns.run_background_turn(anchor.id))
        gate.set()
        async with asyncio.timeout(25):
            assert await asyncio.gather(running, duplicate) == [None, None]
        assert await background_turns.run_background_turn(anchor.id) is None
        assert len(requests) == 1
        assert await terminal_rows(anchor) == []
        async with async_session() as db:
            stored = await db.get(ChatMessage, anchor.id)
            assert stored.message_meta["turn_status"] == "cancelled"
            assert stored.message_meta["background_execution"]["finalized"] is True
            if kind == "task":
                assert (await db.get(Task, resource_id)).status == "pending"
                assert await db.scalar(select(func.count()).select_from(TaskLog).where(TaskLog.task_id == resource_id)) == 2
            else:
                schedule = await db.get(AgentSchedule, resource_id)
                assert schedule.run_count == 1 and schedule.next_run_at > now


async def test_oneshot_committed_before_dispatch_reconciles_failure_notification_once(monkeypatch):
    async with provider([{"content": "Oneshot completed"}]) as (url, requests):
        agent_id, user_id = await runtime(url)
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
            model_id = agent.primary_model_id
            agent.primary_model_id = None
            await db.commit()
        with monkeypatch.context() as patch:
            patch.setattr(background_turns, "run_background_turn", AsyncMock(return_value=None))
            assert await run_agent_oneshot(agent_id, "Original automation input", user_id) == ""
        async with async_session() as db:
            anchor = await db.scalar(select(ChatMessage).where(
                ChatMessage.agent_id == agent_id, ChatMessage.role == "user",
                ChatMessage.message_meta["background_execution"]["kind"].as_string() == "oneshot"))
        assert anchor.content == "Original automation input"
        assert requests == []
        async with asyncio.timeout(20):
            assert (await (await own_dispatched(anchor.id))).resumed == 1
        await asyncio.gather(*(background_turns.run_background_turn(anchor.id) for _ in range(2)))
        assert len(await terminal_rows(anchor)) == 1
        async with async_session() as db:
            notices = list(await db.scalars(select(Notification).where(Notification.ref_id == agent_id)))
            assert len(notices) == 1 and notices[0].user_id == user_id
            assert await db.scalar(select(func.count()).select_from(AgentActivityLog).where(
                AgentActivityLog.related_id == anchor.id)) == 1
            agent = await db.get(Agent, agent_id)
            agent.primary_model_id = model_id
            await db.commit()
        assert await run_agent_oneshot(agent_id, "Try successful automation", user_id) == "Oneshot completed"
        assert len(requests) == 1
        async with async_session() as db:
            assert await db.scalar(select(func.count()).select_from(Notification).where(Notification.ref_id == agent_id)) == 0


async def test_heartbeat_tick_claims_due_agent_and_records_provider_result(monkeypatch):
    from app.services.heartbeat import _heartbeat_tick

    async with provider([{"content": "Heartbeat actionable result"}]) as (url, requests):
        agent_id, _ = await runtime(url)
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
            agent.heartbeat_enabled = True
            agent.heartbeat_active_hours = "00:00-23:59"
            agent.last_heartbeat_at = None
            notice = Notification(agent_id=agent_id, type="system", title="Heartbeat inbox",
                                  body="Preserve this pending notification")
            db.add(notice)
            await db.commit()
        with monkeypatch.context() as patch:
            dispatch = AsyncMock()
            patch.setattr("app.services.agent_execution.bridge.dispatch_background", dispatch)
            await _heartbeat_tick()
            await _heartbeat_tick()
        assert dispatch.await_count == 1 and requests == []
        anchor_id = dispatch.call_args.args[1]
        async with async_session() as db:
            anchor = await db.get(ChatMessage, anchor_id)
            assert "Preserve this pending notification" in anchor.content
            assert (await db.get(Notification, notice.id)).is_read is True
        recovered = await own_dispatched(anchor_id)
        await wait_for_requests(requests)
        async with asyncio.timeout(20):
            assert (await recovered).resumed == 1
        async with asyncio.timeout(20):
            while True:
                async with async_session() as db:
                    activity = await db.scalar(select(AgentActivityLog).where(
                        AgentActivityLog.agent_id == agent_id,
                        AgentActivityLog.action_type == "heartbeat",
                    ))
                    if activity is not None:
                        agent = await db.get(Agent, agent_id)
                        assert agent.last_heartbeat_at is not None
                        agent.heartbeat_enabled = False
                        await db.commit()
                        break
                await asyncio.sleep(.01)
        assert activity.detail_json["reply"] == "Heartbeat actionable result"
        assert "Preserve this pending notification" in str(requests[0]["messages"])
        assert await background_turns.run_background_turn(anchor_id) == "Heartbeat actionable result"
        assert len(requests) == 1


@pytest.mark.parametrize("entry", ["web", "api", "tool"])
async def test_created_task_and_anchor_are_committed_before_dispatch(entry, monkeypatch):
    from app.api import tasks as tasks_api, websocket
    from app.api.websocket_stream_ops import create_task_record_impl
    from app.models.user import User
    from app.schemas.schemas import TaskCreate
    from app.services import task_executor
    from app.services.agent_runtime_workspace import standard_agent_runtime_workspace
    from app.services.agent_tools_task_contact_ops import _manage_tasks

    async with provider([{"content": "Recovered chat task"}]) as (url, requests):
        agent_id, user_id = await runtime(url)
        title = f"Atomic {entry} task"
        accepted = []
        real_context = task_executor.build_agent_context

        async def inspect_before_admission(*args, **kwargs):
            async with async_session() as db:
                assert await db.scalar(select(Task).where(Task.agent_id == agent_id, Task.title == title)) is None
            return await real_context(*args, **kwargs)

        async def observe_dispatch(_entrypoint, anchor_id):
            async with async_session() as db:
                task = await db.scalar(select(Task).where(Task.agent_id == agent_id, Task.title == title))
                anchor = await db.get(ChatMessage, anchor_id)
                assert task.status == "doing"
                assert anchor.message_meta["background_execution"]["completion"]["task_id"] == str(task.id)
                assert await db.scalar(select(func.count()).select_from(TaskLog).where(TaskLog.task_id == task.id)) == 1
                accepted.append(anchor_id)

        with monkeypatch.context() as patch:
            patch.setattr(task_executor, "build_agent_context", inspect_before_admission)
            patch.setattr("app.services.agent_execution.bridge.dispatch_background", observe_dispatch)
            if entry == "web":
                response = await create_task_record_impl(
                    websocket, SimpleNamespace(agent_id=agent_id, user_id=user_id), title, "Chat reply",
                )
                assert "Task synced to task board" in response
            elif entry == "api":
                async with async_session() as db:
                    user = await db.get(User, user_id)
                    result = await tasks_api.create_task(agent_id, TaskCreate(title=title), user, db)
                    assert result.status == "doing"
            else:
                result = await _manage_tasks(agent_id, user_id,
                    standard_agent_runtime_workspace(agent_id).local_root,
                    {"action": "create", "title": title})
                assert "auto-execution started" in result
        assert len(accepted) == 1 and requests == []
        recovered = await own_dispatched(accepted[0])
        async with asyncio.timeout(20):
            assert (await recovered).resumed == 1
        assert len(requests) == 1
        async with async_session() as db:
            task = await db.scalar(select(Task).where(Task.agent_id == agent_id, Task.title == title))
            assert task.status == "done"
            assert await db.scalar(select(func.count()).select_from(TaskLog).where(TaskLog.task_id == task.id)) == 2
