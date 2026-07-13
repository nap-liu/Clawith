"""Trigger daemon orchestrator.

Trigger-specific evaluation and invocation behavior now lives under
`app.services.trigger_runtime`. This module owns the main loop, dedup window,
and distributed claim/invoke flow.
"""

import asyncio
import ipaddress
import json as _json
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.core.logging_config import new_trace_id
from app.database import async_session
from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.evaluator import (
    handle_okr_collection_trigger as handle_okr_collection_trigger_runtime,
    handle_okr_report_trigger as handle_okr_report_trigger_runtime,
    mark_trigger_fired as mark_trigger_fired_runtime,
    mark_trigger_skipped as mark_trigger_skipped_runtime,
    recover_exact_on_message_events,
    recover_legacy_on_message_events,
    should_skip_non_workday as should_skip_non_workday_runtime,
)
from app.services.trigger_runtime import (
    claim_ready_trigger_invocations,
    enqueue_due_trigger,
    mark_trigger_executions_completed,
    mark_trigger_executions_failed,
    requeue_trigger_executions,
)
from app.services.trigger_runtime.executions import renew_trigger_execution_leases

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 30   # seconds — same agent won't be invoked twice within this window
MIN_POLL_INTERVAL_MINUTES = 5  # minimum poll interval to prevent abuse

# Safety: per-agent on_message fire rate limiter
_ON_MSG_RATE_WINDOW = 3600  # 1 hour window
_ON_MSG_RATE_LIMIT = 30     # max on_message fires per agent per hour
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


async def _handle_okr_report_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    return await handle_okr_report_trigger_runtime(trigger, now)


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


