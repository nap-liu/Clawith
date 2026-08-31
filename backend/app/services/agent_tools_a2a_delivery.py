from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.services.agent_tools import (
    _build_outbound_operation_key,
    current_agent_runtime_workspace,
    ensure_focus_item,
    get_storage_backend,
    logger,
    project_agent_runtime_workspace,
    standard_agent_runtime_workspace,
)
from app.services.recipient_resolver import RecipientResolutionError, resolve_agent_recipient


async def _send_file_to_agent(
    from_agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a workspace file to another digital employee (agent)."""
    canonical_agent_id = str(args.get("agent_id") or "").strip()
    rel_path = (args.get("file_path") or "").strip()
    delivery_note = (args.get("message") or "").strip()
    if not canonical_agent_id or not rel_path:
        return "❌ Please provide both canonical agent_id and file_path"
    storage = get_storage_backend()
    source_key = current_agent_runtime_workspace(from_agent_id).storage_key(rel_path)
    if not await storage.is_file(source_key):
        return f"❌ Source file not found: {rel_path}"
    source_entry = await storage.stat(source_key)
    MAX_FILE_SIZE = 50 * 1024 * 1024
    file_size = source_entry.size
    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        return f"❌ File too large ({size_mb:.1f} MB). Maximum allowed is 50 MB."
    source_bytes = await storage.read_bytes(source_key)
    source_name = Path(rel_path).name
    try:
        from app.services.activity_logger import log_activity
        from app.services.a2a_file_delivery import resolve_a2a_file_origin_scope
        async with async_session() as db:
            try:
                origin_scope = await resolve_a2a_file_origin_scope(
                    db,
                    origin_session_id=origin_session_id,
                    sender_agent_id=from_agent_id,
                )
                explicit_project_id = None
                if args.get("_project_id"):
                    try:
                        explicit_project_id = uuid.UUID(str(args["_project_id"]))
                    except (TypeError, ValueError):
                        return "❌ _project_id must be a complete platform UUID"
                if (
                    origin_scope.project_id is not None
                    and explicit_project_id is not None
                    and origin_scope.project_id != explicit_project_id
                ):
                    return "❌ The file delivery project does not match the originating conversation"
                project_id = origin_scope.project_id or explicit_project_id
                recipient = await resolve_agent_recipient(db, from_agent_id, canonical_agent_id, project_id=project_id)
            except RecipientResolutionError as exc:
                return exc.as_json()
            source_agent = recipient.source_agent
            target_agent = recipient.target_agent
            source_agent_name = source_agent.name
            source_creator_id = source_agent.creator_id
            target_name = target_agent.name
            target_id = target_agent.id
            if str(getattr(target_agent, "scope", "") or "").strip().lower() == "project":
                if project_id is None or getattr(target_agent, "project_id", None) != project_id:
                    return "❌ Project Agent file delivery must remain inside its owning project"
                target_workspace = project_agent_runtime_workspace(
                    agent_id=target_id,
                    tenant_id=target_agent.tenant_id,
                    project_id=project_id,
                )
            else:
                target_workspace = standard_agent_runtime_workspace(target_id)
        ts = datetime.now(timezone.utc)
        stamp = ts.strftime("%Y%m%d_%H%M%S_%f")
        delivered_name = source_name
        target_rel_path = f"workspace/inbox/files/{delivered_name}"
        target_key = target_workspace.storage_key(target_rel_path)
        while await storage.exists(target_key):
            delivered_name = f"{stamp}_{source_name}"
            target_rel_path = f"workspace/inbox/files/{delivered_name}"
            target_key = target_workspace.storage_key(target_rel_path)
        await storage.write_bytes(target_key, source_bytes)
        sender_short = str(from_agent_id)[:8]
        note_rel_path = f"workspace/inbox/{stamp}_{sender_short}_file_delivery.md"
        note_key = target_workspace.storage_key(note_rel_path)
        note_lines = [
            f"# File delivery from {source_agent_name}",
            "",
            f"- Time (UTC): {ts.isoformat()}",
            f"- Sender: {source_agent_name}",
            f"- Source path: {rel_path}",
            f"- Delivered file: {target_rel_path}",
            "",
        ]
        if delivery_note:
            note_lines.append("## Note")
            note_lines.append(delivery_note)
            note_lines.append("")
        note_lines.append("## Action")
        note_lines.append(f'- Read the file via `read_file(path="{target_rel_path}")`')
        await storage.write_text(note_key, "\n".join(note_lines), encoding="utf-8")
        from app.models.audit import AuditLog
        async with async_session() as db:
            db.add(
                AuditLog(
                    agent_id=from_agent_id,
                    action="collaboration:file_send",
                    details={
                        "to_agent": str(target_id),
                        "to_agent_name": target_name,
                        "source_file": rel_path,
                        "delivered_file": target_rel_path,
                    },
                )
            )
            db.add(
                AuditLog(
                    agent_id=target_id,
                    action="collaboration:file_receive",
                    details={
                        "from_agent": str(from_agent_id),
                        "from_agent_name": source_agent_name,
                        "source_file": rel_path,
                        "delivered_file": target_rel_path,
                    },
                )
            )
            await db.commit()
        await log_activity(
            from_agent_id,
            "agent_file_sent",
            f"Sent file to {target_name}",
            detail={"target_agent": target_name, "source_file": rel_path, "delivered_file": target_rel_path},
        )
        await log_activity(
            target_id,
            "agent_file_received",
            f"Received file from {source_agent_name}",
            detail={"source_agent": source_agent_name, "source_file": rel_path, "delivered_file": target_rel_path},
        )
        logger.info(
            "[A2A-File] Injecting file delivery message: from=%s to=%s file=%s",
            source_name,
            target_name,
            delivered_name,
        )
        from app.services.a2a_file_delivery import append_a2a_file_delivery_message
        operation_key = _build_outbound_operation_key(
            agent_id=from_agent_id,
            origin_session_id=origin_session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        file_event_key = f"{operation_key}:file"[:500] if operation_key else None
        async with async_session() as db2:
            chat_session_id = await append_a2a_file_delivery_message(
                db2,
                sender_agent_id=from_agent_id,
                target_agent_id=target_id,
                sender_creator_id=source_creator_id,
                sender_name=source_agent_name,
                target_name=target_name,
                project_id=project_id,
                preferred_session_id=origin_scope.preferred_session_id,
                source_path=rel_path,
                delivered_path=target_rel_path,
                delivered_name=delivered_name,
                delivery_note=delivery_note,
                file_size=file_size,
                created_at=ts,
                external_event_key=file_event_key,
                origin_session_id=origin_session_id,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
            )
            await db2.commit()
        logger.info(
            "[A2A-File] Injected file delivery message into session %s for %s",
            chat_session_id,
            target_name,
        )
        return f"✅ File sent to {target_name}.\n- Delivered to: {target_rel_path}\n- Inbox note: {note_rel_path}"
    except Exception as e:
        logger.exception("[A2A-File] File delivery failed")
        return f"❌ Agent file send error: {str(e)[:200]}"


async def _resolve_a2a_target(db, from_agent_id: uuid.UUID, agent_id: str) -> tuple[AgentModel | None, str | None]:
    """Compatibility helper backed by the canonical exact-ID resolver."""
    try:
        recipient = await resolve_agent_recipient(db, from_agent_id, agent_id)
    except RecipientResolutionError as exc:
        return None, exc.as_json()
    return recipient.target_agent, None


async def _create_on_message_trigger(
    agent_id: uuid.UUID,
    trigger_name: str,
    from_agent_id_value: str | None,
    from_user_id_value: str | None = None,
    reason: str = "",
    focus_ref: str | None = None,
    notification_summary: str | None = None,
    origin_session_id: str | None = None,
    origin_user_id: str | None = None,
    origin_source_channel: str | None = None,
    origin_external_conv_id: str | None = None,
    origin_turn_anchor_id: str | None = None,
    watch_session_id: str | None = None,
    watch_source_channel: str | None = None,
    watch_actor_ref: str | None = None,
    outbound_message_id: str | None = None,
    outbound_external_message_id: str | None = None,
    consume_remote: bool = False,
    expires_in_minutes: int = 1440,
) -> None:
    """Programmatically create an on_message trigger for an agent."""
    from app.models.trigger import AgentTrigger
    creator_user_id = uuid.UUID(origin_user_id) if origin_user_id else None
    focus_ref = await ensure_focus_item(
        agent_id,
        focus_ref=focus_ref,
        description=reason or trigger_name,
    )
    config: dict = {}
    if bool(from_agent_id_value) == bool(from_user_id_value):
        raise ValueError("on_message requires exactly one canonical sender ID")
    if from_agent_id_value:
        config["from_agent_id"] = str(uuid.UUID(from_agent_id_value))
    if from_user_id_value:
        config["from_user_id"] = str(uuid.UUID(from_user_id_value))
    if notification_summary:
        config["_notification_summary"] = notification_summary
    if origin_session_id:
        config["_origin_session_id"] = origin_session_id
    if origin_user_id:
        config["_origin_user_id"] = origin_user_id
    if origin_source_channel:
        config["_origin_source_channel"] = origin_source_channel
    if origin_external_conv_id is not None:
        config["_origin_external_conv_id"] = origin_external_conv_id
    if origin_turn_anchor_id:
        config["_origin_turn_anchor_id"] = origin_turn_anchor_id
        config["_origin_completion_barrier"] = True
    if watch_session_id:
        config["_watch_session_id"] = watch_session_id
    if watch_source_channel:
        config["_watch_source_channel"] = watch_source_channel
    if watch_actor_ref:
        config["_watch_actor_ref"] = watch_actor_ref
    if outbound_message_id:
        config["_outbound_message_id"] = outbound_message_id
    if outbound_external_message_id:
        config["_outbound_external_message_id"] = outbound_external_message_id
        config["_correlation_mode"] = "reply_to"
    elif watch_session_id:
        config["_correlation_mode"] = "next_message"
    if watch_session_id:
        config["_consume_remote"] = bool(consume_remote)
    config["_set_trigger_context"] = {
        "name": trigger_name,
        "type": "on_message",
        "reason": reason,
        "focus_ref": focus_ref or "",
        "config": {key: value for key, value in config.items() if not str(key).startswith("_")},
    }
    try:
        from app.models.audit import ChatMessage as _CM
        from app.models.chat_session import ChatSession as _CS
        from sqlalchemy import cast as sa_cast, String as SaString
        async with async_session() as _snap_db:
            if origin_turn_anchor_id and origin_session_id:
                try:
                    _origin_anchor = await _snap_db.get(
                        _CM,
                        uuid.UUID(str(origin_turn_anchor_id)),
                    )
                except (TypeError, ValueError):
                    _origin_anchor = None
                if _origin_anchor is not None and _origin_anchor.conversation_id == str(origin_session_id):
                    _origin_meta = _origin_anchor.message_meta if isinstance(_origin_anchor.message_meta, dict) else {}
                    if _origin_meta.get("actor_ref"):
                        config["_origin_actor_ref"] = str(_origin_meta["actor_ref"])
                    if _origin_meta.get("actor_ref_type"):
                        config["_origin_actor_ref_type"] = str(_origin_meta["actor_ref_type"])
            _outbound_anchor = None
            if outbound_message_id:
                try:
                    _outbound_anchor = await _snap_db.get(
                        _CM,
                        uuid.UUID(str(outbound_message_id)),
                    )
                except (TypeError, ValueError):
                    _outbound_anchor = None
            if (
                _outbound_anchor is not None
                and _outbound_anchor.conversation_id == str(watch_session_id or "")
                and _outbound_anchor.created_at is not None
            ):
                config["_since_ts"] = _outbound_anchor.created_at.isoformat()
            else:
                _snap_q = (
                    select(_CM.created_at)
                    .join(_CS, _CM.conversation_id == sa_cast(_CS.id, SaString))
                    .where(
                        _CS.agent_id == agent_id,
                        _CM.created_at.isnot(None),
                    )
                    .order_by(_CM.created_at.desc())
                    .limit(1)
                )
                _snap_r = await _snap_db.execute(_snap_q)
                _latest_ts = _snap_r.scalar_one_or_none()
                if _latest_ts:
                    config["_since_ts"] = _latest_ts.isoformat()
    except Exception:
        pass
    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name == trigger_name,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            if existing.is_enabled:
                existing_cfg = existing.config or {}
                if existing_cfg.get("_origin_session_id") == config.get("_origin_session_id") and existing_cfg.get(
                    "_outbound_message_id"
                ) == config.get("_outbound_message_id"):
                    trigger = existing
                else:
                    raise RuntimeError(
                        f"Trigger '{trigger_name}' already exists and is active; "
                        "use a distinct trigger name or cancel the existing trigger first."
                    )
            else:
                if creator_user_id is not None:
                    from app.services.execution_identity import align_background_execution_user
                    await align_background_execution_user(
                        db,
                        agent_id=agent_id,
                        resource_type="trigger",
                        resource_id=existing.id,
                        execution_user_id=creator_user_id,
                    )
                existing.type = "on_message"
                existing.config = config
                existing.reason = reason
                existing.focus_ref = focus_ref or None
                existing.is_enabled = True
                existing.fire_count = 0
                trigger = existing
        else:
            trigger = AgentTrigger(
                agent_id=agent_id,
                created_by_user_id=creator_user_id,
                execution_user_id=creator_user_id,
                name=trigger_name,
                type="on_message",
                config=config,
                reason=reason,
                focus_ref=focus_ref or None,
                max_fires=1,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=max(1, expires_in_minutes)),
            )
            db.add(trigger)
        await db.commit()
    if watch_session_id:
        from app.services.trigger_runtime.evaluator import recover_exact_on_message_events
        try:
            await recover_exact_on_message_events(trigger)
        except Exception as replay_error:
            logger.warning(
                "[A2A] reply-before-arm recovery failed for %s: %s",
                trigger_name,
                replay_error,
            )


async def _arm_a2a_delegate_callback(
    *,
    from_agent_id: uuid.UUID,
    target: AgentModel,
    message_text: str,
    owner_id: uuid.UUID,
    origin_session_id: str | None,
    origin_source_channel: str,
    origin_external_conv_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    watch_session_id: str,
    watch_actor_ref: str,
    outbound_message_id: uuid.UUID,
) -> None:
    """Arm the existing exact callback path for one A2A delegation event."""
    from app.models.trigger import AgentTrigger
    from sqlalchemy.exc import IntegrityError
    correlation_suffix = outbound_message_id.hex[:12]
    focus_id = f"wait_{target.id.hex[:8]}_{correlation_suffix}_task"
    await _append_focus_item(
        from_agent_id,
        focus_id,
        f"Waiting for {target.name} to complete delegated task: {message_text[:100]}",
    )
    trigger_name = f"a2a_wait_{target.id.hex[:8]}_{correlation_suffix}"
    trigger_reason = (
        f"{target.name} has replied with the result of a delegated task. "
        f"Original task: {message_text[:200]}. "
        f"Steps: 1) Process {target.name}'s reply. "
        f"2) Mark focus item '{focus_id}' as completed. "
        f"3) Cancel this trigger. "
        f"USER-FACING OUTPUT RULES: Your reply goes directly to the user's chat. "
        f"Write in natural, conversational language as if talking to a colleague. "
        f"NEVER use technical terms like: trigger name, focus item, a2a_wait, "
        f"task_delegate, focus_ref, or any internal identifier. "
        f"NEVER mention your internal operations (canceling triggers, updating focus, "
        f"marking items complete, trigger status, etc.). "
        f"Just summarize the task result in plain language."
    )

    def _same_callback(existing: AgentTrigger) -> bool:
        existing_cfg = existing.config if isinstance(existing.config, dict) else {}
        return (
            existing.is_enabled
            and str(existing_cfg.get("_watch_session_id") or "") == watch_session_id
            and str(existing_cfg.get("_outbound_message_id") or "") == str(outbound_message_id)
            and str(existing_cfg.get("_origin_session_id") or "") == str(origin_session_id or "")
        )

    async def _load_existing() -> AgentTrigger | None:
        async with async_session() as db:
            return (
                await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.agent_id == from_agent_id,
                        AgentTrigger.name == trigger_name,
                    )
                )
            ).scalar_one_or_none()

    existing = await _load_existing()
    if existing is not None:
        if _same_callback(existing):
            return
        raise RuntimeError("A2A delegate callback key is already bound to another route")
    try:
        await _create_on_message_trigger(
            agent_id=from_agent_id,
            trigger_name=trigger_name,
            from_agent_id_value=str(target.id),
            reason=trigger_reason,
            focus_ref=focus_id,
            notification_summary=f"等待{target.name}完成任务并回复",
            origin_session_id=origin_session_id,
            origin_user_id=str(owner_id),
            origin_source_channel=origin_source_channel,
            origin_external_conv_id=origin_external_conv_id,
            origin_turn_anchor_id=str(origin_turn_anchor_id or "") or None,
            watch_session_id=watch_session_id,
            watch_source_channel="agent",
            watch_actor_ref=watch_actor_ref,
            outbound_message_id=str(outbound_message_id),
            consume_remote=True,
        )
    except IntegrityError:
        existing = await _load_existing()
        if existing is not None and _same_callback(existing):
            return
        raise


async def _append_focus_item(agent_id: uuid.UUID, identifier: str, description: str) -> None:
    """Create or update an in-progress Focus item."""
    try:
        await ensure_focus_item(agent_id, focus_ref=identifier, description=description)
    except Exception as e:
        logger.warning(f"[A2A] Failed to update Focus for agent {agent_id}: {e}")


async def _wake_agent_async(
    agent_id: uuid.UUID,
    reason_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    skip_dedup: bool = False,
    a2a_session_id: str | None = None,
) -> None:
    """Wake an agent asynchronously via the trigger invocation path.

    Delegates to the public wake_agent_with_context API in trigger_daemon.
    """
    from app.services.trigger_daemon import wake_agent_with_context
    kwargs = {"from_agent_id": from_agent_id, "skip_dedup": skip_dedup}
    if a2a_session_id is not None:
        kwargs["a2a_session_id"] = a2a_session_id
    await wake_agent_with_context(agent_id, reason_context, **kwargs)
