"""Stable task JSON projection for Agent-visible compatibility files."""

from __future__ import annotations

import uuid

from app.services.timezone_utils import format_datetime_for_agent, get_agent_timezone


async def serialize_tasks_for_agent(agent_id: uuid.UUID, tasks: list) -> list[dict]:
    """Keep canonical timestamps and append explicit Agent-local projections."""
    timezone_name = await get_agent_timezone(agent_id)
    return [
        {
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "description": task.description or "",
            "created_at": task.created_at.isoformat() if task.created_at else "",
            "created_at_local": format_datetime_for_agent(task.created_at, timezone_name) or "",
            "completed_at": task.completed_at.isoformat() if task.completed_at else "",
            "completed_at_local": format_datetime_for_agent(task.completed_at, timezone_name) or "",
            "effective_timezone": timezone_name,
        }
        for task in tasks
    ]