async def _evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    """Return True if this trigger should fire right now."""
    if not trigger.is_enabled:
        return False
    if trigger.expires_at and now >= trigger.expires_at:
        # Auto-disable expired triggers
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    cfg = trigger.config or {}
    t = trigger.type
    webhook_mode = cfg.get("webhook_mode", "legacy") if t == "webhook" else None

    # Cooldown check — queue/merge webhook 绕过(串行锁保证不并发); 其余 type 行为不变
    if trigger.last_fired_at and webhook_mode not in ("queue", "merge"):
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False

    if t == "cron":
        expr = cfg.get("expr", "* * * * *")
        base = trigger.last_fired_at or trigger.created_at
        try:
            # Resolve timezone: trigger config → agent → tenant → UTC
            tz_name = cfg.get("timezone")
            if not tz_name:
                from app.services.timezone_utils import get_agent_timezone
                tz_name = await get_agent_timezone(trigger.agent_id)
            from zoneinfo import ZoneInfo
            try:
                tz = ZoneInfo(tz_name)
            except (KeyError, Exception):
                tz = ZoneInfo("UTC")
            # Evaluate cron in agent's timezone
            local_now = now.astimezone(tz)
            local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
            cron = croniter(expr, local_base)
            next_run = cron.get_next(datetime)
            if local_now >= next_run:
                if await _should_skip_non_workday(trigger, local_now):
                    await _mark_trigger_skipped(trigger.id, now)
                    logger.info(f"[Trigger] Skipped {trigger.name} on non-workday {local_now.date()}")
                    return False
                return True
            return False
        except Exception as e:
            logger.warning(f"Invalid cron expr '{expr}' for trigger {trigger.name}: {e}")
            return False

    elif t == "once":
        at_str = cfg.get("at")
        if not at_str:
            return False
        try:
            at = datetime.fromisoformat(at_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return now >= at and trigger.fire_count == 0
        except Exception:
            return False

    elif t == "interval":
        minutes = cfg.get("minutes", 30)
        base = trigger.last_fired_at or trigger.created_at
        return (now - base) >= timedelta(minutes=minutes)

    elif t == "poll":
        interval_min = max(cfg.get("interval_min", 5), MIN_POLL_INTERVAL_MINUTES)
        base = trigger.last_fired_at or trigger.created_at
        if (now - base) < timedelta(minutes=interval_min):
            return False
        # Actual HTTP poll + change detection
        return await _poll_check(trigger)

    elif t == "on_message":
        return await _check_new_agent_messages(trigger)

    elif t == "webhook":
        if webhook_mode == "legacy":
            return bool(cfg.get("_webhook_pending"))
        # queue / merge
        if cfg.get("_webhook_active"):
            since = cfg.get("_webhook_active_since")
            if since:
                try:
                    since_dt = datetime.fromisoformat(since)
                    if (now - since_dt) > timedelta(minutes=10):
                        return True   # 锁超时, 强制重处理(死锁兜底)
                except Exception:
                    pass
            return False              # 串行: 有活动 session, 等
        return len(cfg.get("_webhook_queue") or []) > 0

    return False


async def _poll_check(trigger: AgentTrigger) -> bool:
    """HTTP poll: fetch URL, extract value via json_path, detect change.
    
    Persists _last_value into the trigger's config JSONB so it survives
    across process restarts.
    """
    import httpx
    cfg = trigger.config or {}
    url = cfg.get("url")
    if not url:
        return False

    # SSRF protection: block private/internal URLs
    if _is_private_url(url):
        logger.warning(f"Poll blocked for trigger {trigger.name}: private/internal URL '{url}'")
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(cfg.get("method", "GET"), url, headers=cfg.get("headers", {}))
            resp.raise_for_status()

        data = resp.json()
        json_path = cfg.get("json_path", "$")
        current_value = _extract_json_path(data, json_path)
        current_str = str(current_value)

        fire_on = cfg.get("fire_on", "change")
        should_fire = False

        if fire_on == "match":
            should_fire = current_str == str(cfg.get("match_value", ""))
        else:  # "change"
            last_value = cfg.get("_last_value")
            # First poll — don't fire, just record baseline
            if last_value is None:
                should_fire = False
            else:
                should_fire = current_str != last_value

        # Persist _last_value to DB so it survives restarts
        cfg["_last_value"] = current_str
        try:
            from sqlalchemy import update
            async with async_session() as db:
                await db.execute(
                    update(AgentTrigger)
                    .where(AgentTrigger.id == trigger.id)
                    .values(config=cfg)
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist poll _last_value for {trigger.name}: {e}")

        return should_fire

    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.name}: {e}")
        return False


def _extract_json_path(data, path: str):
    """Simple JSONPath extraction: $.key.subkey → data['key']['subkey']."""
    if path == "$" or not path:
        return data
    parts = path.lstrip("$.").split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


async def _check_new_agent_messages(trigger: AgentTrigger) -> bool:
    """Production on_message matcher; runtime module is the single implementation."""
    if (trigger.config or {}).get("_watch_session_id"):
        # Exact subscriptions enqueue every missed durable event directly.  The
        # ordinary evaluator must return False or the tick would enqueue a
        # second synthetic execution for only one mutated message snapshot.
        await recover_exact_on_message_events(trigger)
        return False
    await recover_legacy_on_message_events(trigger)
    return False


async def _legacy_check_new_agent_messages(trigger: AgentTrigger) -> bool:
    """Check if there are new messages matching this trigger.
    
    Supports two modes:
    - from_agent_name: check for agent-to-agent messages
    - from_user_name: check for human user messages (Feishu/Slack/Discord)
    
    Stores the actual message content in trigger.config['_matched_message']
    so the invocation context can include it.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    cfg = trigger.config or {}
    from_agent_name = cfg.get("from_agent_name")
    from_user_name = cfg.get("from_user_name")

    if not from_agent_name and not from_user_name:
        return False

    since = trigger.last_fired_at or trigger.created_at
    # Use _since_ts snapshot from trigger creation (set by _handle_set_trigger)
    # This is more precise than the old 5-minute lookback which caused false positives
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at
        # No _since_ts and no last_fired_at → use trigger.created_at (no lookback)

    try:
        async with async_session() as db:
            if from_agent_name:
                # --- Agent-to-agent message check (existing logic) ---
                from app.models.participant import Participant
                from app.models.agent import Agent as AgentModel
                safe_agent_name = from_agent_name.replace("%", "").replace("_", r"\_")
                agent_r = await db.execute(
                    select(AgentModel).where(AgentModel.name.ilike(f"%{safe_agent_name}%"))
                )
                source_agent = agent_r.scalars().first()
                if not source_agent:
                    return False

                result = await db.execute(
                    select(Participant.id).where(
                        Participant.type == "agent",
                        Participant.ref_id == source_agent.id,
                    )
                )
                from_participant = result.scalar_one_or_none()
                if not from_participant:
                    return False

                from sqlalchemy import cast as sa_cast, String as SaString
                result = await db.execute(
                    select(ChatMessage).join(
                        ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                    ).where(
                        ChatMessage.participant_id == from_participant,
                        ChatMessage.created_at > since,
                        ChatMessage.role == "assistant",
                    ).order_by(ChatMessage.created_at.desc()).limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = from_agent_name
                return True

            elif from_user_name:
                # --- Human user message check (Feishu/Slack/Discord) ---
                # Find sessions for this agent from external channels
                from sqlalchemy import cast as sa_cast, String as SaString
                from app.models.agent import Agent as AgentModel

                # 0. Get agent for tenant scoping
                agent_r = await db.execute(select(AgentModel).where(AgentModel.id == trigger.agent_id))
                agent = agent_r.scalar_one_or_none()

                # Look up user by display name or username within tenant
                from sqlalchemy import or_
                from app.models.user import User, Identity
                safe_user_name = from_user_name.replace("%", "").replace("_", r"\_")
                query = (
                    select(User)
                    .join(User.identity)
                    .where(
                        or_(
                            User.display_name.ilike(f"%{safe_user_name}%"),
                            Identity.username.ilike(f"%{safe_user_name}%"),
                        )
                    )
                )
                if agent and agent.tenant_id:
                    query = query.where(User.tenant_id == agent.tenant_id)
                
                user_r = await db.execute(query)
                target_user = user_r.scalars().first()

                if target_user:
                    # Find channel sessions for this user with this agent
                    result = await db.execute(
                        select(ChatMessage).join(
                            ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                        ).where(
                            ChatSession.agent_id == trigger.agent_id,
                            ChatSession.user_id == target_user.id,
                            ChatSession.source_channel.in_(["feishu", "slack", "discord", "web"]),
                            ChatMessage.role == "user",
                            ChatMessage.created_at > since,
                        ).order_by(ChatMessage.created_at.desc()).limit(1)
                    )
                else:
                    # Fallback: search by session title or message content containing the target name
                    result = await db.execute(
                        select(ChatMessage).join(
                            ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString)
                        ).where(
                            ChatSession.agent_id == trigger.agent_id,
                            ChatSession.source_channel.in_(["feishu", "slack", "discord", "web"]),
                            ChatMessage.role == "user",
                            ChatMessage.created_at > since,
                            or_(
                                ChatSession.title.ilike(f"%{safe_user_name}%"),
                                ChatMessage.content.ilike(f"%{safe_user_name}%"),
                            ),
                        ).order_by(ChatMessage.created_at.desc()).limit(1)
                    )

                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = from_user_name
                return True

    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.name}: {e}")
        return False


# ── Agent Invocation ────────────────────────────────────────────────

async def _resolve_trigger_delivery_target(agent: Agent, triggers: list[AgentTrigger]) -> dict | None:
    """Resolve where a trigger result should be delivered.

    Priority:
    1. Explicit A2A callback session
    2. Exact originating ChatSession for interactive triggers
    3. Pure trigger/reflection context → no user-facing delivery

    Never substitute a primary platform session.  ``source_channel`` describes
    where the continuation belongs; it is not a hint from which another session
    may be selected.
    """
    from app.models.chat_session import ChatSession

    # Synthetic A2A wake triggers already carry the callback session explicitly.
    for trigger in triggers:
        cfg = trigger.config or {}
        a2a_sid = cfg.get("_a2a_session_id")
        if a2a_sid:
            try:
                async with async_session() as db:
                    session = await db.get(ChatSession, uuid.UUID(a2a_sid))
                    if not session:
                        return None
                    return {
                        "kind": "session",
                        "session_id": str(session.id),
                        "owner_user_id": str(session.user_id),
                        "source_channel": session.source_channel,
                    }
            except Exception:
                return None

    origin_cfg = None
    for trigger in triggers:
        cfg = trigger.config or {}
        if cfg.get("_origin_session_id") or cfg.get("_origin_user_id"):
            origin_cfg = cfg
            break

    if not origin_cfg:
        return None

    origin_source_channel = str(origin_cfg.get("_origin_source_channel") or "").strip()
    origin_session_id = origin_cfg.get("_origin_session_id")
    origin_user_id = origin_cfg.get("_origin_user_id")
    origin_external_conv_id = origin_cfg.get("_origin_external_conv_id")

    if origin_session_id:
        try:
            async with async_session() as db:
                session = await db.get(ChatSession, uuid.UUID(origin_session_id))
                if not session or session.agent_id != agent.id:
                    return None
                if origin_source_channel and session.source_channel != origin_source_channel:
                    return None
                if origin_external_conv_id is not None and session.external_conv_id != origin_external_conv_id:
                    # /new archives the old row by changing external_conv_id.  A
                    # pending continuation must not silently jump generations.
                    return None
                return {
                    "kind": "session",
                    "session_id": str(session.id),
                    "owner_user_id": str(origin_user_id or session.user_id),
                    "source_channel": session.source_channel,
                    "external_conv_id": session.external_conv_id,
                    "is_group": bool(session.is_group),
                }
        except Exception:
            return None

    return None


# ── Webhook queue/merge fire helpers ────────────────────────────────────────


def _merge_webhook_payloads(queue: list[str]) -> str:
    """Join queued webhook payloads into a single numbered block.

    Format: ``--- [1] ---\\n{p1}\\n--- [2] ---\\n{p2}``. Truncation to 2000
    chars is applied by the caller (after the header is computed).
    """
    return "\n".join(f"--- [{i + 1}] ---\n{p}" for i, p in enumerate(queue))


def _audit_webhook_failed(db, agent_id, name, detail):
    """Best-effort audit row when a queue/merge webhook session failed.

    D6: a failed session still counts as done (the entry is popped/dropped),
    but we record the loss so the user can see it.
    """
    try:
        from app.models.audit import AuditLog
        db.add(AuditLog(
            agent_id=agent_id,
            action="webhook_session_failed",
            details={"trigger_name": name, "payload": str(detail)[:2000]},
        ))
    except Exception:
        pass


def _advance_webhook_trigger(db, trig: AgentTrigger, reply) -> None:
    """Advance a queue/merge webhook trigger after its session finished.

    queue → pop the head; merge → drop the consumed batch
    (``_webhook_batch_size`` recorded at lock time). Failure (empty/None
    reply) still advances (D6) but writes an audit row. Always releases the
    serial lock (``_webhook_active``) so the 10-min deadlock fallback stays
    safe. Mutates ``trig.config`` in place; caller commits.
    """
    if not trig.config:
        return
    wmode = trig.config.get("webhook_mode", "legacy")
    if wmode not in ("queue", "merge"):
        return
    q = list(trig.config.get("_webhook_queue") or [])
    failed = (reply is None) or (isinstance(reply, str) and reply.strip() == "")
    if wmode == "queue":
        if q:
            done = q.pop(0)
            if failed:
                _audit_webhook_failed(db, trig.agent_id, trig.name, done)
    else:  # merge
        n = trig.config.get("_webhook_batch_size", len(q))
        q = q[n:]
        if failed:
            _audit_webhook_failed(db, trig.agent_id, trig.name, f"batch={n}")
    new_cfg = {
        **trig.config,
        "_webhook_queue": q,
        "_webhook_active": False,
        "_webhook_active_since": None,
    }
    new_cfg.pop("_webhook_batch_size", None)
    trig.config = new_cfg


_ONMESSAGE_TURN_NAMESPACE = uuid.UUID("1cb1fc5c-c7c4-4f02-aa83-fab956557622")


class RetryableOnMessageError(RuntimeError):
    """A durable on_message turn completed locally but delivery should retry."""


async def _resume_origin_session_for_on_message(agent_id: uuid.UUID, trigger: AgentTrigger) -> None:
    """Start one real event turn inside the exact originating ChatSession.

    An on_message trigger is a subscription.  The inbound row can therefore
    match several triggers, but every (trigger, inbound event) execution gets
    its own idempotent event turn carrying the trigger's arm-time context.
    The original send turn is never resumed or reused as the new anchor.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.services.channel_dispatch import ChannelReactions, run_channel_message
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import (
        load_recoverable_history_for_turn,
        persist_assistant_reply_row,
    )
    from app.services.turn_runtime import deliver_recovered_reply_to_origin

    cfg = trigger.config if isinstance(trigger.config, dict) else {}
    execution_id = uuid.UUID(str(cfg["_execution_id"]))
    origin_id = uuid.UUID(str(cfg["_origin_session_id"]))
    matched_id = uuid.UUID(str(cfg["_matched_message_id"]))
    anchor_id = uuid.uuid5(_ONMESSAGE_TURN_NAMESPACE, f"anchor:{execution_id}")
    final_id = uuid.uuid5(_ONMESSAGE_TURN_NAMESPACE, f"final:{execution_id}")

    async def _work() -> str:
        async with async_session() as db:
            origin = await db.get(ChatSession, origin_id)
            matched = await db.get(ChatMessage, matched_id)
            if origin is None or matched is None:
                raise RuntimeError("on_message origin or matched message no longer exists")
            owns_origin = origin.agent_id == agent_id or (
                origin.source_channel == "agent"
                and agent_id in {origin.agent_id, origin.peer_agent_id}
            )
            if not owns_origin:
                raise RuntimeError("on_message origin session belongs to another agent")
            expected_channel = str(cfg.get("_origin_source_channel") or "").strip()
            if expected_channel and origin.source_channel != expected_channel:
                raise RuntimeError("on_message origin source channel changed")
            if (
                "_origin_external_conv_id" in cfg
                and origin.external_conv_id != cfg.get("_origin_external_conv_id")
            ):
                raise RuntimeError("on_message origin session generation changed")
            expected_watch = str(
                cfg.get("_watch_session_id") or cfg.get("_matched_session_id") or ""
            )
            if expected_watch and matched.conversation_id != expected_watch:
                raise RuntimeError("on_message matched message belongs to another remote session")

            # The subscription is armed from inside an ordinary LLM/tool turn.
            # A fast remote reply may arrive before that origin turn persists
            # its final assistant row.  Use the durable turn completion marker
            # as a cross-process barrier so the new event turn cannot interleave
            # with the turn that created it.
            origin_turn_anchor_raw = cfg.get("_origin_turn_anchor_id")
            if origin_turn_anchor_raw:
                try:
                    origin_turn_anchor_id = uuid.UUID(str(origin_turn_anchor_raw))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("on_message origin turn anchor is invalid") from exc
                origin_turn_anchor = await db.get(ChatMessage, origin_turn_anchor_id)
                if (
                    origin_turn_anchor is None
                    or origin_turn_anchor.conversation_id != str(origin.id)
                ):
                    raise RuntimeError("on_message origin turn anchor changed")
                completion_query = select(ChatMessage.id).where(
                    ChatMessage.conversation_id == str(origin.id),
                    ChatMessage.role == "assistant",
                    ChatMessage.message_meta["turn_anchor_id"].as_string()
                    == str(origin_turn_anchor_id),
                    ChatMessage.message_meta["turn_status"].as_string()
                    == "completed",
                )
                completion_id = (
                    await db.execute(completion_query.order_by(ChatMessage.created_at.asc()).limit(1))
                ).scalar_one_or_none()
                if completion_id is None:
                    raise RetryableOnMessageError(
                        "on_message origin turn has not completed yet"
                    )

            # Validate the immutable origin envelope before every retry.  A
            # persisted final from an older session generation must never be
            # delivered after /new rotates the external conversation id.
            existing_final = await db.get(ChatMessage, final_id)
            if existing_final is not None:
                return existing_final.content

            owner_user_id = origin.user_id
            configured_user = cfg.get("_origin_user_id")
            if configured_user:
                try:
                    owner_user_id = uuid.UUID(str(configured_user))
                except (TypeError, ValueError):
                    pass

            agent = await db.get(Agent, agent_id)
            if agent is None:
                raise RuntimeError("on_message agent no longer exists")
            from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE

            history_agent_id = origin.agent_id if origin.source_channel == "agent" else agent_id
            trigger_context = cfg.get("_trigger_context") or cfg.get("_set_trigger_context") or {
                "name": trigger.name,
                "type": trigger.type,
                "reason": trigger.reason,
                "focus_ref": trigger.focus_ref or "",
                "config": {
                    key: value for key, value in cfg.items() if not str(key).startswith("_")
                },
            }
            wake_content = (
                "<on-message-event>\n"
                "This event wakes the exact session that created the subscription.\n"
                f"Original set_trigger context: {_json.dumps(trigger_context, ensure_ascii=False)}\n"
                f"Remote channel: {cfg.get('_watch_source_channel') or 'unknown'}\n"
                f"Remote session: {matched.conversation_id}\n"
                f"Sender: {cfg.get('_matched_from') or 'message sender'}\n"
                f"Reply:\n{matched.content}\n"
                "Handle the reply according to the original trigger context.\n"
                "</on-message-event>"
            )

            anchor = await db.get(ChatMessage, anchor_id)
            if anchor is None:
                anchor = ChatMessage(
                    id=anchor_id,
                    agent_id=history_agent_id,
                    user_id=owner_user_id,
                    role="user",
                    content=wake_content,
                    conversation_id=str(origin.id),
                    external_event_key=(
                        f"onmessage-wake:{trigger.id}:{matched.id}"[:500]
                    ),
                    message_meta={
                        "kind": "on_message_event",
                        "trigger_id": str(trigger.id),
                        "trigger_execution_id": str(execution_id),
                        "matched_message_id": str(matched.id),
                        "remote_session_id": matched.conversation_id,
                        "trigger_context": trigger_context,
                    },
                )
                db.add(anchor)
                origin.last_message_at = datetime.now(timezone.utc)
                await db.commit()

            history = await load_recoverable_history_for_turn(
                db,
                agent_id=history_agent_id,
                conversation_id=str(origin.id),
                turn_anchor_id=anchor_id,
                ctx_size=agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE,
                is_group=bool(origin.is_group),
            )
            reply = await _call_agent_llm(
                db,
                agent_id,
                "",
                session_id=str(origin.id),
                user_id=owner_user_id,
                history=history,
                is_group=bool(origin.is_group),
                recovery_hint=None,
                continue_turn=True,
                recovery_mode=True,
                turn_anchor_id=anchor_id,
                storage_agent_id=history_agent_id,
            )
            if reply and reply.strip():
                final_meta = {
                    "kind": "on_message_final",
                    "trigger_execution_id": str(execution_id),
                    "matched_message_id": str(matched_id),
                }
                origin_agent_participant = None
                if origin.source_channel == "agent":
                    from app.models.participant import Participant

                    origin_agent_participant = (
                        await db.execute(
                            select(Participant).where(
                                Participant.type == "agent",
                                Participant.ref_id == agent_id,
                            )
                        )
                    ).scalar_one_or_none()
                    final_meta.update(
                        {
                            "direction": "inbound",
                            "source_channel": "agent",
                            "actor_ref": str(
                                origin_agent_participant.id
                                if origin_agent_participant is not None
                                else agent_id
                            ),
                        }
                    )
                await persist_assistant_reply_row(
                    db,
                    agent_id=history_agent_id,
                    user_id=owner_user_id,
                    conversation_id=str(origin.id),
                    content=reply,
                    message_id=final_id,
                    message_meta=final_meta,
                    turn_anchor_id=anchor_id,
                )
                if origin.source_channel == "agent":
                    final_row = await db.get(ChatMessage, final_id)
                    if final_row is not None:
                        if origin_agent_participant is not None:
                            final_row.participant_id = origin_agent_participant.id
                        from app.services.trigger_runtime.evaluator import (
                            match_incoming_chat_message,
                        )

                        await match_incoming_chat_message(db, final_row, origin)
                origin_row = await db.get(ChatSession, origin.id)
                if origin_row is not None:
                    origin_row.last_message_at = datetime.now(timezone.utc)
            await db.commit()
            return reply

    async def _work_and_deliver() -> str:
        """Keep turn creation and origin transport ordering in one session lock."""
        reply = await _work()
        if not reply or not reply.strip():
            return reply

        async with async_session() as db:
            final_row = await db.get(ChatMessage, final_id)
            final_meta = (
                final_row.message_meta
                if final_row is not None and isinstance(final_row.message_meta, dict)
                else {}
            )
            if final_meta.get("origin_delivery_status") == "delivered":
                return reply

        delivered = await deliver_recovered_reply_to_origin(
            agent_id=agent_id,
            conversation_id=str(origin_id),
            reply=reply,
            origin_actor_ref=str(cfg.get("_origin_actor_ref") or "") or None,
            origin_actor_ref_type=str(cfg.get("_origin_actor_ref_type") or "") or None,
            require_transport=True,
            expected_source_channel=str(cfg.get("_origin_source_channel") or "") or None,
            expected_external_conv_id=cfg.get("_origin_external_conv_id"),
            validate_external_conv_id="_origin_external_conv_id" in cfg,
        )
        if not delivered:
            raise RetryableOnMessageError("on_message origin delivery failed")
        try:
            async with async_session() as db:
                final_row = await db.get(ChatMessage, final_id)
                if final_row is not None:
                    final_meta = final_row.message_meta if isinstance(final_row.message_meta, dict) else {}
                    final_row.message_meta = {
                        **final_meta,
                        "origin_delivery_status": "delivered",
                        "origin_delivered_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await db.commit()
        except Exception as exc:
            raise RetryableOnMessageError(
                "on_message delivery succeeded but its durable receipt was not recorded"
            ) from exc
        return reply

    await run_channel_message(
        str(origin_id),
        is_command=False,
        reactions=ChannelReactions(),
        work=_work_and_deliver,
        distributed=True,
    )


async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTrigger]):
    """Invoke an agent with context from one or more fired triggers.

    Creates a Reflection Session and calls the LLM.
    """
    from app.services.llm import call_llm
    from app.models.llm import LLMModel
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.audit_logger import write_audit_log

    # Each runtime trigger carries the id of the leased TriggerExecution it was
    # claimed from (build_execution_runtime_trigger injects `_execution_id`). The
    # lease is held in status="processing" with a 5-minute expiry; if we never
    # finalize it, claim_pending_trigger_executions re-grabs the expired lease and
    # the trigger re-fires forever. Finalize every claimed execution below.
    execution_ids: list[uuid.UUID] = []
    for _t in triggers:
        _cfg = _t.config if isinstance(_t.config, dict) else {}
        _eid = _cfg.get("_execution_id")
        if _eid:
            try:
                execution_ids.append(uuid.UUID(str(_eid)))
            except (ValueError, TypeError):
                pass
    invocation_error: str | None = None
    invocation_retryable = False
    lease_heartbeat_task: asyncio.Task | None = None

    if execution_ids:
        async def _lease_heartbeat() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    await renew_trigger_execution_leases(execution_ids)
                except Exception as exc:
                    logger.warning("Failed to renew trigger execution leases %s: %s", execution_ids, exc)

        lease_heartbeat_task = asyncio.create_task(_lease_heartbeat())

    try:
        if (
            len(triggers) == 1
            and triggers[0].type == "on_message"
            and (triggers[0].config or {}).get("_origin_session_id")
            and (triggers[0].config or {}).get("_matched_message_id")
            and (triggers[0].config or {}).get("_execution_id")
        ):
            await _resume_origin_session_for_on_message(agent_id, triggers[0])
            return

        async with async_session() as db:
            # Load agent
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent or agent.is_expired:
                return

            # Load LLM model
            if not agent.primary_model_id:
                logger.warning(f"Agent {agent.name} has no LLM model, skipping trigger invocation")
                return
            result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
            model = result.scalar_one_or_none()
            if not model:
                return
            # Skip invocation if model is disabled by admin
            if not model.enabled:
                logger.warning(f"Agent {agent.name}'s model {model.model} is disabled, skipping trigger invocation")
                return

            # Build trigger context. Keep this model-facing prompt in English so
            # autonomous wakeups behave consistently across UI locales.
            context_parts = []
            trigger_names = []
            for t in triggers:
                part = f"Trigger: {t.name} ({t.type})\nReason: {t.reason}"
                if t.name == "daily_okr_collection":
                    part += (
                        "\nExecution requirements: First call get_okr_settings to confirm whether daily report collection is enabled. "
                        "If it is enabled, only contact members and digital employees in your relationship network to collect today's final daily reports, "
                        "then organize them into a formal daily report no longer than 2000 characters. "
                        "If it is disabled, state that no action is needed and stop."
                    )
                elif t.name in ("daily_okr_report", "weekly_okr_report", "monthly_okr_report"):
                    part += (
                        "\nExecution requirements: This company-level report is generated automatically by the system. "
                        "If you are awakened, only add necessary clarification. Do not start another member collection round."
                    )
                elif t.name == "biweekly_okr_checkin":
                    part += (
                        "\nExecution requirements: First call get_okr_settings to confirm whether OKR is enabled. "
                        "If enabled, check the current-cycle company and member OKRs, then proactively remind members who have not set OKRs or whose progress is lagging. "
                        "If disabled, state that no action is needed and stop."
                    )
                elif t.name == "monthly_okr_report":
                    part += (
                        "\nExecution requirements: First call get_okr_settings to confirm whether OKR is enabled. "
                        "If enabled, call generate_monthly_okr_report to generate the OKR monthly report for the month that just ended, "
                        "then send it to admins or publish it to Plaza. If disabled, state that no action is needed and stop."
                    )
                if t.focus_ref:
                    part += f"\nRelated Focus: {t.focus_ref}"
                # Include matched message for on_message triggers
                cfg = t.config or {}
                if t.type == "on_message" and cfg.get("_matched_message"):
                    part += f"\nMatched message from {cfg.get('_matched_from', '?')}:\n\"{cfg['_matched_message'][:500]}\""
                if t.type == "on_message" and cfg.get("okr_member_id") and cfg.get("okr_report_date"):
                    part += (
                        "\nExecution requirements: This is a daily-report reply ingestion event."
                        f"\n1. Organize the other party's reply into a final daily report no longer than 2000 characters."
                        f"\n2. Immediately call upsert_member_daily_report(report_date=\"{cfg['okr_report_date']}\", "
                        f"member_type=\"{cfg.get('okr_member_type', 'user')}\", "
                        f"member_id=\"{cfg['okr_member_id']}\", content=\"<organized daily report>\")."
                        "\n3. After the tool call succeeds, send a brief confirmation that you received and recorded it."
                        "\n4. Do not only confirm without calling the tool, and do not store the raw long conversation verbatim as the daily report."
                    )
                # Include webhook payload (by mode)
                if t.type == "webhook":
                    wmode = cfg.get("webhook_mode", "legacy")
                    if wmode == "legacy":
                        payload_str = cfg.get("_webhook_payload")
                        if payload_str:
                            if len(payload_str) > 2000:
                                payload_str = payload_str[:2000] + "... (truncated)"
                            part += f"\nWebhook Payload:\n{payload_str}"
                    elif wmode == "queue":
                        q = cfg.get("_webhook_queue") or []
                        if q:
                            payload_str = q[0]
                            if len(payload_str) > 2000:
                                payload_str = payload_str[:2000] + "... (truncated)"
                            part += f"\nWebhook Payload:\n{payload_str}"
                    elif wmode == "merge":
                        # B1 fix: render the SAME batch the finally will delete —
                        # read fresh queue + recorded batch_size, not the T0 tick
                        # snapshot. FIFO guarantees queue[:batch_size] is stable
                        # between this build and _advance_webhook_trigger, so the
                        # rendered set == the deleted set (late arrivals only append
                        # to the tail → never silently dropped).
                        async with async_session() as _wdb:
                            _wres = await _wdb.execute(
                                select(AgentTrigger).where(AgentTrigger.id == t.id)
                            )
                            _wtrig = _wres.scalar_one_or_none()
                        _fresh_cfg = (_wtrig.config if _wtrig else cfg) or {}
                        _q = _fresh_cfg.get("_webhook_queue") or []
                        _bs = _fresh_cfg.get("_webhook_batch_size", len(_q))
                        _batch = _q[:_bs]
                        if _batch:
                            merged = _merge_webhook_payloads(_batch)
                            if len(merged) > 2000:
                                merged = merged[:2000] + "... (truncated)"
                            part += f"\nWebhook Payload (merged, {len(_batch)} entries):\n{merged}"
                context_parts.append(part)
                trigger_names.append(t.name)

            trigger_context = (
                "===== Wake Context =====\n"
                f"Wake source: trigger ({'multiple triggers fired together' if len(triggers) > 1 else 'trigger fired'})\n\n"
                + "\n---\n".join(context_parts)
                + "\n========================"
            )

            # Create Reflection Session
            title = f"🤖 Reflection: {', '.join(trigger_names)}"
            # Find agent's participant
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            session = ChatSession(
                agent_id=agent_id,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                title=title[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id

            # Messages: trigger context only (call_llm builds system prompt internally)
            messages = [
                {"role": "user", "content": trigger_context},
            ]

            # Store trigger context as a message in the session
            db.add(ChatMessage(
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="user",
                content=trigger_context,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
            ))
            await db.commit()
            # Cache participant ID for callbacks
            agent_participant_id = agent_participant.id if agent_participant else None

        # Call LLM (outside the DB session to avoid long transactions)
        collected_content = []
        collected_thinking = []
        delivered_platform_message_via_tool = False

        async def on_chunk(text):
            collected_content.append(text)

        # Collect reasoning/thinking for UI persistence — shown when a human
        # views the session, NEVER fed back into the LLM. Mirrors web chat.
        async def on_thinking(text):
            collected_thinking.append(text)

        # Persist tool calls into Reflection Session for Reflections visibility
        async def on_tool_call(data):
            nonlocal delivered_platform_message_via_tool
            try:
                tool_name = data.get("name")
                tool_status = data.get("status")
                if tool_status == "done" and tool_name == "send_platform_message":
                    result_text = str(data.get("result", ""))
                    if result_text.startswith("✅"):
                        delivered_platform_message_via_tool = True

                async with async_session() as _tc_db:
                    if data["status"] == "running":
                        # Store args RAW — this row is replayed into the LLM context;
                        # masking here poisons the model (it copies "******" back into
                        # tool calls). Secrets are masked at output boundaries only.
                        _tc_db.add(ChatMessage(
                            agent_id=agent_id,
                            conversation_id=str(session_id),
                            role="tool_call",
                            content=_json.dumps({"name": data["name"], "args": data.get("args")}, ensure_ascii=False, default=str),
                            user_id=agent.creator_id,
                            participant_id=agent_participant_id,
                        ))
                    elif data["status"] == "done":
                        # data["result"] is already the bounded llm_view
                        # from tool_output_store.finalize_tool_output.
                        result_str = str(data.get("result", ""))
                        _tc_db.add(ChatMessage(
                            agent_id=agent_id,
                            conversation_id=str(session_id),
                            role="tool_call",
                            content=_json.dumps({"name": data["name"], "result": result_str}, ensure_ascii=False, default=str),
                            user_id=agent.creator_id,
                            participant_id=agent_participant_id,
                        ))
                    await _tc_db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist tool call for trigger session: {e}")

        # reply is initialized so the finally (and post-LLM code) can reference
        # it even if call_llm raises. The finally ALWAYS advances any queue/merge
        # webhook trigger + releases the serial lock — success, empty reply, or
        # exception (D6: failure still counts as done). This is what keeps the
        # 10-min deadlock fallback in _evaluate_trigger safe.
        reply = None
        try:
            reply = await call_llm(
                model=model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=agent_id,
                user_id=agent.creator_id,
                session_id=str(session_id),
                on_chunk=on_chunk,
                on_tool_call=on_tool_call,
                on_thinking=on_thinking,
                # A2A wake uses the agent's own max_tool_rounds setting (no override)
            )
        finally:
            if any(t.type == "webhook" for t in triggers):
                try:
                    async with async_session() as _db:
                        for _t in triggers:
                            if _t.type != "webhook":
                                continue
                            _res = await _db.execute(
                                select(AgentTrigger).where(AgentTrigger.id == _t.id)
                            )
                            _trig = _res.scalar_one_or_none()
                            if not _trig:
                                continue
                            _advance_webhook_trigger(_db, _trig, reply)
                        await _db.commit()
                except Exception as _e:
                    logger.warning(f"Failed to advance webhook queue after session: {_e}")

        # Cap the turn's accumulated thinking once; reused by all assistant rows
        # persisted below (Reflection / A2A mirror / delivery). UI-only field.
        from app.services.chat_history import cap_thinking
        _capped_thinking = cap_thinking("".join(collected_thinking))

        # Save assistant reply to Reflection session
        async with async_session() as db:
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            db.add(ChatMessage(
                agent_id=agent_id,
                conversation_id=str(session_id),
                role="assistant",
                content=reply or "".join(collected_content),
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                thinking=_capped_thinking,
            ))

            # NOTE: trigger state (last_fired_at, fire_count, auto-disable)
            # is already updated in _tick() BEFORE this task was launched,
            # to prevent race-condition duplicate fires.

            await db.commit()

        # Compute final reply text once
        final_reply = reply or "".join(collected_content)

        # ── Save reply to A2A session if this was an agent-to-agent wake ──
        # This makes the target agent's reply visible in the A2A chat history
        for t in triggers:
            a2a_sid = (t.config or {}).get("_a2a_session_id")
            if a2a_sid and final_reply:
                try:
                    async with async_session() as db:
                        from app.models.participant import Participant as _P
                        _p_r = await db.execute(select(_P).where(_P.type == "agent", _P.ref_id == agent_id))
                        _p = _p_r.scalar_one_or_none()
                        from app.models.chat_session import ChatSession as _CS
                        _cs_r = await db.execute(select(_CS).where(_CS.id == uuid.UUID(a2a_sid)))
                        _cs = _cs_r.scalar_one_or_none()
                        _source_execution_id = str((t.config or {}).get("_execution_id") or "").strip()
                        _reply_id = (
                            uuid.uuid5(
                                _ONMESSAGE_TURN_NAMESPACE,
                                f"a2a-reply:{_source_execution_id}",
                            )
                            if _source_execution_id
                            else uuid.uuid4()
                        )
                        _existing_reply = await db.get(ChatMessage, _reply_id)
                        if _existing_reply is not None:
                            logger.info(
                                "[A2A] Reply already persisted for execution %s",
                                _source_execution_id,
                            )
                            break
                        reply_row = ChatMessage(
                            id=_reply_id,
                            agent_id=_cs.agent_id if _cs else agent_id,
                            conversation_id=a2a_sid,
                            role="assistant",
                            content=final_reply,
                            user_id=agent.creator_id,
                            participant_id=_p.id if _p else None,
                            thinking=_capped_thinking,
                            external_event_key=(
                                f"a2a-inbound:{_source_execution_id}"[:500]
                                if _source_execution_id
                                else None
                            ),
                            message_meta={
                                "direction": "inbound",
                                "source_channel": "agent",
                                "actor_ref": str(_p.id if _p else agent_id),
                            },
                        )
                        db.add(reply_row)
                        # Update session timestamp
                        if _cs:
                            _cs.last_message_at = datetime.now(timezone.utc)
                            await db.flush()
                            from app.services.trigger_runtime.evaluator import (
                                match_incoming_chat_message,
                            )

                            await match_incoming_chat_message(db, reply_row, _cs)
                        await db.commit()
                        logger.info(f"[A2A] Saved reply to A2A session {a2a_sid}")
                except Exception as e:
                    logger.warning(f"[A2A] Failed to save reply to A2A session {a2a_sid}: {e}")
                break  # Only save once

        # Route trigger results to a single deterministic destination. Pure reflection/system
        # wakes stay inside the reflection session and should not spill into arbitrary user chats.
        is_a2a_internal = all(t.name == "a2a_wake" for t in triggers)
        delivery_target = None if is_a2a_internal else await _resolve_trigger_delivery_target(agent, triggers)

        if final_reply and delivery_target and not delivered_platform_message_via_tool:
            try:
                from app.api.websocket import manager as ws_manager
                agent_id_str = str(agent_id)

                # Build notification message with trigger badge
                trigger_reasons = []
                for t in triggers:
                    ns = (t.config or {}).get("_notification_summary", "").strip()
                    if ns:
                        trigger_reasons.append(ns)
                    else:
                        r = (t.reason or "").strip()
                        if r and len(r) <= 80:
                            trigger_reasons.append(r)
                        elif r:
                            trigger_reasons.append(r[:77] + "...")
                summary = trigger_reasons[0] if trigger_reasons else "有新的事件需要处理"

                _is_a2a_wait = any(t.name.startswith("a2a_wait_") for t in triggers)
                if _is_a2a_wait:
                    import re as _re
                    cleaned = final_reply
                    _internal_patterns = [
                        r'\b(a2a_wait_\w+|a2a_wake)\b',
                        r'\bwait_?\w+_?(task|reply|followup|meeting|sync|api_key)\w*\b',
                        r'\bresolve_\w+\b',
                        r'\bfocus[_ ]?item\b',
                        r'\btask_delegate\b',
                        r'\bfocus_ref\b',
                        r'✅\s*(a2a\w+|wait\w+|触发器\w*|focus\w*).*(?:已取消|已为|保持|活跃|完成状态)[^\n]*',
                        r'[\-•]\s*(?:触发器|trigger|focus|wait_\w+|a2a\w+).*[^\n]*',
                        r'(?:触发器|trigger)\s+\S+\s*(?:已取消|保持活跃|已为完成状态|fired)',
                        r'已静默清理触发器',
                        r'已静默处理完毕',
                        r'继续待命[。，]?\s*',
                        r'，?\s*(?:继续)?待命。',
                    ]
                    for _pat in _internal_patterns:
                        cleaned = _re.sub(_pat, '', cleaned, flags=_re.IGNORECASE)
                    cleaned = _re.sub(r'\n{3,}', '\n\n', cleaned).strip()
                    cleaned = _re.sub(r'[。，]\s*$', '', cleaned).strip()
                    if not cleaned:
                        cleaned = final_reply
                else:
                    cleaned = final_reply

                notification = f"⚡ {summary}\n\n{cleaned}"

                target_session_id = delivery_target["session_id"]
                owner_user_id = delivery_target.get("owner_user_id")

                # Save to the resolved destination session for persistence.
                async with async_session() as db:
                    from app.models.chat_session import ChatSession
                    from app.api.websocket import maybe_mark_session_read_for_active_viewer

                    db.add(ChatMessage(
                        agent_id=agent_id,
                        conversation_id=target_session_id,
                        role="assistant",
                        content=notification,
                        user_id=agent.creator_id,
                        thinking=_capped_thinking,
                    ))
                    session_row = await db.get(ChatSession, uuid.UUID(target_session_id))
                    if session_row:
                        session_row.last_message_at = datetime.now(timezone.utc)
                    if owner_user_id:
                        await maybe_mark_session_read_for_active_viewer(
                            db,
                            agent_id=agent_id,
                            session_id=target_session_id,
                            user_id=uuid.UUID(owner_user_id),
                        )
                    await db.commit()

                payload = {
                    "type": "trigger_notification",
                    "content": notification,
                    "triggers": [t.name for t in triggers],
                    "session_id": target_session_id,
                }

                # Notify only the user who owns the destination session. The frontend will append
                # the message only when that exact session is open; otherwise it just refreshes
                # unread/session state.
                if owner_user_id:
                    await ws_manager.send_to_user(agent_id_str, owner_user_id, payload)
            except Exception as e:
                logger.error(f"Failed to push trigger result to WebSocket: {e}")
                import traceback
                traceback.print_exc()

        # Audit log
        await write_audit_log("trigger_fired", {
            "agent_name": agent.name,
            "triggers": [{"name": t.name, "type": t.type} for t in triggers],
        }, agent_id=agent_id)

        logger.info(f"⚡ Triggers fired for {agent.name}: {[t.name for t in triggers]}")

    except Exception as e:
        invocation_error = str(e)
        invocation_retryable = isinstance(e, RetryableOnMessageError)
        logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if lease_heartbeat_task is not None:
            lease_heartbeat_task.cancel()
            await asyncio.gather(lease_heartbeat_task, return_exceptions=True)
        # Release the lease on every claimed execution so it is not re-fired.
        # Runs on success, on early return (agent expired / model disabled), and
        # on exception. Early returns leave invocation_error=None → completed,
        # which is correct: the trigger was handled (decided to skip), so re-firing
        # would not help.
        if execution_ids:
            try:
                if invocation_error is None:
                    await mark_trigger_executions_completed(execution_ids)
                elif invocation_retryable:
                    await requeue_trigger_executions(execution_ids, invocation_error)
                else:
                    await mark_trigger_executions_failed(execution_ids, invocation_error)
            except Exception as _mark_err:
                logger.warning(
                    f"Failed to finalize trigger executions {execution_ids} for agent {agent_id}: {_mark_err}"
                )


# ── Main Tick Loop ──────────────────────────────────────────────────

async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    new_trace_id()
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.is_enabled.is_(True))
        )
        all_triggers = result.scalars().all()
        # Expunge each object before session.close() is called.
        # session.close() expires all objects still in the identity map;
        # explicit expunge() detaches them WITHOUT expiry so their scalar
        # attributes remain readable outside the session context.
        for _t in all_triggers:
            db.expunge(_t)

    if not all_triggers:
        return


    # Evaluate and enqueue due triggers. Agent invocation happens only after
    # executions are claimed through the distributed execution queue.
    for trigger in all_triggers:
        # Auto-disable expired triggers
        if trigger.expires_at and now >= trigger.expires_at:
            async with async_session() as db:
                result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                t = result.scalar_one_or_none()
                if t:
                    t.is_enabled = False
                    await db.commit()
            continue

        try:
            if await _evaluate_trigger(trigger, now):
                handled = await _handle_okr_report_trigger(trigger, now)
                if not handled:
                    handled = await _handle_okr_collection_trigger(trigger, now)
                if not handled:
                    # Fix 3: Rate limit on_message triggers per agent
                    if trigger.type == "on_message":
                        agent_fires = _on_msg_fire_log.get(trigger.agent_id, [])
                        cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
                        recent = [t for t in agent_fires if t > cutoff]
                        if len(recent) >= _ON_MSG_RATE_LIMIT:
                            logger.warning(
                                f"[A2A Safety] Agent {trigger.agent_id} hit "
                                f"on_message rate limit ({_ON_MSG_RATE_LIMIT}/hr). "
                                f"Auto-disabling trigger '{trigger.name}'."
                            )
                            async with async_session() as db:
                                result = await db.execute(
                                    select(AgentTrigger).where(AgentTrigger.id == trigger.id)
                                )
                                t_obj = result.scalar_one_or_none()
                                if t_obj:
                                    t_obj.is_enabled = False
                                    await db.commit()
                            continue
                        recent.append(now)
                        _on_msg_fire_log[trigger.agent_id] = recent
                    await enqueue_due_trigger(trigger, now)
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.name}: {e}")

    # Claim queued executions with a DB lease so only one worker handles each event.
    try:
        fired_by_invocation, force_invoke = await claim_ready_trigger_invocations(now)
    except Exception as e:
        logger.warning(f"Failed to claim trigger executions: {e}")
        fired_by_invocation = {}
        force_invoke = set()

    # Invoke each independent execution.  on_message buckets are force-invoked
    # and are serialized by their exact origin session in the invocation path.
    for invocation_key, agent_triggers in fired_by_invocation.items():
        agent_id, _bucket = invocation_key
        last = _last_invoke.get(invocation_key)
        if invocation_key not in force_invoke and last and (now - last).total_seconds() < DEDUP_WINDOW:
            continue  # Skip — invoked too recently
        _last_invoke[invocation_key] = now

        # Trigger state (last_fired_at / fire_count / single-shot auto-disable /
        # legacy-webhook `_webhook_pending` clear) is updated atomically at claim
        # time by claim_pending_trigger_executions → apply_base_trigger_fired_state,
        # BEFORE this loop runs — which is what stops a long-running trigger from
        # re-firing on the next tick. Every runtime trigger reaching this point
        # carries an `_execution_id`, so the old inline pre-update block here was
        # unreachable dead code (it `continue`d on `_execution_id`). Worse, that
        # dead copy was the ONLY place clearing legacy `_webhook_pending`, so once
        # the lease path took over, legacy webhooks re-fired every cooldown
        # forever. Removed to keep a single source of truth in executions.py.
        asyncio.create_task(_invoke_agent_for_triggers(agent_id, agent_triggers))


