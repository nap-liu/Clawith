"""Trigger eligibility and source evaluation."""

from app.services.trigger_daemon_shared import *  # noqa: F401,F403

async def _evaluate_trigger(
    trigger: AgentTrigger,
    now: datetime,
    *,
    cron_occurrence: CronOccurrence | None = None,
) -> bool:
    """Return True if this trigger should fire right now."""
    from app.core.okr_feature import is_retired_okr_trigger

    if is_retired_okr_trigger(trigger.name, trigger.agent_id):
        return False
    if not _is_trigger_eligible(trigger, now):
        return False

    cfg = trigger.config or {}
    t = trigger.type
    webhook_mode = cfg.get("webhook_mode", "legacy") if t == "webhook" else None

    if t == "cron":
        expr = cfg.get("expr", "* * * * *")
        try:
            occurrence = cron_occurrence or await compute_next_trigger_cron_occurrence(trigger)
            if now >= occurrence.scheduled_for:
                if await _should_skip_non_workday(
                    trigger,
                    occurrence.local_scheduled_for,
                ):
                    await _mark_trigger_skipped(trigger.id, now)
                    logger.info(
                        f"[Trigger] Skipped {trigger.name} on non-workday {occurrence.local_scheduled_for.date()}"
                    )
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
            from app.services.trigger_time_contract import resolve_once_trigger_at

            at = await resolve_once_trigger_at(trigger, cfg)
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
                        return True  # 锁超时, 强制重处理(死锁兜底)
                except Exception:
                    pass
            return False  # 串行: 有活动 session, 等
        return len(cfg.get("_webhook_queue") or []) > 0

    return False


def _is_trigger_eligible(trigger: AgentTrigger, now: datetime) -> bool:
    """Check common trigger guards without I/O."""
    if not trigger.is_enabled:
        return False
    if trigger.expires_at and now >= trigger.expires_at:
        # Auto-disable expired triggers
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    cfg = trigger.config or {}
    webhook_mode = cfg.get("webhook_mode", "legacy") if trigger.type == "webhook" else None

    # Cooldown check — queue/merge webhook 绕过(串行锁保证不并发); 其余 type 行为不变
    if trigger.last_fired_at and webhook_mode not in ("queue", "merge"):
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False
    return True


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
                await db.execute(update(AgentTrigger).where(AgentTrigger.id == trigger.id).values(config=cfg))
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
                from app.models.agent import Agent as AgentModel
                from app.models.participant import Participant

                safe_agent_name = from_agent_name.replace("%", "").replace("_", r"\_")
                agent_r = await db.execute(select(AgentModel).where(AgentModel.name.ilike(f"%{safe_agent_name}%")))
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

                from sqlalchemy import String as SaString
                from sqlalchemy import cast as sa_cast

                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatMessage.participant_id == from_participant,
                        ChatMessage.created_at > since,
                        ChatMessage.role == "assistant",
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
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
                from sqlalchemy import String as SaString
                from sqlalchemy import cast as sa_cast

                from app.models.agent import Agent as AgentModel

                # 0. Get agent for tenant scoping
                agent_r = await db.execute(select(AgentModel).where(AgentModel.id == trigger.agent_id))
                agent = agent_r.scalar_one_or_none()

                # Look up user by display name or username within tenant
                from sqlalchemy import or_

                from app.models.user import Identity, User

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
                        select(ChatMessage)
                        .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                        .where(
                            ChatSession.agent_id == trigger.agent_id,
                            ChatSession.user_id == target_user.id,
                            ChatSession.source_channel.in_(["feishu", "slack", "discord", "web"]),
                            ChatMessage.role == "user",
                            ChatMessage.created_at > since,
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(1)
                    )
                else:
                    # Fallback: search by session title or message content containing the target name
                    result = await db.execute(
                        select(ChatMessage)
                        .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                        .where(
                            ChatSession.agent_id == trigger.agent_id,
                            ChatSession.source_channel.in_(["feishu", "slack", "discord", "web"]),
                            ChatMessage.role == "user",
                            ChatMessage.created_at > since,
                            or_(
                                ChatSession.title.ilike(f"%{safe_user_name}%"),
                                ChatMessage.content.ilike(f"%{safe_user_name}%"),
                            ),
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(1)
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

__all__ = [name for name in globals() if not name.startswith("__")]
