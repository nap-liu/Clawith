"""Trigger daemon orchestrator.

Trigger-specific evaluation and invocation behavior now lives under
`app.services.trigger_runtime`. This module owns the main loop, dedup window,
and distributed claim/invoke flow.
"""

import asyncio
import ipaddress
import json as _json
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select

from app.core.logging_config import new_trace_id
from app.database import async_session
from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.services.project_runtime_boundary import (
    is_project_agent,
    project_agent_runtime_allows,
)
from app.services.redis_lease_lock import RedisLeaseError
from app.services.trigger_runtime import (
    claim_ready_trigger_invocations,
    enqueue_due_trigger,
    mark_trigger_executions_completed,
)
from app.services.trigger_runtime.cron_schedule import (
    CronOccurrence,
    compute_next_trigger_cron_occurrence,
    format_cron_timing_context,
)
from app.services.trigger_runtime.evaluator import (
    handle_okr_collection_trigger as handle_okr_collection_trigger_runtime,
)
from app.services.trigger_runtime.evaluator import (
    handle_okr_report_trigger as handle_okr_report_trigger_runtime,
)
from app.services.trigger_runtime.evaluator import (
    mark_trigger_fired as mark_trigger_fired_runtime,
)
from app.services.trigger_runtime.evaluator import (
    mark_trigger_skipped as mark_trigger_skipped_runtime,
)
from app.services.trigger_runtime.evaluator import (
    recover_exact_on_message_events,
    recover_legacy_on_message_events,
)
from app.services.trigger_runtime.evaluator import (
    should_skip_non_workday as should_skip_non_workday_runtime,
)
from app.services.trigger_runtime.executions import renew_trigger_execution_leases
from app.services.webhook_inbox import format_webhook_inbox_context
from app.services.workload_capacity import (
    WorkloadKind,
    WorkloadOverloadedError,
    get_workload_capacity,
)

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 30  # seconds — same agent won't be invoked twice within this window
MIN_POLL_INTERVAL_MINUTES = 5  # minimum poll interval to prevent abuse

# Safety: per-agent on_message fire rate limiter
_ON_MSG_RATE_WINDOW = 3600  # 1 hour window
_ON_MSG_RATE_LIMIT = 30  # max on_message fires per agent per hour
_on_msg_fire_log: dict[uuid.UUID, list[datetime]] = {}  # agent_id -> list of fire timestamps

_last_invoke: dict[object, datetime] = {}

_A2A_WAKE_CHAIN: dict[str, int] = {}
_A2A_WAKE_CHAIN_TTL = 300
_A2A_MAX_WAKE_DEPTH = 3


def _cleanup_stale_invoke_cache():
    now = datetime.now(timezone.utc)
    stale = [k for k, v in _last_invoke.items() if (now - v).total_seconds() > DEDUP_WINDOW * 2]
    for k in stale:
        del _last_invoke[k]
    # Clean up old on_message rate limiter entries
    cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
    stale_agents = []
    for aid, timestamps in _on_msg_fire_log.items():
        _on_msg_fire_log[aid] = [t for t in timestamps if t > cutoff]
        if not _on_msg_fire_log[aid]:
            stale_agents.append(aid)
    for aid in stale_agents:
        del _on_msg_fire_log[aid]


async def _should_skip_non_workday(trigger: AgentTrigger, local_now: datetime) -> bool:
    return await should_skip_non_workday_runtime(trigger, local_now)


async def _mark_trigger_skipped(trigger_id: uuid.UUID, now: datetime) -> None:
    await mark_trigger_skipped_runtime(trigger_id, now)


async def _mark_trigger_fired(trigger_id: uuid.UUID, now: datetime) -> None:
    await mark_trigger_fired_runtime(trigger_id, now)


async def _handle_okr_report_trigger(
    trigger: AgentTrigger,
    now: datetime,
    *,
    scheduled_for: datetime | None = None,
    local_scheduled_for: datetime | None = None,
) -> bool:
    return await handle_okr_report_trigger_runtime(
        trigger,
        now,
        scheduled_for=scheduled_for,
        local_scheduled_for=local_scheduled_for,
    )


async def _handle_okr_collection_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    return await handle_okr_collection_trigger_runtime(trigger, now)


def _is_private_url(url: str) -> bool:
    """Block private/internal URLs to prevent SSRF attacks."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True

        # Block obvious private hostnames
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True

        # Try to resolve hostname and check IP
        import socket

        try:
            infos = socket.getaddrinfo(hostname, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except (socket.gaierror, ValueError):
            return True  # Cannot resolve = block

        return False
    except Exception:
        return True  # Block on any parsing error

__all__ = [name for name in globals() if not name.startswith("__")]

