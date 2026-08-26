"""Background task executor — runs LLM to complete tasks automatically.

Uses the same agent context (soul, memory, skills, relationships, tools)
as the chat dialog. Supports tool-calling loop for autonomous execution.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.project import Project
from app.models.task import Task, TaskLog
from app.services.project_service import project_runtime_allows_agent
from app.services.workload_capacity import (
    WorkloadKind,
    WorkloadOverloadedError,
    get_workload_capacity,
)

settings = get_settings()


async def execute_task(
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID | None = None,
) -> None:
    """Run a task and always restore a cancelled durable execution."""

    try:
        await _execute_task_impl(task_id, agent_id, execution_user_id)
    except asyncio.CancelledError:
        await _restore_cancelled_task(
            task_id,
            execution_user_id=execution_user_id,
        )
        raise


async def _execute_task_impl(
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID | None = None,
) -> None:
    """Execute a task using the agent's configured LLM with full context.

    Uses the same context as chat dialog: build_agent_context for system prompt,
    agent tools for tool-calling, and a multi-round tool loop.

    Flow:
      - todo tasks: pending → doing → done
      - supervision tasks: pending → doing → pending (stays active, just logs result)
    """
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")
    task_run_id: uuid.UUID | None = None

    # Step 1: Claim only work whose authoritative project runtime is running.
    # Lock the Project before the Task so this admission boundary serializes
    # with the owner pause switch and the shared manual-run path.
    async with async_session() as db:
        task_agent_id = await db.scalar(select(Task.agent_id).where(Task.id == task_id))
        if task_agent_id is None:
            # Lightweight session doubles and older adapters may not implement
            # scalar column reads. Keep the standard-Agent path compatible by
            # falling back to the original locked Task lookup.
            task = (
                await db.execute(select(Task).where(Task.id == task_id).with_for_update())
            ).scalar_one_or_none()
            if task is None:
                logger.warning(f"[TaskExec] Task {task_id} not found")
                return
            task_agent_id = getattr(task, "agent_id", agent_id)
            task_agent = None
        else:
            task_agent = await db.get(Agent, task_agent_id)
            if task_agent is None:
                logger.warning(f"[TaskExec] Agent {task_agent_id} not found for task {task_id}")
                return
        if task_agent_id != agent_id:
            logger.warning(
                f"[TaskExec] Task {task_id} belongs to agent {task_agent_id}, not {agent_id}"
            )
            return

        if task_agent is not None and getattr(task_agent, "scope", "standard") == "project" and getattr(task_agent, "project_id", None) is not None:
            await db.scalar(
                select(Project.id)
                .where(Project.id == task_agent.project_id)
                .with_for_update()
            )
        if task_agent is not None and not await project_runtime_allows_agent(db, task_agent):
            logger.info(f"[TaskExec] Task {task_id} deferred because its project is paused")
            return

        if task_agent is not None:
            task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None:
                logger.warning(f"[TaskExec] Task {task_id} disappeared before claim")
                return
        if task.status == "doing":
            logger.info(f"[TaskExec] Task {task_id} is already running; duplicate skipped")
            return

        task_execution_user_id = execution_user_id or task.execution_user_id or task.created_by
        from app.services.active_turns import ensure_active_turn

        await ensure_active_turn(
            owner_user_id=task_execution_user_id,
            agent_id=agent_id,
            session_id=str(task_id),
            turn_type="task",
            title=task.title,
        )
        task.status = "doing"
        task_run = TaskLog(
            task_id=task_id,
            content="🤖 开始执行任务...",
            execution_user_id=task_execution_user_id,
        )
        db.add(task_run)
        await db.flush()
        task_run_id = task_run.id
        await db.commit()
        task_title = task.title
        task_description = task.description or ""
        task_type = task.type  # 'todo' or 'supervision'

    # Reload the durable run snapshot after releasing the transition
    # transaction. This is the source of truth if an administrator reassigns
    # future task runs while this run is already active.
    async with async_session() as db:
        snapshot = await db.scalar(select(TaskLog.execution_user_id).where(TaskLog.id == task_run_id))
        if snapshot is not None:
            task_execution_user_id = snapshot

    # Step 2: Load an immutable agent snapshot, then release the read
    # transaction before storage/context work or provider I/O begins.
    async with async_session() as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            await _log_error(task_id, "数字员工未找到")
            if task_type == "supervision":
                await _restore_supervision_status(task_id)
            return
        agent_name = agent.name
        agent_role_description = agent.role_description or ""
        is_project_agent = getattr(agent, "scope", "standard") == "project"
        tenant_key = (
            getattr(agent, "company_id", None)
            or getattr(agent, "tenant_id", None)
            or task_execution_user_id
            or agent_id
        )

    try:
        # Admission may wait. All task and agent snapshot transactions are
        # closed before this point, so a queued background turn cannot pin the
        # database pool.
        async with get_workload_capacity().slot(WorkloadKind.BACKGROUND, tenant_key):
            project_paused = False
            if is_project_agent:
                async with async_session() as db:
                    current_agent = await db.get(Agent, agent_id)
                    project_paused = current_agent is not None and not await project_runtime_allows_agent(
                        db, current_agent
                    )
                if current_agent is None:
                    await _log_error(task_id, "数字员工未找到")
                    if task_type == "supervision":
                        await _restore_supervision_status(task_id)
                    return
            if project_paused:
                logger.info(
                    f"[TaskExec] Task {task_id} returned to pending because its project paused before execution"
                )
                await _restore_retryable_task(
                    task_id,
                    execution_user_id=task_execution_user_id,
                    log_message="⏸️ 项目已暂停，本次执行未开始；恢复项目后可重新执行。",
                )
                return
            await _execute_admitted_task(
                task=task,
                task_id=task_id,
                agent_id=agent_id,
                task_execution_user_id=task_execution_user_id,
                task_title=task_title,
                task_description=task_description,
                task_type=task_type,
                agent_name=agent_name,
                agent_role_description=agent_role_description,
            )
    except asyncio.CancelledError:
        await _restore_cancelled_task(task_id, execution_user_id=task_execution_user_id)
        raise
    except WorkloadOverloadedError as e:
        logger.warning(f"[TaskExec] Task {task_id} deferred by workload capacity: {e}")
        await _restore_retryable_task(
            task_id,
            execution_user_id=task_execution_user_id,
        )
        return
    except Exception as e:  # noqa: BLE001 - task failures are persisted for operators
        error_msg = str(e) or repr(e)
        logger.error(f"[TaskExec] Error: {error_msg}")
        await _log_error(task_id, f"执行出错: {error_msg[:150]}")
        if task_type == "supervision":
            await _restore_supervision_status(task_id)
        return


async def _execute_admitted_task(
    *,
    task: Task,
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_execution_user_id: uuid.UUID | None,
    task_title: str,
    task_description: str,
    task_type: str,
    agent_name: str,
    agent_role_description: str,
) -> None:
    """Execute one admitted background task without carrying snapshot sessions."""

    if task_type == "supervision":
        # Supervision has a deterministic canonical delivery path. Do not ask
        # the model to re-resolve a display name or choose an unrelated target.
        from app.services.supervision_reminder import _send_supervision_reminder

        await _send_supervision_reminder(
            task,
            agent_name,
            execution_user_id=task_execution_user_id,
        )
        await _restore_supervision_status(task_id)
        return

    from app.services.agent_context import build_agent_context

    static_prompt, dynamic_prompt = await build_agent_context(
        agent_id,
        agent_name,
        agent_role_description,
    )

    task_addendum = """

