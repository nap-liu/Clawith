"""Trigger evaluation and deterministic special-case handlers."""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import and_, func, or_, select

from app.database import async_session
from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.cron_schedule import (
    compute_next_trigger_cron_occurrence,
)
from app.services.trigger_runtime.on_message_evaluator import (
    MIN_POLL_INTERVAL_MINUTES,
    OnMessageMatchResult,
    _canonical_trigger_actor,
    _canonical_message_actor,
    match_incoming_chat_message,
    recover_exact_on_message_events,
    recover_legacy_on_message_events,
)

async def should_skip_non_workday(trigger: AgentTrigger, local_now: datetime) -> bool:
    if trigger.name != "daily_okr_collection":
        return False

    from app.models.okr import OKRSettings
    from app.models.tenant import Tenant
    from app.services.business_calendar import is_non_workday

    async with async_session() as db:
        result = await db.execute(
            select(Agent.tenant_id).where(Agent.id == trigger.agent_id)
        )
        tenant_id = result.scalar_one_or_none()
        if not tenant_id:
            return False

        settings_result = await db.execute(
            select(OKRSettings.daily_report_skip_non_workdays).where(OKRSettings.tenant_id == tenant_id)
        )
        skip_enabled = settings_result.scalar_one_or_none()
        if skip_enabled is False:
            return False

        tenant_result = await db.execute(
            select(Tenant.country_region).where(Tenant.id == tenant_id)
        )
        country_region = tenant_result.scalar_one_or_none()

    return is_non_workday(local_now.date(), country_region)


async def mark_trigger_skipped(trigger_id: uuid.UUID, now: datetime) -> None:
    try:
        async with async_session() as db:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if trigger:
                trigger.last_fired_at = now
                await db.commit()
    except Exception as e:
        logger.warning(f"Failed to mark skipped trigger {trigger_id}: {e}")


async def mark_trigger_fired(trigger_id: uuid.UUID, now: datetime) -> None:
    try:
        async with async_session() as db:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if trigger:
                trigger.last_fired_at = now
                trigger.fire_count += 1
                if trigger.type == "once":
                    trigger.is_enabled = False
                if trigger.max_fires and trigger.fire_count >= trigger.max_fires:
                    trigger.is_enabled = False
                await db.commit()
    except Exception as e:
        logger.warning(f"Failed to mark fired trigger {trigger_id}: {e}")


async def handle_okr_report_trigger(
    trigger: AgentTrigger,
    now: datetime,
    *,
    scheduled_for: datetime | None = None,
    local_scheduled_for: datetime | None = None,
) -> bool:
    if trigger.name not in {"daily_okr_report", "weekly_okr_report", "monthly_okr_report"}:
        return False
    from app.core.okr_feature import is_retired_okr_trigger

    if is_retired_okr_trigger(trigger.name, trigger.agent_id):
        return True

    from zoneinfo import ZoneInfo
    from app.models.okr import OKRSettings
    from app.services.okr_reporting import (
        generate_company_daily_report,
        generate_company_monthly_report,
        generate_company_weekly_report,
    )
    from app.services.timezone_utils import get_agent_timezone

    async with async_session() as db:
        agent_result = await db.execute(select(Agent.tenant_id).where(Agent.id == trigger.agent_id))
        tenant_id = agent_result.scalar_one_or_none()
        if not tenant_id:
            return True

        settings_result = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == tenant_id))
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.enabled:
            return True

    if local_scheduled_for is not None:
        if local_scheduled_for.tzinfo is None:
            raise ValueError("local_scheduled_for must be timezone-aware")
        local_today = local_scheduled_for.date()
    else:
        tz_name = await get_agent_timezone(trigger.agent_id)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        local_today = (scheduled_for or now).astimezone(tz).date()

    if trigger.name == "daily_okr_report":
        await generate_company_daily_report(tenant_id, local_today - timedelta(days=1))
    elif trigger.name == "weekly_okr_report":
        previous_week_anchor = local_today - timedelta(days=7)
        week_start = previous_week_anchor - timedelta(days=previous_week_anchor.weekday())
        await generate_company_weekly_report(tenant_id, week_start)
    elif trigger.name == "monthly_okr_report":
        previous_month_end = local_today.replace(day=1) - timedelta(days=1)
        await generate_company_monthly_report(tenant_id, previous_month_end)

    await mark_trigger_fired(trigger.id, now)
    logger.info(f"[Trigger] Auto-generated OKR report for trigger {trigger.name}")
    return True


