"""Task management API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, require_current_agent_tenant
from app.core.security import get_current_user
from app.database import get_db
from app.models.task import Task, TaskLog
from app.models.user import User
from app.schemas.schemas import TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate
from app.services.recipient_resolver import RecipientResolutionError
from app.services.supervision_targets import resolve_supervision_target

router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])


def _target_error(exc: Exception) -> HTTPException:
    detail = exc.as_dict() if isinstance(exc, RecipientResolutionError) else str(exc)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _enrich_task_out(task: Task, db: AsyncSession) -> TaskOut:
    """Convert Task to TaskOut with human-readable identity labels populated."""
    await db.refresh(task)
    out = TaskOut.model_validate(task)
    user_ids = {user_id for user_id in (task.created_by, task.execution_user_id) if user_id}
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users = {user.id: user for user in users_result.scalars().all()}
        creator = users.get(task.created_by)
        execution_user = users.get(task.execution_user_id)
        if creator:
            out.creator_username = creator.username
            out.creator_display_name = creator.display_name
        if execution_user:
            out.execution_user_display_name = execution_user.display_name
    return out


@router.get("/", response_model=list[TaskOut])
async def list_tasks(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    type_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List tasks for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    query = select(Task).where(Task.agent_id == agent_id)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if type_filter:
        query = query.where(Task.type == type_filter)
    query = query.order_by(Task.created_at.desc())
    result = await db.execute(query)
    tasks_list = result.scalars().all()
    # Batch-load labels for both the immutable creator and the future execution user.
    user_ids = {
        user_id
        for task in tasks_list
        for user_id in (task.created_by, task.execution_user_id)
        if user_id
    }
    user_map = {}
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        user_map = {u.id: u for u in users_result.scalars().all()}
    out_list = []
    for t in tasks_list:
        t_out = TaskOut.model_validate(t)
        creator = user_map.get(t.created_by)
        execution_user = user_map.get(t.execution_user_id)
        if creator:
            t_out.creator_username = creator.username
            t_out.creator_display_name = creator.display_name
        if execution_user:
            t_out.execution_user_display_name = execution_user.display_name
        out_list.append(t_out)
    return out_list


@router.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    agent_id: uuid.UUID,
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new task for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    resolved_target = None
    if data.type == "supervision":
        try:
            resolved_target = await resolve_supervision_target(
                db,
                agent_id,
                target_user_id=data.supervision_target_user_id,
                target_agent_id=data.supervision_target_agent_id,
                channel=data.supervision_channel,
            )
        except (RecipientResolutionError, ValueError) as exc:
            raise _target_error(exc) from exc
    task = Task(
        agent_id=agent_id,
        title=data.title,
        description=data.description,
        type=data.type,
        priority=data.priority,
        due_date=data.due_date,
        created_by=current_user.id,
        execution_user_id=current_user.id,
        supervision_target_user_id=data.supervision_target_user_id,
        supervision_target_agent_id=data.supervision_target_agent_id,
        supervision_target_name=(
            resolved_target.display_name if resolved_target is not None else None
        ),
        supervision_channel=(
            resolved_target.channel if resolved_target is not None else None
        ),
        remind_schedule=data.remind_schedule,
    )
    db.add(task)
    await db.flush()

    task_out = await _enrich_task_out(task, db)

    # Commit so the background executor can see the task in its own session
    await db.commit()

    # Fire background execution for todo tasks
    if data.type == "todo":
        import asyncio
        from app.services.task_executor import execute_task
        asyncio.create_task(execute_task(task.id, agent_id, task.execution_user_id))

    return task_out


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a task."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    changes = data.model_dump(exclude_unset=True)
    identity_reassigned = False
    if "execution_user_id" in changes:
        if changes["execution_user_id"] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="execution_user_id cannot be null",
            )
        if "expected_execution_user_id" not in changes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expected_execution_user_id is required",
            )
        from app.services.execution_identity import (
            ExecutionIdentityConflict,
            ExecutionIdentityError,
            ExecutionIdentityPermissionError,
            reassign_background_execution_user,
        )

        try:
            await reassign_background_execution_user(
                db,
                actor_user_id=current_user.id,
                agent_id=agent_id,
                resource_type="task",
                resource_id=task_id,
                execution_user_id=changes["execution_user_id"],
                expected_execution_user_id=changes.get("expected_execution_user_id"),
                expected_provided=True,
            )
        except ExecutionIdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExecutionIdentityPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExecutionIdentityError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        identity_reassigned = True
        changes.pop("execution_user_id")
        changes.pop("expected_execution_user_id", None)
    elif "expected_execution_user_id" in changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expected_execution_user_id requires execution_user_id",
        )
    if (
        changes.get("supervision_target_user_id") is not None
        and "supervision_target_agent_id" not in changes
    ):
        changes["supervision_target_agent_id"] = None
    if (
        changes.get("supervision_target_agent_id") is not None
        and "supervision_target_user_id" not in changes
    ):
        changes["supervision_target_user_id"] = None
    target_user_id = changes.get(
        "supervision_target_user_id", task.supervision_target_user_id
    )
    target_agent_id = changes.get(
        "supervision_target_agent_id", task.supervision_target_agent_id
    )
    target_channel = changes.get("supervision_channel", task.supervision_channel)
    target_contract_changed = any(
        field in changes
        for field in (
            "supervision_target_user_id",
            "supervision_target_agent_id",
            "supervision_channel",
        )
    )
    target_is_reactivated = changes.get("status") in {"pending", "doing"}
    if task.type == "supervision" and (
        target_contract_changed or target_is_reactivated
    ):
        try:
            resolved_target = await resolve_supervision_target(
                db,
                agent_id,
                target_user_id=target_user_id,
                target_agent_id=target_agent_id,
                channel=target_channel,
            )
        except (RecipientResolutionError, ValueError) as exc:
            raise _target_error(exc) from exc
        changes["supervision_target_name"] = resolved_target.display_name
        changes["supervision_channel"] = resolved_target.channel
    elif task.type != "supervision" and (
        target_user_id is not None or target_agent_id is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="only supervision tasks may define a supervision target",
        )

    if (changes or data.model_fields_set) and not identity_reassigned:
        from app.services.execution_identity import align_background_execution_user

        await align_background_execution_user(
            db,
            agent_id=agent_id,
            resource_type="task",
            resource_id=task.id,
            execution_user_id=current_user.id,
        )
    for field, value in changes.items():
        setattr(task, field, value)
    await db.flush()
    return await _enrich_task_out(task, db)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_task_logs(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get progress logs for a task."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    task_exists = await db.scalar(
        select(Task.id).where(Task.id == task_id, Task.agent_id == agent_id)
    )
    if not task_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    result = await db.execute(
        select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at.asc())
    )
    return [TaskLogOut.model_validate(log) for log in result.scalars().all()]


@router.post("/{task_id}/logs", response_model=TaskLogOut, status_code=status.HTTP_201_CREATED)
async def add_task_log(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskLogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a progress log entry to a task."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    task = await db.scalar(
        select(Task).where(Task.id == task_id, Task.agent_id == agent_id)
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    from app.services.execution_identity import align_background_execution_user

    await align_background_execution_user(
        db,
        agent_id=agent_id,
        resource_type="task",
        resource_id=task.id,
        execution_user_id=current_user.id,
    )
    log = TaskLog(task_id=task_id, content=data.content)
    db.add(log)
    await db.flush()
    return TaskLogOut.model_validate(log)


@router.post("/{task_id}/trigger")
async def trigger_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a supervision task execution (for testing)."""
    from app.core.permissions import is_agent_expired
    agent, _access = await check_agent_access(db, current_user, agent_id)
    require_current_agent_tenant(current_user, agent)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")

    result = await db.execute(select(Task).where(Task.id == task_id, Task.agent_id == agent_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.services.background_manual_run import (
        BackgroundManualRunConflict,
        run_background_resource,
    )

    try:
        await run_background_resource(
            db,
            actor_user_id=current_user.id,
            agent_id=agent_id,
            resource_type="task",
            resource=str(task.id),
        )
    except BackgroundManualRunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"status": "triggered", "task_id": str(task_id)}
