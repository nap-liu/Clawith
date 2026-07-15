"""Supervision reminder service — periodically sends reminders for supervision tasks.

Checks all supervision-type tasks that are not done and sends Feishu reminders
to the target person based on the configured schedule preset.

Schedule presets: daily, every_2_days, every_3_days, weekly

Runs as a background task inside the FastAPI process.
"""

import asyncio
from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.task import Task, TaskLog
from app.models.agent import Agent

# Schedule JSON format:
# {"freq": "daily"|"weekly", "interval": N, "time": "HH:MM", "weekdays": [0-6]}
# weekdays: 0=Sun, 1=Mon, ..., 6=Sat


def _parse_schedule(remind_schedule: str) -> dict | None:
    """Parse remind_schedule — supports JSON format or legacy simple presets."""
    import json
    if not remind_schedule:
        return None
    try:
        sched = json.loads(remind_schedule)
        if isinstance(sched, dict) and "freq" in sched:
            return sched
    except (json.JSONDecodeError, TypeError):
        pass
    # Legacy simple preset fallback
    legacy_map = {
        "daily": {"freq": "daily", "interval": 1, "time": "09:00"},
        "every_2_days": {"freq": "daily", "interval": 2, "time": "09:00"},
        "every_3_days": {"freq": "daily", "interval": 3, "time": "09:00"},
        "weekly": {"freq": "weekly", "interval": 1, "time": "09:00", "weekdays": [1, 2, 3, 4, 5]},
    }
    return legacy_map.get(remind_schedule)


def _is_reminder_due(remind_schedule: str, last_reminded_at: datetime | None, now_utc: datetime) -> bool:
    """Check if a reminder is due based on the schedule config.
    
    All time calculations are anchored to now_utc (provided by tick loop).
    Default behavior is to use UTC for hour/minute checks unless a timezone is specified.
    """
    sched = _parse_schedule(remind_schedule)
    if not sched:
        return False

    freq = sched.get("freq", "daily")
    interval = sched.get("interval", 1)
    time_str = sched.get("time", "09:00")

    # Parse target hour/minute
    try:
        th, tm = map(int, time_str.split(":"))
    except Exception:
        th, tm = 9, 0

    # For now, we use UTC for the hour/minute check.
    # In the future, we should load agent.timezone and convert now_utc.
    current_time = now_utc

    # Not yet time today
    if current_time.hour < th or (current_time.hour == th and current_time.minute < tm):
        return False

    # Already past the time window (allow 60-min window)
    if current_time.hour > th or (current_time.hour == th and current_time.minute > tm + 59):
        return False

    # Weekly: check if today is a selected weekday
    if freq == "weekly":
        weekdays = sched.get("weekdays", [1, 2, 3, 4, 5])
        # Python: Monday=0, Sunday=6 → convert to our format: Sunday=0, Monday=1, ...
        py_weekday = current_time.weekday()  # Mon=0
        our_weekday = (py_weekday + 1) % 7  # Sun=0
        if our_weekday not in weekdays:
            return False

    # Check interval since last reminder
    if last_reminded_at is None:
        return True

    # Ensure both are timezone-aware for comparison
    if last_reminded_at.tzinfo is None:
        last_reminded_at = last_reminded_at.replace(tzinfo=timezone.utc)

    elapsed = now_utc - last_reminded_at
    min_interval = timedelta(days=interval) - timedelta(hours=2)  # tolerance
    return elapsed >= min_interval


