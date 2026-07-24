"""Deterministic idempotency keys for trigger executions."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app.models.trigger import AgentTrigger


def build_scheduled_execution_key(
    trigger: AgentTrigger,
    now: datetime,
    *,
    scheduled_for: datetime | None = None,
) -> str:
    """Build a deterministic idempotency key for non-webhook trigger runs."""
    cfg = trigger.config or {}
    trigger_type = trigger.type

    if trigger_type == "once":
        return f"once:{trigger.id}:{cfg.get('at', '')}"

    if trigger_type == "interval":
        minutes = int(cfg.get("minutes", 30) or 30)
        base = trigger.last_fired_at or trigger.created_at
        due_at = base + timedelta(minutes=minutes)
        return f"interval:{trigger.id}:{due_at.astimezone(timezone.utc).isoformat()}"

    if trigger_type == "cron":
        if scheduled_for is None:
            raise ValueError("cron execution keys require scheduled_for")
        if scheduled_for.tzinfo is None:
            raise ValueError("scheduled_for must be timezone-aware")
        canonical = scheduled_for.astimezone(timezone.utc).replace(microsecond=0)
        return f"cron:{trigger.id}:{canonical.isoformat()}"

    if trigger_type == "on_message":
        matched_message_id = str(cfg.get("_matched_message_id") or "").strip()
        if matched_message_id:
            return f"on_message:{trigger.id}:{matched_message_id}"
        matched_from = str(cfg.get("_matched_from") or "")
        matched_message = str(cfg.get("_matched_message") or "")
        digest = hashlib.sha256(f"{matched_from}\n{matched_message}".encode("utf-8")).hexdigest()
        return f"on_message:{trigger.id}:{digest}"

    if trigger_type == "poll":
        current_value = str(cfg.get("_last_value") or "")
        digest = hashlib.sha256(current_value.encode("utf-8")).hexdigest()
        return f"poll:{trigger.id}:{digest}"

    return f"{trigger_type}:{trigger.id}:{now.replace(microsecond=0).isoformat()}"
