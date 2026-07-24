"""Queue trigger executions for distributed workers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution


_IDEMPOTENCY_CONSTRAINT = "uq_trigger_execution_idempotency"


def _is_idempotency_conflict(error: IntegrityError) -> bool:
    original = getattr(error, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        if candidate is None:
            continue
        constraint_name = getattr(candidate, "constraint_name", None)
        if constraint_name == _IDEMPOTENCY_CONSTRAINT:
            return True
        diag = getattr(candidate, "diag", None)
        if getattr(diag, "constraint_name", None) == _IDEMPOTENCY_CONSTRAINT:
            return True

    # SQLite has no named-constraint diagnostics. Keep the fallback exact so
    # unrelated integrity failures are never misclassified as deduplication.
    message = str(original or "").lower()
    return (
        "unique constraint failed: "
        "trigger_executions.trigger_id, trigger_executions.idempotency_key"
    ) in message


async def enqueue_trigger_execution(
    db: AsyncSession,
    *,
    trigger: AgentTrigger,
    source: str,
    idempotency_key: str,
    payload_text: str = "",
    payload_obj: dict | None = None,
    scheduled_at: datetime | None = None,
    commit: bool = True,
) -> tuple[TriggerExecution | None, bool]:
    """Insert a generic trigger execution record."""
    canonical_scheduled_at = scheduled_at or datetime.now(timezone.utc)
    if canonical_scheduled_at.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware")
    canonical_scheduled_at = canonical_scheduled_at.astimezone(timezone.utc)
    execution = TriggerExecution(
        trigger_id=trigger.id,
        agent_id=trigger.agent_id,
        source=source,
        status="pending",
        idempotency_key=idempotency_key[:255],
        payload=payload_obj if isinstance(payload_obj, dict) else {},
        payload_text=payload_text[:8000],
        scheduled_at=canonical_scheduled_at,
    )
    try:
        async with db.begin_nested():
            db.add(execution)
            await db.flush()
        if commit:
            await db.commit()
        return execution, True
    except IntegrityError as error:
        if commit:
            await db.rollback()
        if not _is_idempotency_conflict(error):
            raise
        logger.debug(
            "[Trigger] Deduplicated execution trigger_id={} key={}",
            trigger.id,
            idempotency_key[:255],
        )
        return None, False


async def enqueue_webhook_execution(
    db: AsyncSession,
    *,
    trigger: AgentTrigger,
    body: bytes,
    payload_text: str,
    payload_obj: dict | None,
    request_headers: dict[str, str],
) -> tuple[TriggerExecution | None, bool]:
    """Insert a webhook execution record.

    Returns `(execution, created)` where `created=False` means an identical
    idempotency key already exists and the event should be treated as a no-op.
    """
    delivery_key = (
        request_headers.get("x-idempotency-key")
        or request_headers.get("x-github-delivery")
        or request_headers.get("x-request-id")
        or request_headers.get("x-event-id")
        or hashlib.sha256(body).hexdigest()
    )[:255]

    return await enqueue_trigger_execution(
        db,
        trigger=trigger,
        source="webhook",
        idempotency_key=delivery_key,
        payload_text=payload_text,
        payload_obj=payload_obj,
    )