async def handle_okr_collection_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    if trigger.name != "daily_okr_collection":
        return False
    from app.core.okr_feature import is_retired_okr_trigger

    if is_retired_okr_trigger(trigger.name, trigger.agent_id):
        return True

    from app.models.okr import OKRSettings
    from app.services.okr_daily_collection import trigger_daily_collection_for_tenant

    async with async_session() as db:
        agent_result = await db.execute(select(Agent.tenant_id).where(Agent.id == trigger.agent_id))
        tenant_id = agent_result.scalar_one_or_none()
        if not tenant_id:
            return True

        settings_result = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == tenant_id))
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.enabled or not settings.daily_report_enabled:
            return True

    await trigger_daily_collection_for_tenant(tenant_id)
    await mark_trigger_fired(trigger.id, now)
    logger.info(f"[Trigger] Deterministic OKR collection sent for trigger {trigger.name}")
    return True


def is_private_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        import socket
        try:
            infos = socket.getaddrinfo(hostname, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except (socket.gaierror, ValueError):
            return True
        return False
    except Exception:
        return True


async def evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    from app.core.okr_feature import is_retired_okr_trigger

    if is_retired_okr_trigger(trigger.name, trigger.agent_id):
        return False
    if not trigger.is_enabled:
        return False
    if trigger.expires_at and now >= trigger.expires_at:
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    if trigger.last_fired_at:
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False

    cfg = trigger.config or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    t = trigger.type

    if t == "cron":
        expr = cfg.get("expr", "* * * * *")
        try:
            occurrence = await compute_next_trigger_cron_occurrence(trigger)
            if now >= occurrence.scheduled_for:
                if await should_skip_non_workday(
                    trigger,
                    occurrence.local_scheduled_for,
                ):
                    await mark_trigger_skipped(trigger.id, now)
                    logger.info(
                        f"[Trigger] Skipped {trigger.name} on non-workday "
                        f"{occurrence.local_scheduled_for.date()}"
                    )
                    return False
                return True
            return False
        except Exception as e:
            logger.warning(f"Invalid cron expr '{expr}' for trigger {trigger.name}: {e}")
            return False

    if t == "once":
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

    if t == "interval":
        minutes = cfg.get("minutes", 30)
        base = trigger.last_fired_at or trigger.created_at
        return (now - base) >= timedelta(minutes=minutes)

    if t == "poll":
        interval_min = max(cfg.get("interval_min", 5), MIN_POLL_INTERVAL_MINUTES)
        base = trigger.last_fired_at or trigger.created_at
        if (now - base) < timedelta(minutes=interval_min):
            return False
        return await poll_check(trigger)

    if t == "on_message":
        return await check_new_agent_messages(trigger)

    if t == "webhook":
        return False

    return False


async def poll_check(trigger: AgentTrigger) -> bool:
    import httpx

    cfg = trigger.config or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    url = cfg.get("url")
    if not url:
        return False
    if is_private_url(url):
        logger.warning(f"Poll blocked for trigger {trigger.name}: private/internal URL '{url}'")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(cfg.get("method", "GET"), url, headers=cfg.get("headers", {}))
            resp.raise_for_status()

        data = resp.json()
        json_path = cfg.get("json_path", "$")
        current_value = extract_json_path(data, json_path)
        current_str = str(current_value)
        fire_on = cfg.get("fire_on", "change")
        should_fire = False
        if fire_on == "match":
            should_fire = current_str == str(cfg.get("match_value", ""))
        else:
            last_value = cfg.get("_last_value")
            should_fire = last_value is not None and current_str != last_value

        cfg["_last_value"] = current_str
        try:
            from sqlalchemy import update
            async with async_session() as db:
                await db.execute(
                    update(AgentTrigger).where(AgentTrigger.id == trigger.id).values(config=cfg)
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist poll _last_value for {trigger.name}: {e}")

        return should_fire
    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.name}: {e}")
        return False


def extract_json_path(data, path: str):
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


async def check_new_agent_messages(trigger: AgentTrigger) -> bool:
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    cfg = trigger.config or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    actor = _canonical_trigger_actor(cfg)
    if actor is None:
        return False
    actor_type, actor_id = actor

    since = trigger.last_fired_at or trigger.created_at
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at

    try:
        async with async_session() as db:
            watch_session_id = str(cfg.get("_watch_session_id") or "").strip()
            if watch_session_id:
                try:
                    watch_uuid = uuid.UUID(watch_session_id)
                except (TypeError, ValueError):
                    logger.warning("on_message trigger %s has invalid watch session %r", trigger.name, watch_session_id)
                    return False

                watch_session = await db.get(ChatSession, watch_uuid)
                owns_watch = watch_session is not None and (
                    watch_session.agent_id == trigger.agent_id
                    or (
                        watch_session.source_channel == "agent"
                        and trigger.agent_id in {watch_session.agent_id, watch_session.peer_agent_id}
                    )
                )
                if not owns_watch:
                    return False
                expected_channel = str(cfg.get("_watch_source_channel") or "").strip()
                if expected_channel and watch_session.source_channel != expected_channel:
                    logger.warning(
                        "on_message trigger %s watch channel changed: expected=%s actual=%s",
                        trigger.name,
                        expected_channel,
                        watch_session.source_channel,
                    )
                    return False

                anchor = None
                anchor_id = cfg.get("_outbound_message_id")
                if anchor_id:
                    try:
                        anchor = await db.get(ChatMessage, uuid.UUID(str(anchor_id)))
                    except (TypeError, ValueError):
                        anchor = None
                    if anchor is not None and anchor.conversation_id == watch_session_id and anchor.created_at:
                        since = max(since, anchor.created_at)

                result = await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == watch_session_id,
                        ChatMessage.role.in_(
                            ["user", "assistant"] if watch_session.source_channel == "agent" else ["user"]
                        ),
                        ChatMessage.created_at >= since,
                    )
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                    .limit(50)
                )
                candidates = result.scalars().all()
                correlation_mode = str(cfg.get("_correlation_mode") or "next_message")
                outbound_external_id = str(cfg.get("_outbound_external_message_id") or "").strip()
                msg = None
                for candidate in candidates:
                    if anchor is not None:
                        if candidate.id == anchor.id:
                            continue
                        if (
                            anchor.created_at
                            and candidate.created_at
                            and candidate.created_at < anchor.created_at
                        ):
                            continue
                    meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                    if _canonical_message_actor(candidate) != actor:
                        continue
                    if correlation_mode == "reply_to":
                        reply_to = str(meta.get("reply_to_external_message_id") or "").strip()
                        if not outbound_external_id or reply_to != outbound_external_id:
                            continue
                    msg = candidate
                    break

                if msg is None:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = str(actor_id)
                cfg["_matched_message_id"] = str(msg.id)
                cfg["_matched_session_id"] = watch_session_id
                if msg.external_event_key:
                    cfg["_matched_external_event_key"] = msg.external_event_key
                return True

            if actor_type == "agent":
                from sqlalchemy import String as SaString, cast as sa_cast
                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        or_(
                            ChatSession.agent_id == trigger.agent_id,
                            and_(
                                ChatSession.source_channel == "agent",
                                ChatSession.peer_agent_id == trigger.agent_id,
                            ),
                        ),
                        ChatMessage.sender_agent_id == actor_id,
                        ChatMessage.created_at > since,
                        # Fix 1: Only match real conversational messages,
                        # not internal tool_call / system records.
                        ChatMessage.role.in_(["assistant", "user"]),
                        # Fix 2: Exclude trigger internal "reflection"
                        # sessions to avoid cross-trigger false matches.
                        ChatSession.source_channel != "trigger",
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = str(actor_id)
                cfg["_matched_message_id"] = str(msg.id)
                cfg["_matched_session_id"] = str(msg.conversation_id)
                if msg.external_event_key:
                    cfg["_matched_external_event_key"] = msg.external_event_key
                return True

            if actor_type == "user":
                from sqlalchemy import String as SaString, cast as sa_cast
                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatSession.source_channel != "trigger",
                        ChatMessage.sender_user_id == actor_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )

                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                cfg["_matched_message"] = (msg.content or "")[:2000]
                cfg["_matched_from"] = str(actor_id)
                cfg["_matched_message_id"] = str(msg.id)
                cfg["_matched_session_id"] = str(msg.conversation_id)
                if msg.external_event_key:
                    cfg["_matched_external_event_key"] = msg.external_event_key
                return True
    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.name}: {e}")
        return False

    return False