## Task Execution Mode

You are now in TASK EXECUTION MODE (not a conversation). A task has been assigned to you.
- Focus on completing the task as thoroughly as possible.
- Break down complex tasks into steps and execute each step.
- Use your tools actively to gather information, send messages, read/write files, etc.
- Provide a detailed execution report at the end.
- If the task involves contacting someone, use `send_feishu_message` to reach them.
- If the task requires data or information, use your tools to fetch it.
- Do NOT ask the user follow-up questions — take initiative and complete the task autonomously.
"""
    dynamic_prompt += task_addendum
    system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

    user_prompt = f"[任务执行] {task_title}"
    if task_description:
        user_prompt += f"\n任务描述: {task_description}"
    user_prompt += "\n\n请认真完成此任务，给出详细的执行结果。"

    from app.services.llm import call_agent_llm_with_tools

    logger.info(f"[TaskExec] Calling LLM with tools for task: {task_title}")
    async with async_session() as db:
        reply = await call_agent_llm_with_tools(
            db=db,
            agent_id=agent_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_rounds=50,
            session_id=str(task_id),
            execution_user_id=task_execution_user_id,
            turn_type="task",
        )

    logger.info(f"[TaskExec] LLM reply: {reply[:80]}")

    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        persisted_task = result.scalar_one_or_none()
        if persisted_task:
            persisted_task.status = "done"
            persisted_task.completed_at = datetime.now(UTC)
            db.add(TaskLog(task_id=task_id, content=f"✅ 任务完成\n\n{reply}"))
            await db.commit()
            logger.info(f"[TaskExec] Task {task_id} completed!")

    from app.services.activity_logger import log_activity

    await log_activity(
        agent_id,
        "task_updated",
        f"任务执行: {task_title[:60]}",
        detail={
            "task_id": str(task_id),
            "task_type": task_type,
            "title": task_title,
            "reply": reply[:500],
        },
        related_id=task_id,
    )


async def _log_error(task_id: uuid.UUID, message: str) -> None:
    """Add an error log to the task."""
    logger.error(f"[TaskExec] Error for {task_id}: {message}")
    async with async_session() as db:
        db.add(TaskLog(task_id=task_id, content=f"❌ {message}"))
        await db.commit()


async def _restore_supervision_status(task_id: uuid.UUID) -> None:
    """Restore supervision task status back to pending after a failed execution."""
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task and task.status == "doing":
            task.status = "pending"
            await db.commit()


async def _restore_retryable_task(
    task_id: uuid.UUID,
    *,
    execution_user_id: uuid.UUID | None,
    log_message: str = "⏳ 系统繁忙，本次执行未开始，可重新执行。",
) -> None:
    """Return an admission-deferred task to pending without marking failure."""

    async with async_session() as db:
        task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task and task.status == "doing":
            log_execution_user_id = execution_user_id or task.execution_user_id or task.created_by
            task.status = "pending"
            db.add(
                TaskLog(
                    task_id=task_id,
                    content=log_message,
                    execution_user_id=log_execution_user_id,
                )
            )
            await db.commit()


async def _restore_cancelled_task(
    task_id: uuid.UUID,
    *,
    execution_user_id: uuid.UUID | None,
) -> None:
    """Return an interrupted task to a retryable state and leave an audit log."""

    async with async_session() as db:
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task and task.status == "doing":
            log_execution_user_id = execution_user_id or task.execution_user_id or task.created_by
            task.status = "pending"
            db.add(
                TaskLog(
                    task_id=task_id,
                    content="⏹️ 本次执行已被管理员终止，可重新执行。",
                    execution_user_id=log_execution_user_id,
                )
            )
            await db.commit()
