"""Trigger management tools extracted from the unified agent tool service."""

import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.services.focus_service import ensure_focus_item
from app.services.recipient_resolver import (
    RecipientResolutionError,
    resolve_agent_recipient,
    resolve_platform_user_recipient,
)


MAX_TRIGGERS_PER_AGENT = 20
VALID_TRIGGER_TYPES = {"cron", "once", "interval", "poll", "on_message", "webhook"}


async def _resolve_trigger_model_id(db, agent_id: uuid.UUID, reference: object) -> uuid.UUID | None:
    raw = str(reference or "").strip()
    if not raw:
        return None
    from app.models.agent import Agent
    from app.services.chat_model_selection import MODEL_STATUS_OK, resolve_tenant_model_reference

    agent = await db.get(Agent, agent_id)
    if agent is None or agent.tenant_id is None:
        raise ValueError("当前 Agent 没有可用模型目录")
    resolved = await resolve_tenant_model_reference(
        db,
        tenant_id=agent.tenant_id,
        reference=raw,
    )
    if resolved.status != MODEL_STATUS_OK or resolved.model is None:
        raise ValueError(f"模型 {raw} 不可用")
    return resolved.model.id


async def _handle_set_trigger(
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    session_id: str = "",
    user_id: uuid.UUID | None = None,
    turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Create a new trigger for the agent."""
    from app.models.trigger import AgentTrigger
    from app.models.chat_session import ChatSession

    name = arguments.get("name", "").strip()
    ttype = arguments.get("type", "").strip()
    # Runtime correlation keys are platform-owned.  Keep the public tool
    # contract unchanged and never let a model/client forge hidden routing
    # state through the free-form config object.
    config = {
        key: value for key, value in dict(arguments.get("config", {}) or {}).items() if not str(key).startswith("_")
    }
    reason = arguments.get("reason", "").strip()
    focus_ref = arguments.get("focus_ref", "") or arguments.get("agenda_ref", "")  # backward compat
    soul = arguments.get("soul", True) is not False
    memory = arguments.get("memory", True) is not False
    try:
        from app.services.chat_model_selection import validate_temperature

        temperature = validate_temperature(arguments.get("temperature"))
        async with async_session() as model_db:
            model_id = await _resolve_trigger_model_id(model_db, agent_id, arguments.get("model"))
    except ValueError as exc:
        return f"❌ {exc}"

    if not name:
        return "❌ Missing required argument 'name'"
    if ttype not in VALID_TRIGGER_TYPES:
        return f"❌ Invalid trigger type '{ttype}'. Valid types: {', '.join(VALID_TRIGGER_TYPES)}"
    if not reason:
        return "❌ Missing required argument 'reason'"

    try:
        focus_ref = await ensure_focus_item(
            agent_id,
            focus_ref=focus_ref,
            description=reason or name,
            system=False,
        )
    except Exception as e:
        logger.warning(f"[Trigger] Failed to ensure Focus item for trigger {name}: {e}")
        focus_ref = focus_ref or name

    # Validate type-specific config
    if ttype == "cron":
        expr = config.get("expr", "")
        if not expr:
            return '❌ cron trigger requires config.expr, e.g. {"expr": "0 9 * * *"}'
        try:
            from croniter import croniter

            croniter(expr)
        except Exception:
            return f"❌ Invalid cron expression: '{expr}'"
    elif ttype == "once":
        if not config.get("at"):
            return '❌ once trigger requires config.at, e.g. {"at": "2026-03-10T09:00:00+08:00"}'
    elif ttype == "interval":
        if not config.get("minutes"):
            return '❌ interval trigger requires config.minutes, e.g. {"minutes": 30}'
    elif ttype == "poll":
        if not config.get("url"):
            return "❌ poll trigger requires config.url"
    elif ttype == "on_message":
        raw_agent_id = str(config.get("from_agent_id") or "").strip()
        raw_user_id = str(config.get("from_user_id") or "").strip()
        if bool(raw_agent_id) == bool(raw_user_id):
            return "❌ on_message config requires exactly one of from_agent_id or from_user_id"
        try:
            async with async_session() as _identity_db:
                if raw_agent_id:
                    resolved = await resolve_agent_recipient(_identity_db, agent_id, raw_agent_id)
                    config["from_agent_id"] = str(resolved.target_agent.id)
                else:
                    resolved = await resolve_platform_user_recipient(_identity_db, agent_id, raw_user_id)
                    config["from_user_id"] = str(resolved.user.id)
        except RecipientResolutionError as exc:
            return exc.as_json()
        # Snapshot the latest message timestamp so we only detect NEW messages after this point
        # This prevents false positives from already-processed messages
        try:
            from app.models.audit import ChatMessage
            from app.models.chat_session import ChatSession
            from sqlalchemy import cast as sa_cast, String as SaString

            async with async_session() as _snap_db:
                _snap_q = (
                    select(ChatMessage.created_at)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == agent_id,
                        ChatMessage.created_at.isnot(None),
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                _snap_r = await _snap_db.execute(_snap_q)
                _latest_ts = _snap_r.scalar_one_or_none()
                if _latest_ts:
                    config["_since_ts"] = _latest_ts.isoformat()
        except Exception:
            pass  # Fallback to trigger.created_at in the daemon
    elif ttype == "webhook":
        # Auto-generate a unique token for the webhook URL
        import secrets

        token = secrets.token_urlsafe(8)  # ~11 chars, URL-safe
        config["token"] = token
        wmode = arguments.get("webhook_mode", "legacy")
        if wmode in ("queue", "merge"):
            config["webhook_mode"] = wmode
            config["_webhook_queue"] = []

    public_config = {key: value for key, value in config.items() if not str(key).startswith("_")}

    # Record the session that created this trigger so trigger results can later be routed to
    # the correct destination instead of being broadcast to every live web session.
    if session_id:
        try:
            async with async_session() as _ctx_db:
                _session_result = await _ctx_db.execute(
                    select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
                )
                origin_session = _session_result.scalar_one_or_none()
                if origin_session:
                    config["_origin_session_id"] = str(origin_session.id)
                    config["_origin_source_channel"] = origin_session.source_channel
                    config["_origin_external_conv_id"] = origin_session.external_conv_id
                    if turn_anchor_id:
                        config["_origin_turn_anchor_id"] = str(turn_anchor_id)
                        config["_origin_completion_barrier"] = True
                        origin_anchor = await _ctx_db.get(ChatMessage, turn_anchor_id)
                        if origin_anchor is not None and origin_anchor.conversation_id == str(origin_session.id):
                            origin_meta = (
                                origin_anchor.message_meta if isinstance(origin_anchor.message_meta, dict) else {}
                            )
                            if origin_meta.get("actor_ref"):
                                config["_origin_actor_ref"] = str(origin_meta["actor_ref"])
                            if origin_meta.get("actor_ref_type"):
                                config["_origin_actor_ref_type"] = str(origin_meta["actor_ref_type"])
                    # The active tool caller is authoritative.  Group IM
                    # sessions keep a creator placeholder in ChatSession.user_id.
                    if user_id:
                        config["_origin_user_id"] = str(user_id)
                    if origin_session.source_channel == "agent" and origin_session.peer_agent_id:
                        config["_origin_peer_agent_id"] = str(origin_session.peer_agent_id)
                    elif origin_session.source_channel != "trigger" and not user_id:
                        config["_origin_user_id"] = str(origin_session.user_id)
                elif user_id:
                    config["_origin_user_id"] = str(user_id)
        except Exception:
            if user_id:
                config["_origin_user_id"] = str(user_id)

    # Bind a send-derived on_message subscription to the exact remote session.
    # Only receipts created by THIS origin turn are eligible: a plain
    # set_trigger with no same-turn send remains the historical name-based
    # watcher and is never silently narrowed by an older outbound message.
    if ttype == "on_message":
        try:
            async with async_session() as _bind_db:
                outbound = None
                if session_id and turn_anchor_id:
                    target_user_id = str(config.get("from_user_id") or "").strip()
                    target_agent_id = str(config.get("from_agent_id") or "").strip()
                    receipt_rows = await _bind_db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.role.in_(["assistant", "user"]),
                            ChatMessage.message_meta["direction"].as_string() == "outbound",
                            ChatMessage.message_meta["origin_session_id"].as_string() == str(session_id),
                            ChatMessage.message_meta["origin_turn_anchor_id"].as_string() == str(turn_anchor_id),
                        )
                        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                    )
                    candidates: list[ChatMessage] = []
                    for candidate in receipt_rows.scalars().all():
                        meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                        candidate_user_id = str(meta.get("target_user_id") or "").strip()
                        candidate_agent_id = str(meta.get("target_agent_id") or "").strip()
                        if target_user_id and candidate_user_id != target_user_id:
                            continue
                        if target_agent_id and candidate_agent_id != target_agent_id:
                            continue
                        candidates.append(candidate)

                    watch_ids = {candidate.conversation_id for candidate in candidates}
                    if len(watch_ids) > 1:
                        return (
                            "❌ Multiple matching recipients/channels were used in this turn. "
                            "Create each on_message trigger immediately after its corresponding send."
                        )
                    if candidates:
                        outbound = candidates[-1]

                if outbound is not None:
                    meta = outbound.message_meta if isinstance(outbound.message_meta, dict) else {}
                    config["_outbound_message_id"] = str(outbound.id)
                    if meta.get("external_message_id"):
                        config["_outbound_external_message_id"] = str(meta["external_message_id"])
                    if meta.get("actor_ref"):
                        config["_watch_actor_ref"] = str(meta["actor_ref"])
                    watch_session = await _bind_db.get(ChatSession, uuid.UUID(str(outbound.conversation_id)))
                    if watch_session is None:
                        return "❌ The sent message's conversation no longer exists"
                    owns_session = watch_session.agent_id == agent_id or (
                        watch_session.source_channel == "agent"
                        and agent_id in {watch_session.agent_id, watch_session.peer_agent_id}
                    )
                    if not owns_session:
                        return "❌ The sent message's conversation does not belong to this agent"
                    config["_watch_session_id"] = str(watch_session.id)
                    config["_watch_source_channel"] = watch_session.source_channel
                    config["_correlation_mode"] = "session_event"
                    config["_consume_remote"] = True
                    if outbound.created_at:
                        config["_since_ts"] = outbound.created_at.isoformat()
        except (TypeError, ValueError):
            return "❌ Unable to resolve the sent message's conversation"

        config["_set_trigger_context"] = {
            "name": name,
            "type": "on_message",
            "reason": reason,
            "focus_ref": focus_ref or "",
            "config": public_config,
        }

    reenabled_fire_count: int | None = None
    try:
        async with async_session() as db:
            # Load agent to get per-agent trigger limit
            from app.models.agent import Agent as _AgentModel

            _a_result = await db.execute(select(_AgentModel).where(_AgentModel.id == agent_id))
            _agent_obj = _a_result.scalar_one_or_none()
            agent_max_triggers = (_agent_obj.max_triggers if _agent_obj else None) or MAX_TRIGGERS_PER_AGENT

            # Check max triggers
            from sqlalchemy import func as sa_func

            result = await db.execute(
                select(sa_func.count())
                .select_from(AgentTrigger)
                .where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.is_enabled == True,
                )
            )
            count = result.scalar() or 0
            if count >= agent_max_triggers:
                return f"❌ Maximum trigger limit reached ({agent_max_triggers}). Cancel some triggers first."

            # Check for duplicate name
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                if existing.is_enabled:
                    return f"❌ Trigger '{name}' already exists and is active. Use update_trigger to modify it, or cancel_trigger first."
                # Re-enable disabled trigger with new config (preserve fire history).
                # For webhook triggers, reuse the old token so the URL stays stable.
                if ttype == "webhook":
                    old_token = (existing.config or {}).get("token")
                    if old_token:
                        config["token"] = old_token
                if user_id is not None:
                    from app.services.execution_identity import align_background_execution_user

                    await align_background_execution_user(
                        db,
                        agent_id=agent_id,
                        resource_type="trigger",
                        resource_id=existing.id,
                        execution_user_id=user_id,
                    )
                existing.type = ttype
                existing.config = config
                existing.reason = reason
                existing.focus_ref = focus_ref
                existing.is_enabled = True
                existing.model_id = model_id
                existing.temperature = temperature
                existing.soul = soul
                existing.memory = memory
                # Keep fire_count and last_fired_at — they are cumulative stats,
                # but reset fire_count if it reached max_fires to allow it to run again.
                if existing.max_fires and existing.fire_count >= existing.max_fires:
                    existing.fire_count = 0
                trigger = existing
                reenabled_fire_count = existing.fire_count
            else:
                trigger = AgentTrigger(
                    agent_id=agent_id,
                    created_by_user_id=user_id,
                    execution_user_id=user_id,
                    name=name,
                    type=ttype,
                    config=config,
                    reason=reason,
                    focus_ref=focus_ref,
                    model_id=model_id,
                    temperature=temperature,
                    soul=soul,
                    memory=memory,
                )
            # Fix 4: Safety cap for on_message triggers —
            # prevent infinite loops if agent creates broad watchers.
            if ttype == "on_message":
                trigger.max_fires = trigger.max_fires or 100
                if not trigger.expires_at:
                    trigger.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
            if existing is None:
                db.add(trigger)
            await db.commit()

        # Close the send→arm race: a fast remote reply may already be durable
        # before set_trigger commits.  Replay only the exact watch session after
        # the outbound receipt; the execution key keeps this idempotent with the
        # daemon poll and the live ingress matcher.
        if ttype == "on_message" and config.get("_watch_session_id"):
            try:
                from app.services.trigger_runtime.evaluator import (
                    recover_exact_on_message_events,
                )

                await recover_exact_on_message_events(trigger)
            except Exception as replay_error:
                logger.warning(
                    "[Trigger] reply-before-arm replay failed for %s: %s",
                    name,
                    replay_error,
                )

        # Activity log
        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log(
                "trigger_created",
                {
                    "name": name,
                    "type": ttype,
                    "reason": reason[:100],
                },
                agent_id=agent_id,
            )
        except Exception:
            pass

        # Return webhook URL for webhook triggers.
        # Resolve via resolve_base_url (the same tenant-aware resolver used by
        # list_triggers and the agent's "Platform Base URLs" prompt) so the URL
        # stays consistent across the system and never falls back to a
        # placeholder domain from a legacy deployment.
        if ttype == "webhook":
            from app.core.domain import resolve_base_url
            from app.models.agent import Agent as _AgentModel

            async with async_session() as _url_db:
                _a_r = await _url_db.execute(select(_AgentModel).where(_AgentModel.id == agent_id))
                _agent = _a_r.scalar_one_or_none()
                _tenant_id = str(_agent.tenant_id) if _agent and _agent.tenant_id else None
                base = (await resolve_base_url(_url_db, request=None, tenant_id=_tenant_id)).rstrip("/")
            webhook_url = f"{base}/api/webhooks/t/{config['token']}"

            mode_note = f"\nMode: {wmode}" if wmode in ("queue", "merge") else ""
            hook_token = config["token"]
            return (
                f"✅ Webhook trigger '{name}' created.\n\n"
                f"Webhook URL: {webhook_url}{mode_note}\n\n"
                "Two ways to drive this hook:\n\n"
                "1) External services — give the URL to the user to configure in GitHub/Grafana/etc.; "
                "a POST wakes you up with the payload.\n\n"
                "2) Collect info from a standalone page you generate — your report/HTML pages are hosted "
                "independently (outside the main app). Inject the platform SDK and that page can "
                "(a) identify whoever opens it via company OAuth (incl. mobile), and (b) POST any info "
                "collected on the page back to THIS hook to wake you. Add to the HTML <head>:\n"
                f'   <script src="/sdk/clawith.js" data-hook="{hook_token}"></script>\n'
                "   Reader identity: window.Clawith.onReady(u => ...)  // {userId, userName, mobile}\n"
                "   Send info back:  window.Clawith.triggerHook(window.Clawith.hook, { ...any fields })\n"
                "   Anti-leak watermark: add data-watermark to that same <script> to tile the "
                "viewer's name + mobile tail across the page.\n"
                "   Works for ANY reader-to-you collection (survey, confirmation, choice, sign-up, "
                "feedback, ...), not just feedback. Each call wakes you once "
                "(use webhook_mode=queue to process them one-by-one)."
            )

        if reenabled_fire_count is not None:
            return (
                f"✅ Trigger '{name}' re-enabled with new configuration "
                f"({ttype}, fired {reenabled_fire_count} times so far)"
            )
        return f"✅ Trigger '{name}' created ({ttype}). It will fire according to your config and wake you up with the reason as context."

    except Exception as e:
        return f"❌ Failed to create trigger: {e}"


async def _handle_update_trigger(
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    user_id: uuid.UUID | None = None,
) -> str:
    """Update an existing trigger's config or reason."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    if not name:
        return "❌ Missing required argument 'name'"

    new_config = arguments.get("config")
    new_reason = arguments.get("reason")
    new_webhook_mode = arguments.get("webhook_mode")
    model_supplied = "model" in arguments
    temperature_supplied = "temperature" in arguments
    soul_supplied = "soul" in arguments
    memory_supplied = "memory" in arguments

    if (
        new_config is None
        and new_reason is None
        and new_webhook_mode is None
        and not model_supplied
        and not temperature_supplied
        and not soul_supplied
        and not memory_supplied
    ):
        return "❌ Provide at least one field to update"

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentTrigger)
                .where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
                .with_for_update()
            )
            trigger = result.scalar_one_or_none()
            if not trigger:
                return f"❌ Trigger '{name}' not found"
            if model_supplied:
                try:
                    trigger.model_id = await _resolve_trigger_model_id(
                        db,
                        agent_id,
                        arguments.get("model"),
                    )
                except ValueError as exc:
                    return f"❌ {exc}"
                changes = ["model updated"]
            else:
                changes = []
            if temperature_supplied:
                try:
                    from app.services.chat_model_selection import validate_temperature

                    trigger.temperature = validate_temperature(arguments.get("temperature"))
                except ValueError as exc:
                    return f"❌ {exc}"
                changes.append("temperature updated")
            if soul_supplied:
                trigger.soul = arguments.get("soul") is not False
                changes.append("soul updated")
            if memory_supplied:
                trigger.memory = arguments.get("memory") is not False
                changes.append("memory updated")

            # Freeze already-queued work before changing config fields that can
            # participate in legacy execution-user fallback.
            if user_id is not None:
                from app.services.execution_identity import align_background_execution_user

                await align_background_execution_user(
                    db,
                    agent_id=agent_id,
                    resource_type="trigger",
                    resource_id=trigger.id,
                    execution_user_id=user_id,
                )

            if new_config is not None:
                if not isinstance(new_config, dict):
                    return "❌ Trigger config must be an object"
                old_config = dict(trigger.config or {})
                public_new = {
                    key: value for key, value in new_config.items() if not str(key).startswith("_")
                }
                if trigger.type == "webhook":
                    # Webhook config updates are patches. The callback token,
                    # delivery mode and security settings are durable identity;
                    # omitting one must never reset it or strand queued events.
                    public_old = {
                        key: value
                        for key, value in old_config.items()
                        if not str(key).startswith("_")
                    }
                    public_new = {**public_old, **public_new}
                if trigger.type == "on_message":
                    raw_agent_id = str(public_new.get("from_agent_id") or "").strip()
                    raw_user_id = str(public_new.get("from_user_id") or "").strip()
                    if bool(raw_agent_id) == bool(raw_user_id):
                        return "❌ on_message config requires exactly one of from_agent_id or from_user_id"
                    try:
                        if raw_agent_id:
                            recipient = await resolve_agent_recipient(db, agent_id, raw_agent_id)
                            public_new["from_agent_id"] = str(recipient.target_agent.id)
                        else:
                            recipient = await resolve_platform_user_recipient(db, agent_id, raw_user_id)
                            public_new["from_user_id"] = str(recipient.user.id)
                    except RecipientResolutionError as exc:
                        return exc.as_json()
                    public_new.pop("from_agent_name", None)
                    public_new.pop("from_user_name", None)
                private_old = {key: value for key, value in old_config.items() if str(key).startswith("_")}
                old_target = (
                    old_config.get("from_agent_id"),
                    old_config.get("from_user_id"),
                )
                new_target = (
                    public_new.get("from_agent_id"),
                    public_new.get("from_user_id"),
                )
                if trigger.type == "on_message" and old_target != new_target:
                    # A changed public sender criterion is a legacy watcher until
                    # a future same-turn send arms a new exact route.  Preserve
                    # only the origin destination; never keep a stale remote R.
                    private_old = {
                        key: value
                        for key, value in private_old.items()
                        if key.startswith("_origin_") or key == "_set_trigger_context"
                    }
                trigger.config = {**public_new, **private_old}
                changes.append(f"config: {old_config} → {new_config}")
            if new_webhook_mode is not None:
                if trigger.type != "webhook":
                    return "❌ webhook_mode only applies to webhook triggers"
                if new_webhook_mode not in ("legacy", "queue", "merge"):
                    return "❌ Invalid webhook_mode (must be legacy/queue/merge)"
                # Merge into existing config — preserve token / queued payloads, only flip the mode
                cfg = dict(trigger.config or {})
                if new_webhook_mode == "legacy":
                    cfg.pop("webhook_mode", None)  # legacy is the default → drop the key
                else:
                    cfg["webhook_mode"] = new_webhook_mode
                    cfg.setdefault("_webhook_queue", [])  # ensure queue exists when switching to queue/merge
                trigger.config = cfg
                changes.append(f"webhook_mode → {new_webhook_mode}")
            if new_reason is not None:
                trigger.reason = new_reason
                changes.append("reason updated")

            if trigger.type == "on_message":
                cfg = dict(trigger.config or {})
                cfg["_set_trigger_context"] = {
                    "name": trigger.name,
                    "type": "on_message",
                    "reason": trigger.reason,
                    "focus_ref": trigger.focus_ref or "",
                    "config": {key: value for key, value in cfg.items() if not str(key).startswith("_")},
                }
                trigger.config = cfg

            await db.commit()

        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log(
                "trigger_updated",
                {
                    "name": name,
                    "changes": "; ".join(changes),
                },
                agent_id=agent_id,
            )
        except Exception:
            pass

        return f"✅ Trigger '{name}' updated: {'; '.join(changes)}"

    except Exception as e:
        return f"❌ Failed to update trigger: {e}"


