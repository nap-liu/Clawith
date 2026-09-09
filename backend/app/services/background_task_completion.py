"""Task-like business outcomes, committed atomically with shared turn finalization."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.models.activity_log import AgentActivityLog
from app.models.audit import ChatMessage
from app.models.notification import Notification
from app.models.schedule import AgentSchedule
from app.models.task import Task, TaskLog
from app.services.llm.failure_outcome import render_message


def _outcome(anchor: ChatMessage, reply_row: ChatMessage | None) -> tuple[dict, str, str, dict]:
    metadata = anchor.message_meta or {}
    execution = metadata.get("background_execution") or {}
    status = str(metadata.get("turn_status") or "")
    reply = reply_row.content if reply_row else ""
    failure = dict(reply_row.message_meta or {}) if reply_row else {}
    return execution, status, reply, {key: failure[key] for key in ("error_code", "llm_failure") if key in failure}


async def finalize_task_turn(db, anchor: ChatMessage, reply_row: ChatMessage | None) -> None:
    execution, status, reply, failure = _outcome(anchor, reply_row)
    completion = execution.get("completion") or {}
    task_id = uuid.UUID(completion["task_id"])
    task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        return
    run_id = uuid.UUID(execution["reference_id"])
    run = await db.get(TaskLog, run_id)
    if run is None or run.task_id != task.id:
        return
    # No Task scanner requeues a pending task. STOP ends this anchor; a manual
    # run is a new TaskLog identity and can never revive the cancelled anchor.
    completed = status == "completed"
    task.status = "done" if completed else "pending"
    task.completed_at = datetime.now(UTC) if completed else None
    if status == "cancelled":
        log_content = render_message("background.taskCancelled")
    else:
        key = "background.taskCompleted" if completed else "background.taskFailed"
        log_content = render_message(key).format(reply=reply)
    db.add(TaskLog(task_id=task.id, content=log_content, execution_user_id=run.execution_user_id))
    db.add(AgentActivityLog(
        agent_id=anchor.agent_id, action_type="task_updated", related_id=task.id,
        summary=render_message("background.taskActivity").format(title=completion.get("title", "")[:60]),
        detail_json={
            "task_id": str(task.id), "execution_id": str(run_id),
            "task_type": completion.get("task_type"), "title": completion.get("title"),
            "reply": reply[:500], "status": status, **failure,
        },
    ))


async def finalize_schedule_turn(db, anchor: ChatMessage, reply_row: ChatMessage | None) -> None:
    execution, status, reply, failure = _outcome(anchor, reply_row)
    completion = execution.get("completion") or {}
    schedule_id = uuid.UUID(completion["schedule_id"])
    if completion.get("manual") and status == "completed":
        schedule = await db.scalar(select(AgentSchedule).where(AgentSchedule.id == schedule_id).with_for_update())
        if schedule is not None:
            schedule.last_run_at = datetime.now(UTC)
            schedule.run_count = (schedule.run_count or 0) + 1
    instruction = completion.get("instruction", "")
    db.add(AgentActivityLog(
        agent_id=anchor.agent_id, action_type="schedule_run", related_id=schedule_id,
        summary=render_message("background.scheduleActivity").format(instruction=instruction[:60]),
        detail_json={
            "schedule_id": str(schedule_id), "execution_id": execution["reference_id"],
            "instruction": instruction, "reply": reply[:500], "status": status, **failure,
        },
    ))


async def finalize_oneshot_turn(db, anchor: ChatMessage, reply_row: ChatMessage | None) -> None:
    execution, status, reply, failure = _outcome(anchor, reply_row)
    completion = execution.get("completion") or {}
    triggered_by = completion.get("triggered_by_user_id")
    agent_name = completion.get("agent_name") or ""
    if triggered_by:
        user_id = uuid.UUID(triggered_by)
        title = render_message("background.oneshotFailed").format(agent_name=agent_name)[:200]
        if status == "failed":
            db.add(Notification(
                user_id=user_id, type="system", title=title,
                body=reply[:500], link=f"/agents/{anchor.agent_id}#chat",
                ref_id=anchor.agent_id, sender_name=agent_name,
            ))
        elif status == "completed":
            await db.execute(delete(Notification).where(
                Notification.user_id == user_id, Notification.ref_id == anchor.agent_id,
                Notification.type == "system", Notification.title == title,
            ))
    db.add(AgentActivityLog(
        agent_id=anchor.agent_id, action_type="task_updated", related_id=anchor.id,
        summary=render_message("background.oneshotActivity"),
        detail_json={
            "reply": reply[:500], "status": status, "triggered_by": triggered_by,
            "execution_id": execution["reference_id"], **failure,
        },
    ))


async def finalize_heartbeat_turn(db, anchor: ChatMessage, reply_row: ChatMessage | None) -> None:
    execution, status, reply, failure = _outcome(anchor, reply_row)
    if not reply or "HEARTBEAT_OK" in reply.upper().replace(" ", "_"):
        return
    db.add(AgentActivityLog(
        agent_id=anchor.agent_id, action_type="heartbeat", related_id=anchor.id,
        summary=render_message("background.heartbeatActivity").format(reply=reply[:80]),
        detail_json={"reply": reply[:500], "status": status,
                     "execution_id": execution["reference_id"], **failure},
    ))