async def wake_agent_with_context(agent_id: uuid.UUID, message_context: str, *, from_agent_id: uuid.UUID | None = None, skip_dedup: bool = False, a2a_session_id: str | None = None) -> None:
    """Public API: wake an agent asynchronously with a message context.

    Creates a synthetic trigger invocation so the agent processes the
    message in a Reflection Session via the standard trigger path.
    If a2a_session_id is provided, the agent's reply will also be saved
    to the A2A chat session for visibility in the admin chat history.
    Safe to call from any async context.

    Args:
        agent_id: The agent to wake.
        message_context: The message to deliver.
        from_agent_id: The agent that initiated this wake (for chain depth tracking).
        skip_dedup: If True, bypass the dedup window check.
        a2a_session_id: Optional A2A chat session ID to mirror the reply into.
    """
    now = datetime.now(timezone.utc)

    if from_agent_id:
        chain_key = f"{from_agent_id}->{agent_id}"
        current_depth = _A2A_WAKE_CHAIN.get(chain_key, 0)
        if current_depth >= _A2A_MAX_WAKE_DEPTH:
            logger.warning(
                f"[A2A] Wake chain depth {current_depth} reached for {chain_key}, "
                f"stopping to prevent wake storm"
            )
            return

        _A2A_WAKE_CHAIN[chain_key] = current_depth + 1

        def _decay_chain():
            _A2A_WAKE_CHAIN.pop(chain_key, None)
        asyncio.get_running_loop().call_later(_A2A_WAKE_CHAIN_TTL, _decay_chain)

    if not skip_dedup and agent_id in _last_invoke:
        elapsed = (now - _last_invoke[agent_id]).total_seconds()
        if elapsed < DEDUP_WINDOW:
            logger.info(
                f"[A2A] Skipping wake for agent {agent_id} — "
                f"invoked {elapsed:.0f}s ago (dedup window {DEDUP_WINDOW}s)"
            )
            return

    _last_invoke[agent_id] = now

    from_agent_name = ""
    if from_agent_id:
        try:
            async with async_session() as db:
                from app.models.agent import Agent as AgentModel
                r = await db.execute(select(AgentModel.name).where(AgentModel.id == from_agent_id))
                from_agent_name = r.scalar() or ""
        except Exception as e:
            logger.warning(f"Failed to lookup sender agent name: {e}")

    dummy_trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="a2a_wake",
        type="on_message",
        config={"from_agent_name": from_agent_name, "_matched_message": message_context[:2000], "_matched_from": "agent", "_a2a_session_id": a2a_session_id},
        reason=(
            "You received a notification from another agent. "
            "Read the message content above, update your focus and memory if needed, "
            "and take any action you deem necessary. "
            "Do NOT reply back to the sender unless you have a genuine question — "
            "this was a notification, not a request for response."
        ),
        is_enabled=True,
        last_fired_at=now,
        fire_count=0,
    )
    asyncio.create_task(_invoke_agent_for_triggers(agent_id, [dummy_trigger]))


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    logger.info("⚡ Trigger Daemon started (15s tick, heartbeat every ~60s)")
    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error(f"Trigger Daemon error: {e}")
            import traceback
            traceback.print_exc()

        # Run heartbeat check every 4th tick (~60 seconds)
        _heartbeat_counter += 1
        if _heartbeat_counter >= 4:
            _heartbeat_counter = 0
            _cleanup_stale_invoke_cache()
            try:
                from app.services.heartbeat import _heartbeat_tick
                await _heartbeat_tick()
            except Exception as e:
                logger.error(f"Heartbeat tick error: {e}")

        await asyncio.sleep(TICK_INTERVAL)