async def _handle_cancel_trigger(
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    user_id: uuid.UUID | None = None,
) -> str:
    """Cancel (disable) a trigger by name."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    if not name:
        return "❌ Missing required argument 'name'"

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
            )
            trigger = result.scalar_one_or_none()
            if not trigger:
                return f"❌ Trigger '{name}' not found"
            if not trigger.is_enabled:
                return f"ℹ️ Trigger '{name}' is already disabled"

            if user_id is not None:
                from app.services.execution_identity import align_background_execution_user

                await align_background_execution_user(
                    db,
                    agent_id=agent_id,
                    resource_type="trigger",
                    resource_id=trigger.id,
                    execution_user_id=user_id,
                )
            trigger.is_enabled = False
            await db.commit()

        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log("trigger_cancelled", {"name": name}, agent_id=agent_id)
        except Exception:
            pass

        return f"✅ Trigger '{name}' cancelled. It will no longer fire."

    except Exception as e:
        return f"❌ Failed to cancel trigger: {e}"


async def _handle_list_triggers(agent_id: uuid.UUID) -> str:
    """List all active triggers for the agent."""
    from app.models.trigger import AgentTrigger
    from app.models.agent import Agent as AgentModel
    from app.core.domain import resolve_base_url

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentTrigger)
                .where(
                    AgentTrigger.agent_id == agent_id,
                )
                .order_by(AgentTrigger.created_at.desc())
            )
            triggers = result.scalars().all()

            # Resolve base_url for webhook triggers
            _a_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _a_r.scalar_one_or_none()
            _tenant_id = str(_agent.tenant_id) if _agent and _agent.tenant_id else None
            _base_url = (await resolve_base_url(db, request=None, tenant_id=_tenant_id)).rstrip("/")

        if not triggers:
            return "No triggers found. Use set_trigger to create one."

        active_onmessage = sum(1 for trigger in triggers if trigger.type == "on_message" and trigger.is_enabled)
        total_onmessage = sum(1 for trigger in triggers if trigger.type == "on_message")
        lines = [
            f"on_message: {active_onmessage} active / {total_onmessage} total",
            "",
            "| ID | Name | Type | Config | Webhook URL | Reason | Status | Fires | Created By | Execution User |",
            "|----|------|------|--------|-------------|--------|--------|-------|------------|----------------|",
        ]
        for t in triggers:
            status = "✅ active" if t.is_enabled else "⏸ disabled"
            config = t.config or {}
            if t.type == "webhook" and config.get("token"):
                config_str = f"token: {config['token']}"
                webhook_url = f"{_base_url}/api/webhooks/t/{config['token']}"
            else:
                public_config = {key: value for key, value in config.items() if not str(key).startswith("_")}
                config_str = str(public_config)[:50]
                webhook_url = "-"
            reason_str = t.reason[:40] if t.reason else ""
            lines.append(
                f"| {t.id} | {t.name} | {t.type} | {config_str} | {webhook_url} | "
                f"{reason_str} | {status} | {t.fire_count} | {t.created_by_user_id} | "
                f"{t.execution_user_id} |"
            )

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Failed to list triggers: {e}"

__all__ = [name for name in globals() if not name.startswith("__")]