async def _send_supervision_reminder(task: Task, agent_name: str):
    """Send one reminder through the canonical delivery paths.

    The task must already contain exactly one frozen canonical target ID. A
    legacy name-only task fails closed and is surfaced for operator repair.
    """
    try:
        from app.models.activity_log import AgentActivityLog
        from app.services.agent_tools import _send_channel_message, _send_message_to_agent
        from app.services.recipient_resolver import RecipientResolutionError
        from app.services.supervision_targets import resolve_supervision_target

        if (task.supervision_target_user_id is None) == (
            task.supervision_target_agent_id is None
        ):
            logger.warning(
                "Supervision task %s requires canonical target migration", task.id
            )
            async with async_session() as db:
                db.add(
                    TaskLog(
                        task_id=task.id,
                        content=(
                            "⚠️ 提醒失败：督办对象缺少唯一的 user_id/agent_id，"
                            "请修复任务后重试（migration_required）"
                        ),
                    )
                )
                await db.commit()
            return

        days_since = (datetime.now(timezone.utc) - task.created_at).days
        reminder_msg = (
            f"📋 督办提醒 — 来自 {agent_name}\n\n"
            f"事项：{task.title}\n"
        )
        if task.description:
            reminder_msg += f"说明：{task.description}\n"
        reminder_msg += f"创建于：{days_since} 天前\n"
        if task.due_date:
            reminder_msg += f"截止日期：{task.due_date.strftime('%Y-%m-%d')}\n"
        reminder_msg += "\n请及时处理，谢谢！"

        async with async_session() as db:
            try:
                target = await resolve_supervision_target(
                    db,
                    task.agent_id,
                    target_user_id=task.supervision_target_user_id,
                    target_agent_id=task.supervision_target_agent_id,
                    channel=task.supervision_channel,
                )
            except (RecipientResolutionError, ValueError) as exc:
                target_name = task.supervision_target_name or "unknown"
                db.add(
                    TaskLog(
                        task_id=task.id,
                        content=f"⚠️ 提醒失败：{exc}",
                    )
                )
                db.add(
                    AgentActivityLog(
                        agent_id=task.agent_id,
                        action_type="schedule_run",
                        summary=f"📋 督办提醒失败：{task.title} → {target_name}",
                        detail_json={
                            "task_id": str(task.id),
                            "target_user_id": str(task.supervision_target_user_id or ""),
                            "target_agent_id": str(task.supervision_target_agent_id or ""),
                            "sent": False,
                            "error": str(exc),
                        },
                        related_id=task.id,
                    )
                )
                await db.commit()
                return

            target_name = target.display_name

        if target.target_type == "agent":
            result = await _send_message_to_agent(
                task.agent_id,
                {
                    "agent_id": str(target.target_id),
                    "message": reminder_msg,
                    "msg_type": "consult",
                },
                user_id=task.created_by,
                origin_session_id=str(task.id),
            )
            send_method = "agent"
        else:
            result = await _send_channel_message(
                task.agent_id,
                {
                    "user_id": str(target.target_id),
                    "message": reminder_msg,
                    "channel": target.channel,
                },
                origin_user_id=task.created_by,
            )
            send_method = target.channel or "channel"

        sent = result.startswith("✅")

        async with async_session() as db:

            # Log result to TaskLog
            if sent:
                log = TaskLog(task_id=task.id, content=f"✅ 已向 {target_name} 发送督办提醒（{send_method}）")
            else:
                log = TaskLog(task_id=task.id, content=f"⚠️ 提醒失败：{result[:300]}")
            db.add(log)

            # Log to AgentActivityLog for Activity tab visibility
            activity = AgentActivityLog(
                agent_id=task.agent_id,
                action_type="schedule_run",
                summary=f"📋 督办提醒：{task.title} → {target_name}" + (f"（{send_method}已发送）" if sent else ""),
                detail_json={
                    "task_id": str(task.id),
                    "target_user_id": str(task.supervision_target_user_id or ""),
                    "target_agent_id": str(task.supervision_target_agent_id or ""),
                    "sent": sent,
                },
                related_id=task.id,
            )
            db.add(activity)
            await db.commit()

            logger.info(f"📋 Supervision reminder for '{task.title}' -> {target_name}, sent={sent}")

    except Exception as e:
        logger.exception(f"Supervision reminder error for task {task.id}: {e}")


async def _supervision_tick():
    """One tick: check all supervision tasks and send due reminders."""
    logger.info("[supervision] tick running...")
    from app.services.audit_logger import write_audit_log

    try:
        now = datetime.now(timezone.utc)

        async with async_session() as db:
            # Find active supervision tasks
            result = await db.execute(
                select(Task, Agent.name).join(Agent, Agent.id == Task.agent_id).where(
                    Task.type == "supervision",
                    Task.status.in_(["pending", "doing"]),
                    Task.remind_schedule.isnot(None),
                )
            )
            rows = result.all()
            logger.info(f"[supervision] found {len(rows)} supervision tasks")

            await write_audit_log("supervision_tick", {"tasks_found": len(rows)})

            for task, agent_name in rows:
                try:
                    # Get last reminder log for this task
                    log_result = await db.execute(
                        select(TaskLog)
                        .where(TaskLog.task_id == task.id)
                        .order_by(TaskLog.created_at.desc())
                        .limit(1)
                    )
                    last_log = log_result.scalar_one_or_none()
                    last_reminded = last_log.created_at if last_log else None

                    if _is_reminder_due(task.remind_schedule, last_reminded, now):
                        logger.info(f"[supervision] FIRING reminder for '{task.title}' -> {task.supervision_target_name}")
                        await write_audit_log(
                            "supervision_fire",
                            {"task_id": str(task.id), "title": task.title, "target": task.supervision_target_name},
                            agent_id=task.agent_id,
                        )
                        await _send_supervision_reminder(task, agent_name)

                except Exception as e:
                    logger.error(f"Error checking supervision task {task.id}: {e}")

    except Exception as e:
        logger.exception(f"Supervision tick error: {e}")
        await write_audit_log("supervision_error", {"error": str(e)[:300]})


async def start_supervision_reminder():
    """Start the background supervision reminder loop. Call from FastAPI startup."""
    logger.info("📋 [supervision] Reminder service started (60s tick)")
    logger.info("📋 Supervision reminder service started (60s tick)")
    while True:
        await _supervision_tick()
        await asyncio.sleep(60)
