"""Dispatch helpers for trigger executions."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.database import async_session
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.executions import (
    build_execution_runtime_trigger,
    claim_pending_trigger_executions,
)
from app.services.trigger_runtime.keys import build_scheduled_execution_key
from app.services.trigger_runtime.queue import enqueue_trigger_execution


def runtime_execution_payload(trigger: AgentTrigger) -> dict:
    """Capture ephemeral trigger evaluation context into an execution payload."""
    cfg = trigger.config or {}
    payload: dict = {}
    for key in (
        "_matched_message",
        "_matched_from",
        "_matched_message_id",
        "_matched_session_id",
        "_matched_external_event_key",
        "okr_member_id",
        "okr_member_type",
        "okr_report_date",
        "_notification_summary",
        "_origin_session_id",
        "_origin_user_id",
        "_origin_source_channel",
        "_origin_external_conv_id",
        "_origin_turn_anchor_id",
        "_origin_completion_barrier",
        "_origin_actor_ref",
        "_origin_actor_ref_type",
        "_watch_session_id",
        "_watch_source_channel",
        "_watch_actor_ref",
        "_outbound_message_id",
        "_outbound_external_message_id",
        "_correlation_mode",
        "_consume_remote",
        "_set_trigger_context",
        "_trigger_context",
        "_a2a_session_id",
    ):
        if key in cfg and cfg.get(key) is not None:
            payload[key] = cfg.get(key)
    return payload


async def enqueue_due_trigger(trigger: AgentTrigger, now: datetime) -> None:
    async with async_session() as db:
        await enqueue_trigger_execution(
            db,
            trigger=trigger,
            source=trigger.type,
            idempotency_key=build_scheduled_execution_key(trigger, now),
            payload_obj=runtime_execution_payload(trigger),
        )


InvocationKey = tuple[uuid.UUID, str]


async def claim_ready_trigger_invocations(
    now: datetime,
) -> tuple[dict[InvocationKey, list[AgentTrigger]], set[InvocationKey]]:
    """Claim executions without merging independent on_message replies.

    Scheduled reflection triggers keep their historical per-agent merge bucket.
    Each message execution receives its own bucket and is serialized later by
    the exact origin-session guard.  Combining two replies merely because they
    target the same agent/session destroys their individual idempotency boundary.
    """
    fired_by_invocation: dict[InvocationKey, list[AgentTrigger]] = {}
    force_invoke: set[InvocationKey] = set()

    claimed_executions = await claim_pending_trigger_executions()

    for execution, trigger in claimed_executions:
        runtime_trigger = build_execution_runtime_trigger(trigger, execution)
        bucket = str(execution.id) if trigger.type == "on_message" else "reflection"
        key = (trigger.agent_id, bucket)
        fired_by_invocation.setdefault(key, []).append(runtime_trigger)
        force_invoke.add(key)

    return fired_by_invocation, force_invoke
