"""Schedule API — CRUD for agent cron jobs."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    check_agent_access,
    is_agent_creator,
    is_agent_expired,
    is_platform_admin_user,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.schedule import AgentSchedule
from app.models.user import User
from app.services.project_service import project_runtime_allows_agent
from app.services.scheduler import compute_next_run

router = APIRouter(prefix="/agents/{agent_id}/schedules", tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    instruction: str = Field(default="", max_length=5000)
    cron_expr: str = Field(min_length=1, max_length=100)
    is_enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    instruction: str | None = None
    cron_expr: str | None = None
    is_enabled: bool | None = None
    execution_user_id: uuid.UUID | None = None
    expected_execution_user_id: uuid.UUID | None = None


class ScheduleOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    cron_expr: str
    is_enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int
    created_by: uuid.UUID | None = None
    created_by_user_id: uuid.UUID | None = None
    execution_user_id: uuid.UUID | None = None
    creator_username: str | None = None
    creator_display_name: str | None = None
    execution_user_display_name: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[ScheduleOut])
async def list_schedules(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all schedules for an agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentSchedule).where(AgentSchedule.agent_id == agent_id).order_by(AgentSchedule.created_at.desc())
    )
    schedules = result.scalars().all()
    user_ids = {
        user_id for schedule in schedules for user_id in (schedule.created_by, schedule.execution_user_id) if user_id
    }
    user_map = {}
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        user_map = {u.id: u for u in users_result.scalars().all()}
    out_list = []
    for s in schedules:
        s_out = ScheduleOut.model_validate(s)
        creator = user_map.get(s.created_by)
        execution_user = user_map.get(s.execution_user_id)
        if creator:
            s_out.creator_username = creator.username
            s_out.creator_display_name = creator.display_name
        if execution_user:
            s_out.execution_user_display_name = execution_user.display_name
        out_list.append(s_out)
    return out_list


@router.post("/", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    agent_id: uuid.UUID,
    data: ScheduleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new schedule for an agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")

    # Validate cron expression
    next_run = compute_next_run(data.cron_expr)
    if not next_run:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {data.cron_expr}")

    sched = AgentSchedule(
        agent_id=agent_id,
        name=data.name,
        instruction=data.instruction,
        cron_expr=data.cron_expr,
        is_enabled=data.is_enabled,
        next_run_at=next_run if data.is_enabled else None,
        created_by=current_user.id,
        execution_user_id=current_user.id,
    )
    db.add(sched)
    await db.flush()
    return ScheduleOut.model_validate(sched)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a schedule."""
    agent, access = await check_agent_access(db, current_user, agent_id)

    result = await db.execute(
        select(AgentSchedule).where(AgentSchedule.id == schedule_id, AgentSchedule.agent_id == agent_id)
    )
    sched = result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = data.model_dump(exclude_unset=True)
    non_identity_fields = set(updates) - {
        "execution_user_id",
        "expected_execution_user_id",
    }
    if non_identity_fields and not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")
    identity_admin = access == "manage" and (current_user.role == "org_admin" or is_platform_admin_user(current_user))
    if updates and not non_identity_fields and not (is_agent_creator(current_user, agent) or identity_admin):
        raise HTTPException(status_code=403, detail="Manage access is required")
    identity_reassigned = False
    if "execution_user_id" in updates:
        if updates["execution_user_id"] is None:
            raise HTTPException(status_code=422, detail="execution_user_id cannot be null")
        if "expected_execution_user_id" not in updates:
            raise HTTPException(status_code=422, detail="expected_execution_user_id is required")
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
                resource_type="schedule",
                resource_id=schedule_id,
                execution_user_id=updates["execution_user_id"],
                expected_execution_user_id=updates.get("expected_execution_user_id"),
                expected_provided=True,
            )
        except ExecutionIdentityConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExecutionIdentityPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExecutionIdentityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        identity_reassigned = True
        updates.pop("execution_user_id")
        updates.pop("expected_execution_user_id", None)
    elif "expected_execution_user_id" in updates:
        raise HTTPException(status_code=422, detail="expected_execution_user_id requires execution_user_id")
    if data.model_fields_set and not identity_reassigned:
        from app.services.execution_identity import align_background_execution_user

        await align_background_execution_user(
            db,
            agent_id=agent_id,
            resource_type="schedule",
            resource_id=sched.id,
            execution_user_id=current_user.id,
        )
    for field, value in updates.items():
        setattr(sched, field, value)

    # Recompute next_run if cron or enabled changed
    if "cron_expr" in updates or "is_enabled" in updates:
        if sched.is_enabled:
            sched.next_run_at = compute_next_run(sched.cron_expr)
        else:
            sched.next_run_at = None

    await db.flush()
    return ScheduleOut.model_validate(sched)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a schedule."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")

    result = await db.execute(
        select(AgentSchedule).where(AgentSchedule.id == schedule_id, AgentSchedule.agent_id == agent_id)
    )
    sched = result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    await db.delete(sched)
    await db.flush()


@router.post("/{schedule_id}/run")
async def trigger_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a schedule execution."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="数字员工已过期，无法触发。")
    if not await project_runtime_allows_agent(db, agent):
        raise HTTPException(status_code=409, detail="项目已暂停；恢复项目后才能运行调度。")

    result = await db.execute(
        select(AgentSchedule).where(AgentSchedule.id == schedule_id, AgentSchedule.agent_id == agent_id)
    )
    sched = result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")

    from app.services.background_manual_run import run_background_resource

    await run_background_resource(
        db,
        actor_user_id=current_user.id,
        agent_id=agent_id,
        resource_type="schedule",
        resource=str(sched.id),
    )

    return {"status": "triggered", "schedule_id": str(schedule_id)}


@router.get("/{schedule_id}/history")
async def get_schedule_history(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get execution history for a schedule from activity logs."""
    await check_agent_access(db, current_user, agent_id)
    from app.models.activity_log import AgentActivityLog

    result = await db.execute(
        select(AgentActivityLog)
        .where(
            AgentActivityLog.agent_id == agent_id,
            AgentActivityLog.action_type == "schedule_run",
        )
        .order_by(AgentActivityLog.created_at.desc())
    )
    logs = result.scalars().all()
    # Filter by schedule_id in detail_json
    history = []
    for log in logs:
        detail = log.detail_json or {}
        if detail.get("schedule_id") == str(schedule_id):
            history.append(
                {
                    "id": str(log.id),
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                    "summary": log.summary,
                    "instruction": detail.get("instruction", ""),
                    "reply": detail.get("reply", ""),
                }
            )
        if len(history) >= 20:
            break
    return history
