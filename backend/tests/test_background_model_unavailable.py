"""Durable background work must not report success with an unavailable model."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.models.registry  # noqa: F401 - real durable turn foreign-key graph

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_runtime() -> tuple[User, Agent, LLMModel]:
    async with async_session() as db:
        tenant = Tenant(name="Background", slug=f"background-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"background_{uuid.uuid4().hex[:10]}",
            email=f"{uuid.uuid4().hex[:10]}@background.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Owner",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Background Agent",
            creator_id=user.id,
            tenant_id=tenant.id,
            agent_type="native",
            status="running",
        )
        model = LLMModel(
            tenant_id=tenant.id,
            provider="openai",
            model="disabled-model",
            label="Disabled Model",
            api_key_encrypted="x",
            enabled=False,
        )
        db.add_all([agent, model])
        await db.commit()
        await db.refresh(user)
        await db.refresh(agent)
        await db.refresh(model)
        return user, agent, model


async def test_task_with_unavailable_model_returns_to_pending(monkeypatch):
    from app.services.task_executor import execute_task

    user, agent, model = await _seed_runtime()
    async with async_session() as db:
        task = Task(
            agent_id=agent.id,
            title="Unavailable override",
            type="todo",
            status="pending",
            priority="medium",
            assignee="self",
            created_by=user.id,
            execution_user_id=user.id,
            model_id=model.id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    monkeypatch.setattr(
        "app.services.active_turns.ensure_active_turn",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("static", "dynamic")),
    )

    await execute_task(task_id, agent.id, user.id)

    async with async_session() as db:
        persisted = await db.get(Task, task_id)
        logs = (
            await db.execute(
                select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at)
            )
        ).scalars().all()
        terminal = await db.scalar(select(ChatMessage).where(
            ChatMessage.agent_id == agent.id, ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_status"].as_string() == "failed",
        ))
    assert persisted.status == "pending"
    assert logs and terminal is not None
    assert terminal.message_meta["error_code"] == "model_unavailable"
    assert terminal.content in logs[-1].content


async def test_schedule_with_unavailable_model_is_failed(monkeypatch):
    from app.services.scheduler import ScheduleExecutionOutcome, _execute_schedule

    user, agent, model = await _seed_runtime()
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("static", "dynamic")),
    )

    outcome = await _execute_schedule(
        uuid.uuid4(),
        agent.id,
        "Run once",
        user.id,
        model_id=model.id,
    )

    assert outcome is ScheduleExecutionOutcome.FAILED
