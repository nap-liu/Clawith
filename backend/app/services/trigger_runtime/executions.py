"""Execution claiming and completion helpers for distributed triggers."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update

from app.config import get_settings
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution

settings = get_settings()


async def mark_trigger_executions_completed(execution_ids: list[uuid.UUID]) -> None:
    if not execution_ids:
        return
    async with async_session() as db:
        result = await db.execute(
            select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids))
        )
        for execution in result.scalars().all():
            execution.status = "completed"
            execution.finished_at = datetime.now(timezone.utc)
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.last_error = None
        await db.commit()


async def mark_trigger_executions_failed(execution_ids: list[uuid.UUID], error_text: str) -> None:
    if not execution_ids:
        return
    async with async_session() as db:
        result = await db.execute(
            select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids))
        )
        for execution in result.scalars().all():
            execution.status = "failed"
            execution.finished_at = datetime.now(timezone.utc)
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.last_error = error_text
        await db.commit()


async def requeue_trigger_executions(execution_ids: list[uuid.UUID], error_text: str) -> None:
    """Release retryable executions back to the durable queue.

    Exact on_message turns persist deterministic anchor/final rows, so a
    worker or transport failure can safely retry without creating a second
    model turn.  Keep the error for observability while clearing the lease.
    """
    if not execution_ids:
        return
    async with async_session() as db:
        result = await db.execute(
            select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids))
        )
        for execution in result.scalars().all():
            execution.status = "pending"
            execution.finished_at = None
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.last_error = error_text
        await db.commit()


async def claim_pending_trigger_executions(
    *,
    sources: list[str] | None = None,
    limit: int = 100,
) -> list[tuple[TriggerExecution, AgentTrigger]]:
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(minutes=5)
    claimed_pairs: list[tuple[TriggerExecution, AgentTrigger]] = []
    first_claim_counts: Counter[uuid.UUID] = Counter()
    triggers_by_id: dict[uuid.UUID, AgentTrigger] = {}
    sources = sources or ["webhook", "cron", "once", "interval", "poll", "on_message"]
    async with async_session() as db:
        result = await db.execute(
            select(TriggerExecution, AgentTrigger)
            .join(AgentTrigger, AgentTrigger.id == TriggerExecution.trigger_id)
            .where(
                TriggerExecution.source.in_(sources),
                or_(
                    and_(
                        TriggerExecution.status == "pending",
                        or_(
                            AgentTrigger.is_enabled.is_(True),
                            # A retryable on_message execution may have disabled
                            # its one-shot base trigger on the first claim.  The
                            # same execution must still be reclaimable to finish
                            # deterministic origin delivery.
                            and_(
                                TriggerExecution.source == "on_message",
                                TriggerExecution.started_at.isnot(None),
                            ),
                        ),
                    ),
                    and_(
                        TriggerExecution.status == "processing",
                        or_(
                            TriggerExecution.lease_expires_at.is_(None),
                            TriggerExecution.lease_expires_at < now,
                        ),
                    ),
                ),
            )
            .order_by(TriggerExecution.scheduled_at.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        rows = result.all()
        for execution, trigger in rows:
            # Transient marker consumed by dispatch before the objects are
            # detached.  Retries must not increment fire_count a second time.
            execution._is_first_claim = execution.started_at is None
            if execution._is_first_claim:
                first_claim_counts[trigger.id] += 1
                triggers_by_id[trigger.id] = trigger
            execution.status = "processing"
            execution.started_at = execution.started_at or now
            execution.finished_at = None
            execution.lease_owner = settings.INSTANCE_ID
            execution.lease_expires_at = lease_until
            claimed_pairs.append((execution, trigger))
        # The first lease and its base trigger transition are one transaction.
        # A worker crash can therefore cause neither an uncounted fire nor a
        # double-counted retry.
        for trigger_id, increment in first_claim_counts.items():
            apply_base_trigger_fired_state(
                triggers_by_id[trigger_id],
                now,
                fire_count_increment=increment,
            )
        await db.commit()
        for execution, trigger in claimed_pairs:
            if execution in db:
                db.expunge(execution)
            if trigger in db:
                db.expunge(trigger)
    return claimed_pairs


async def renew_trigger_execution_leases(execution_ids: list[uuid.UUID]) -> None:
    """Extend leases owned by this instance while a long LLM turn is active."""
    if not execution_ids:
        return
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(minutes=5)
    async with async_session() as db:
        await db.execute(
            update(TriggerExecution)
            .where(
                TriggerExecution.id.in_(execution_ids),
                TriggerExecution.status == "processing",
                TriggerExecution.lease_owner == settings.INSTANCE_ID,
            )
            .values(lease_expires_at=lease_until)
        )
        await db.commit()


def build_execution_runtime_trigger(trigger: AgentTrigger, execution: TriggerExecution) -> AgentTrigger:
    runtime_cfg = {
        **(trigger.config or {}),
        "_execution_id": str(execution.id),
    }
    if execution.payload:
        runtime_cfg.update(execution.payload)
    if execution.payload_text:
        runtime_cfg["_webhook_payload"] = execution.payload_text
    return AgentTrigger(
        id=trigger.id,
        agent_id=trigger.agent_id,
        name=trigger.name,
        type=trigger.type,
        config=runtime_cfg,
        reason=trigger.reason,
        focus_ref=trigger.focus_ref,
        is_enabled=trigger.is_enabled,
        last_fired_at=trigger.last_fired_at,
        fire_count=trigger.fire_count,
        max_fires=trigger.max_fires,
        cooldown_seconds=trigger.cooldown_seconds,
        is_system=trigger.is_system,
        created_at=trigger.created_at,
        expires_at=trigger.expires_at,
    )


def apply_base_trigger_fired_state(
    trigger: AgentTrigger,
    now: datetime,
    *,
    fire_count_increment: int = 1,
) -> None:
    """Single source of truth for the 'this trigger just fired' state transition.

    Applied in place when the lease/execution path claims a trigger for firing.
    Covers the stamp (last_fired_at / fire_count), single-shot auto-disable, and
    the legacy-webhook pending clear.

    The legacy-webhook clear is critical: ``_evaluate_trigger`` treats a legacy
    webhook as due whenever ``_webhook_pending`` is truthy, so if it is never
    reset the daemon re-fires the same webhook every cooldown forever (the
    runaway that burned ~1 fire/minute). The clear used to live inline in the
    daemon tick, but that block became dead code once every runtime trigger
    carries an ``_execution_id`` (the tick skips it) — so it has to happen here,
    the one place that now owns post-fire state. queue/merge webhooks advance via
    a different mechanism (``_webhook_queue`` / ``_advance_webhook_trigger``) and
    must NOT be touched here.
    """
    increment = max(0, int(fire_count_increment))
    trigger.last_fired_at = now
    trigger.fire_count = (trigger.fire_count or 0) + increment
    if trigger.type == "once":
        trigger.is_enabled = False
    if trigger.max_fires and trigger.fire_count >= trigger.max_fires:
        trigger.is_enabled = False
    if trigger.type == "webhook":
        cfg = trigger.config or {}
        if cfg.get("webhook_mode", "legacy") == "legacy":
            trigger.config = {**cfg, "_webhook_pending": False, "_webhook_payload": None}


async def mark_base_triggers_fired(trigger_ids: list[uuid.UUID], now: datetime) -> None:
    if not trigger_ids:
        return
    trigger_counts = Counter(trigger_ids)
    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.id.in_(trigger_counts))
        )
        for trigger in result.scalars().all():
            apply_base_trigger_fired_state(
                trigger,
                now,
                fire_count_increment=trigger_counts[trigger.id],
            )
        await db.commit()
