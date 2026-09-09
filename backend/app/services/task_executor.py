"""Task admission and deterministic reminders; model turns use shared recovery."""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.task import Task, TaskLog
from app.services.agent_context import build_agent_context
from app.services.background_task_admission import create_background_turn, resource_execution_settings
from app.services.llm.failure_outcome import render_message
from app.services.project_runtime_boundary import is_project_agent, lock_and_check_project_agent_runtime

TASK_EXECUTION_ADDENDUM = """

## Task Execution Mode

You are now in TASK EXECUTION MODE (not a conversation). A task has been assigned to you.
- Focus on completing the task as thoroughly as possible.
- Break down complex tasks into steps and execute each step.
- Use your tools actively to gather information, send messages, read/write files, etc.
- Provide a detailed execution report at the end.
- If the task involves contacting someone, use the available messaging tools to reach them.
- If the task requires data or information, use your tools to fetch it.
- Do NOT ask the user follow-up questions — take initiative and complete the task autonomously.
"""

async def prepare_task_turn(db, task_id, agent_id, execution_user_id=None):
    """Prepare a model task; release snapshot reads before loading its context.

    The caller starts with no uncommitted business writes and commits the
    returned anchor before dispatch or returning an accepted response.
    """
    task = await db.get(Task, task_id)
    agent = await db.get(Agent, agent_id)
    if task is None or agent is None or task.agent_id != agent_id or task.status == "doing":
        return None
    if task.type == "supervision":
        return None
    task_settings = resource_execution_settings(task)
    agent_name, role = agent.name, agent.role_description or ""
    await db.commit()
    static, dynamic = await build_agent_context(
        agent_id, agent_name, role,
        include_soul=task_settings["include_soul"],
        include_memory=task_settings["include_memory"],
    )
    return await _admit_task_turn(db, task_id, agent_id, execution_user_id, task_settings, static, dynamic)


async def prepare_created_task_turn(db, task: Task):
    """Persist a new task and its run together; the caller commits before dispatch.

    The Task stays transient while context is loaded, so releasing the initial
    read transaction cannot strand an unstarted pending task.
    """
    agent = await db.get(Agent, task.agent_id)
    if agent is None:
        return None
    task.soul = True if task.soul is None else task.soul
    task.memory = True if task.memory is None else task.memory
    settings = resource_execution_settings(task)
    agent_id, agent_name, role = agent.id, agent.name, agent.role_description or ""
    await db.commit()
    static, dynamic = await build_agent_context(
        agent_id, agent_name, role,
        include_soul=settings["include_soul"], include_memory=settings["include_memory"],
    )
    db.add(task)
    await db.flush()
    return await _admit_task_turn(
        db, task.id, agent_id, task.execution_user_id, settings, static, dynamic,
    )


async def _admit_task_turn(db, task_id, agent_id, execution_user_id, task_settings, static, dynamic):
    agent = await db.get(Agent, agent_id, populate_existing=True)
    if agent is None:
        return None
    if is_project_agent(agent) and not await lock_and_check_project_agent_runtime(db, agent):
        return None
    task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
    if task is None or task.agent_id != agent_id or task.status == "doing" or task.type == "supervision":
        return None
    executor = execution_user_id or task.execution_user_id or task.created_by
    task.status = "doing"
    task.completed_at = None
    task_run = TaskLog(
        task_id=task_id, content=render_message("background.taskStarted"), execution_user_id=executor,
    )
    db.add(task_run)
    await db.flush()
    user_prompt = render_message("background.taskInput").format(title=task.title)
    if task.description:
        user_prompt += "\n" + render_message("background.taskDescription").format(description=task.description)
    user_prompt += "\n\n" + render_message("background.taskInstruction")
    task_settings["prepared_turn_context"] = [static, dynamic + TASK_EXECUTION_ADDENDUM]
    return await create_background_turn(
        db, agent=agent, kind="task", reference_id=task_run.id,
        user_prompt=user_prompt, execution_user_id=executor, title=task.title,
        settings=task_settings,
        completion={"task_id": str(task.id), "title": task.title, "task_type": task.type},
    )


async def _execute_supervision(task_id, agent_id, execution_user_id):
    """Keep deterministic reminder delivery outside the model execution loop."""
    from app.services.supervision_reminder import _send_supervision_reminder

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        if agent is None:
            return
        if is_project_agent(agent) and not await lock_and_check_project_agent_runtime(db, agent):
            return
        task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None or task.agent_id != agent_id or task.status == "doing":
            return
        executor = execution_user_id or task.execution_user_id or task.created_by
        task.status = "doing"
        db.add(TaskLog(task_id=task.id, content=render_message("background.taskStarted"), execution_user_id=executor))
        await db.commit()
    try:
        await _send_supervision_reminder(task, agent.name, execution_user_id=executor)
    finally:
        async with async_session() as db:
            stored = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if stored and stored.status == "doing":
                stored.status = "pending"
                await db.commit()


async def execute_task(
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID | None = None,
) -> None:
    """Persist one task run before dispatch; interruption keeps its anchor intact."""
    from app.services.background_turns import run_background_turn

    async with async_session() as db:
        task = await db.get(Task, task_id)
        supervision = task is not None and task.type == "supervision"
    if supervision:
        await _execute_supervision(task_id, agent_id, execution_user_id)
        return
    async with async_session() as db:
        anchor = await prepare_task_turn(db, task_id, agent_id, execution_user_id)
        if anchor is None:
            return
        anchor_id = anchor.id
        await db.commit()
    try:
        await run_background_turn(anchor_id)
    except Exception:
        logger.exception("Task {} dispatch interrupted; its durable turn remains recoverable", task_id)
