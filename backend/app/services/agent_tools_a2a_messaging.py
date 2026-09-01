from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.agent_tools import (
    A2A_DELIVERY_GUIDANCE,
    _arm_a2a_delegate_callback,
    _build_outbound_operation_key,
    _lock_outbound_operation,
    _wake_agent_async,
    logger,
)
from app.services.recipient_resolver import RecipientResolutionError, resolve_agent_recipient


async def _send_message_to_agent(
    from_agent_id: uuid.UUID,
    args: dict,
    user_id: uuid.UUID | None = None,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a message to another digital employee.

    Behaviour depends on ``msg_type``:
    - notify:   fire-and-forget — message is saved, target is woken asynchronously.
                Returns immediately.
    - task_delegate: async with callback — message is saved, source agent sets up
                a focus item + on_message trigger so it is notified when the
                target completes the task.  Returns immediately.
    - consult:  synchronous request-response (original behaviour).

    ``user_id`` / ``origin_session_id`` (added by the A2A trigger-routing fix):
    attribute the A2A session to the real caller and let a task_delegate callback
    trigger remember WHERE the originating conversation lived so the eventual
    reply is routed back to it.
    """
    recoverable_anchor = None
    canonical_agent_id = str(args.get("agent_id") or "").strip()
    message_text = args.get("message", "").strip()
    msg_type = args.get("msg_type", "notify").strip().lower()
    force_async = bool(args.get("force_async"))
    new_conversation = bool(args.get("new_conversation"))
    project_id = None
    if args.get("_project_id"):
        try:
            project_id = uuid.UUID(str(args["_project_id"]))
        except (TypeError, ValueError):
            return "❌ _project_id must be a complete platform UUID"
    work_item_id = None
    if args.get("_work_item_id"):
        try:
            work_item_id = uuid.UUID(str(args["_work_item_id"]))
        except (TypeError, ValueError):
            return "❌ _work_item_id must be a complete platform UUID"
        if project_id is None:
            return "❌ _work_item_id requires a project-scoped message"
    if not canonical_agent_id or not message_text:
        return "❌ Please provide canonical agent_id and message content"
    try:
        from app.models.participant import Participant
        from datetime import datetime, timezone
        origin_source_channel = "web"
        origin_external_conv_id = None
        async with async_session() as db:
            if origin_session_id:
                try:
                    _osr = await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(origin_session_id)))
                    _osess = _osr.scalar_one_or_none()
                    if _osess:
                        origin_source_channel = _osess.source_channel
                        origin_external_conv_id = _osess.external_conv_id
                except Exception:
                    pass
            try:
                recipient = await resolve_agent_recipient(db, from_agent_id, canonical_agent_id, project_id=project_id)
            except RecipientResolutionError as exc:
                return exc.as_json()
            source_agent = recipient.source_agent
            target = recipient.target_agent
            source_name = source_agent.name
            src_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == from_agent_id)
            )
            src_participant = src_part_r.scalar_one_or_none()
            tgt_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == target.id)
            )
            tgt_participant = tgt_part_r.scalar_one_or_none()
            outbound_operation_key = _build_outbound_operation_key(
                agent_id=from_agent_id,
                origin_session_id=origin_session_id,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
            )
            recorded_openclaw_outbound = None
            recorded_project_outbound = None
            if getattr(target, "agent_type", "native") == "openclaw" and outbound_operation_key:
                await _lock_outbound_operation(db, outbound_operation_key)
                candidate = (
                    await db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.external_event_key == outbound_operation_key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if candidate is not None:
                    candidate_meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                    if str(candidate_meta.get("target_agent_id") or "") != str(target.id):
                        return "❌ The replayed message receipt does not match the requested target"
                    if candidate_meta.get("delivery_status") == "queued":
                        return f"✅ Message already sent to {target.name} via agent (idempotent replay)."
                    if candidate_meta.get("delivery_status") != "recorded":
                        return "❌ The replayed message receipt has an invalid delivery state"
                    try:
                        replay_session = await db.get(
                            ChatSession,
                            uuid.UUID(str(candidate.conversation_id)),
                        )
                    except (TypeError, ValueError):
                        replay_session = None
                    if replay_session is None:
                        return "❌ The recorded message's conversation no longer exists"
                    expected_pair = {from_agent_id, target.id}
                    if (
                        {
                            replay_session.agent_id,
                            replay_session.peer_agent_id,
                        }
                        != expected_pair
                        or replay_session.source_channel != "agent"
                        or replay_session.project_id != project_id
                    ):
                        return "❌ The recorded message's conversation route changed"
                    recorded_openclaw_outbound = candidate
            elif project_id is not None and outbound_operation_key:
                await _lock_outbound_operation(db, outbound_operation_key)
                candidate = (
                    await db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.external_event_key == outbound_operation_key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if candidate is not None:
                    candidate_meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                    if str(candidate_meta.get("target_agent_id") or "") != str(target.id):
                        return "❌ The replayed project message does not match the requested target"
                    if str(candidate_meta.get("work_item_id") or "") != str(work_item_id or ""):
                        return "❌ The replayed project message does not match the requested work item"
                    try:
                        replay_session = await db.get(
                            ChatSession,
                            uuid.UUID(str(candidate.conversation_id)),
                        )
                    except (TypeError, ValueError):
                        replay_session = None
                    expected_pair = {from_agent_id, target.id}
                    if (
                        replay_session is None
                        or replay_session.source_channel != "agent"
                        or replay_session.project_id != project_id
                        or {replay_session.agent_id, replay_session.peer_agent_id} != expected_pair
                    ):
                        return "❌ The replayed project message's conversation route changed"
                    recorded_project_outbound = candidate
            session_agent_id = min(from_agent_id, target.id, key=str)
            session_peer_id = max(from_agent_id, target.id, key=str)
            owner_id = user_id or (source_agent.creator_id if source_agent else from_agent_id)
            chat_session = (
                replay_session
                if recorded_openclaw_outbound is not None or recorded_project_outbound is not None
                else None
            )
            if chat_session is None and not new_conversation:
                sess_r = await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.agent_id == session_agent_id,
                        ChatSession.peer_agent_id == session_peer_id,
                        ChatSession.source_channel == "agent",
                        ChatSession.project_id == project_id if project_id else ChatSession.project_id.is_(None),
                    )
                    .order_by(
                        ChatSession.last_message_at.desc().nulls_last(),
                        ChatSession.created_at.desc(),
                    )
                    .limit(1)
                )
                chat_session = sess_r.scalars().first()
            if not chat_session:
                _ext = f"project-a2a:{project_id}:{session_peer_id}" if project_id else None
                _suffix = ""
                if new_conversation:
                    from sqlalchemy import func as _sa_func
                    _ext = f"a2a-{uuid.uuid4().hex[:8]}"
                    _cnt_r = await db.execute(
                        select(_sa_func.count())
                        .select_from(ChatSession)
                        .where(
                            ChatSession.agent_id == session_agent_id,
                            ChatSession.peer_agent_id == session_peer_id,
                            ChatSession.source_channel == "agent",
                            ChatSession.project_id == project_id if project_id else ChatSession.project_id.is_(None),
                        )
                    )
                    _suffix = f" #{(_cnt_r.scalar() or 0) + 1}"
                src_part_id = src_participant.id if src_participant else None
                chat_session = ChatSession(
                    agent_id=session_agent_id,
                    project_id=project_id,
                    user_id=owner_id,
                    title=f"{source_name} ↔ {target.name}{_suffix}",
                    source_channel="agent",
                    participant_id=src_part_id,
                    peer_agent_id=session_peer_id,
                    external_conv_id=_ext,
                )
                db.add(chat_session)
                await db.flush()
            session_id = str(chat_session.id)
            if getattr(target, "agent_type", "native") == "openclaw":
                outbound_a2a_message = recorded_openclaw_outbound
                if outbound_a2a_message is None:
                    outbound_a2a_message = ChatMessage(
                        id=uuid.uuid4(),
                        agent_id=session_agent_id,
                        user_id=owner_id,
                        sender_agent_id=from_agent_id,
                        role="user",
                        content=message_text,
                        conversation_id=session_id,
                        participant_id=src_participant.id if src_participant else None,
                        external_event_key=outbound_operation_key,
                        message_meta={
                            "direction": "outbound",
                            "source_channel": "agent",
                            "origin_session_id": str(origin_session_id or ""),
                            "origin_source_channel": origin_source_channel,
                            "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                            "tool_call_id": str(tool_call_id or ""),
                            "actor_ref": str(tgt_participant.id if tgt_participant else target.id),
                            "target_agent_id": str(target.id),
                            "target_name": target.name,
                            "delivery_status": "recorded",
                        },
                    )
                    db.add(outbound_a2a_message)
                    chat_session.last_message_at = datetime.now(timezone.utc)
                    await db.commit()
                if msg_type == "task_delegate":
                    try:
                        await _arm_a2a_delegate_callback(
                            from_agent_id=from_agent_id,
                            target=target,
                            message_text=message_text,
                            owner_id=owner_id,
                            origin_session_id=origin_session_id,
                            origin_source_channel=origin_source_channel,
                            origin_external_conv_id=origin_external_conv_id,
                            origin_turn_anchor_id=origin_turn_anchor_id,
                            watch_session_id=session_id,
                            watch_actor_ref=str(tgt_participant.id if tgt_participant else target.id),
                            outbound_message_id=outbound_a2a_message.id,
                        )
                    except Exception as e:
                        logger.exception(f"[A2A] Failed to create OpenClaw delegate callback: {e}")
                        return (
                            "❌ The delegated message was recorded, but its delivery state "
                            "could not be confirmed. Please retry; the operation is idempotent."
                        )
                await _lock_outbound_operation(db, outbound_operation_key)
                await db.refresh(outbound_a2a_message)
                refreshed_meta = (
                    outbound_a2a_message.message_meta if isinstance(outbound_a2a_message.message_meta, dict) else {}
                )
                if refreshed_meta.get("delivery_status") == "queued":
                    return f"✅ Message already sent to {target.name} via agent (idempotent replay)."
                if refreshed_meta.get("delivery_status") != "recorded":
                    return "❌ The replayed message receipt has an invalid delivery state"
                from app.models.gateway_message import GatewayMessage as GMsg
                gw_msg = GMsg(
                    agent_id=target.id,
                    sender_agent_id=from_agent_id,
                    content=f"[From {source_name}] {message_text}",
                    status="pending",
                    conversation_id=session_id,
                )
                db.add(gw_msg)
                outbound_a2a_message.message_meta = {
                    **refreshed_meta,
                    "delivery_status": "queued",
                }
                await db.commit()
                from app.services.activity_logger import log_activity
                await log_activity(
                    from_agent_id,
                    "agent_msg_sent",
                    f"Sent message to {target.name} (queued)",
                    detail={"partner": target.name, "message": message_text[:200]},
                )
                online = (
                    target.openclaw_last_seen
                    and (datetime.now(timezone.utc) - target.openclaw_last_seen).total_seconds() < 300
                )
                status_hint = "online" if online else "offline (message will be delivered on next heartbeat)"
                if msg_type == "task_delegate":
                    return (
                        f"✅ Task delegated to {target.name} (OpenClaw agent, currently {status_hint}). "
                        "You will be notified when they complete it."
                    )
                return f"✅ Message sent to {target.name} (OpenClaw agent, currently {status_hint}). The message has been queued and will be delivered when the agent polls for updates."
            _a2a_async = False
            if source_agent.tenant_id:
                try:
                    from app.models.tenant import Tenant
                    _t_r = await db.execute(select(Tenant).where(Tenant.id == source_agent.tenant_id))
                    _tenant = _t_r.scalar_one_or_none()
                    if _tenant:
                        _a2a_async = getattr(_tenant, "a2a_async_enabled", False)
                except Exception:
                    pass
            if not _a2a_async and not force_async:
                if msg_type in ("notify", "task_delegate"):
                    msg_type = "consult"
            if msg_type == "consult":
                from app.services.active_turns import ensure_active_turn
                await ensure_active_turn(
                    owner_user_id=owner_id,
                    agent_id=target.id,
                    session_id=session_id,
                    turn_type="agent",
                    title=message_text.strip()[:40] or None,
                )
            outbound_a2a_message = recorded_project_outbound
            if (
                outbound_a2a_message is None
                and msg_type == "consult"
                and project_id is None
            ):
                from app.services.chat_history import ingest_incoming_chat_message
                ingested = await ingest_incoming_chat_message(
                    db,
                    session=chat_session,
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    content=message_text,
                    source_channel="agent",
                    provider_event_id=(
                        outbound_operation_key or f"native-consult:{uuid.uuid4()}"
                    ),
                    channel_config_id="native-a2a",
                    actor_ref=str(
                        src_participant.id if src_participant else from_agent_id
                    ),
                    participant_id=(
                        src_participant.id if src_participant else None
                    ),
                    message_meta={
                        "origin_session_id": str(origin_session_id or ""),
                        "origin_source_channel": origin_source_channel,
                        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                        "tool_call_id": str(tool_call_id or ""),
                        "execution_agent_id": str(target.id),
                        "target_agent_id": str(target.id),
                        "target_name": target.name,
                    },
                )
                outbound_a2a_message = ingested.message
                outbound_a2a_message.sender_user_id = None
                outbound_a2a_message.sender_agent_id = from_agent_id
                chat_session.last_message_at = datetime.now(timezone.utc)
                if ingested.consumed_by_onmessage:
                    await db.commit()
                    inbox_mode = dict(
                        outbound_a2a_message.message_meta or {}
                    ).get("turn_inbox_mode")
                    return (
                        f"✅ Message to {target.name} was "
                        + (
                            "merged into the current durable conversation turn."
                            if inbox_mode == "current_turn"
                            else "queued for the next durable conversation turn."
                        )
                    )
            elif outbound_a2a_message is None:
                outbound_a2a_message = ChatMessage(
                    id=uuid.uuid4(),
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    sender_agent_id=from_agent_id,
                    role="user",
                    content=message_text,
                    conversation_id=session_id,
                    participant_id=src_participant.id if src_participant else None,
                    external_event_key=outbound_operation_key,
                    message_meta={
                        "direction": "outbound",
                        "source_channel": "agent",
                        "origin_session_id": str(origin_session_id or ""),
                        "origin_source_channel": origin_source_channel,
                        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                        "tool_call_id": str(tool_call_id or ""),
                        "actor_ref": str(tgt_participant.id if tgt_participant else target.id),
                        **(
                            {"execution_agent_id": str(target.id)}
                            if msg_type == "consult" and project_id is None
                            else {}
                        ),
                        "target_agent_id": str(target.id),
                        "target_name": target.name,
                        "work_item_id": str(work_item_id) if work_item_id else None,
                    },
                )
                db.add(outbound_a2a_message)
                chat_session.last_message_at = datetime.now(timezone.utc)
            if msg_type == "consult":
                from app.services.active_turns import commit_current_turn_anchor
                await commit_current_turn_anchor(
                    db.commit,
                    agent_id=session_agent_id,
                    session_id=session_id,
                    message_id=outbound_a2a_message.id,
                )
                if project_id is None:
                    recoverable_anchor = outbound_a2a_message
            else:
                await db.commit()
            if project_id is not None:
                from app.services.subagent_runtime import enqueue_project_a2a_run
                raw_project_run_id = args.get("_project_run_id") or dict(outbound_a2a_message.message_meta or {}).get(
                    "project_run_id"
                )
                try:
                    scoped_project_run_id = uuid.UUID(str(raw_project_run_id)) if raw_project_run_id else None
                except (TypeError, ValueError):
                    return "❌ _project_run_id must be a complete platform UUID"
                raw_parent_project_run_id = args.get("_parent_project_run_id")
                try:
                    parent_project_run_id = (
                        uuid.UUID(str(raw_parent_project_run_id)) if raw_parent_project_run_id else None
                    )
                except (TypeError, ValueError):
                    return "❌ _parent_project_run_id must be a complete platform UUID"
                try:
                    dispatch_result = await enqueue_project_a2a_run(
                        project_id=project_id,
                        a2a_session_id=chat_session.id,
                        outbound_message_id=outbound_a2a_message.id,
                        from_agent_id=from_agent_id,
                        to_agent_id=target.id,
                        execution_user_id=owner_id,
                        message=message_text,
                        mode=msg_type,
                        project_run_id=scoped_project_run_id,
                        parent_project_run_id=parent_project_run_id,
                        work_item_id=work_item_id,
                        run_title=str(args.get("_run_title") or "").strip() or None,
                    )
                except Exception as exc:
                    logger.exception("[project-a2a] durable target dispatch failed: {}", exc)
                    return "❌ 成员协作请求未能提交，请稍后重试。"
                return json.dumps(
                    {
                        "status": "queued",
                        "message": f"已向 {target.name} 提交成员协作请求",
                        "session_id": session_id,
                        "a2a_session_id": session_id,
                        "project_run_id": str(scoped_project_run_id or dispatch_result.get("project_run_id") or ""),
                        "subagent_run_id": dispatch_result.get("subagent_run_id"),
                        "subagent_session_id": dispatch_result.get("subagent_session_id"),
                        "awakened_agent_ids": [str(target.id)],
                    },
                    ensure_ascii=False,
                )
            if msg_type == "notify":
                try:
                    from app.services.activity_logger import log_activity
                    await log_activity(
                        from_agent_id,
                        "agent_msg_sent",
                        f"Sent notification to {target.name}",
                        detail={"partner": target.name, "message": message_text[:200], "msg_type": "notify"},
                    )
                except Exception:
                    pass
                try:
                    await _wake_agent_async(
                        target.id,
                        f"[From {source_name}] {message_text}",
                        from_agent_id=from_agent_id,
                        skip_dedup=True,
                        a2a_session_id=session_id,
                    )
                except Exception as e:
                    logger.warning(f"[A2A] Failed to wake {target.name} for notify: {e}")
                return f"✅ Notification sent to {target.name}. They will process it asynchronously."
            if msg_type == "task_delegate":
                try:
                    await _arm_a2a_delegate_callback(
                        from_agent_id=from_agent_id,
                        target=target,
                        message_text=message_text,
                        owner_id=owner_id,
                        origin_session_id=origin_session_id,
                        origin_source_channel=origin_source_channel,
                        origin_external_conv_id=origin_external_conv_id,
                        origin_turn_anchor_id=origin_turn_anchor_id,
                        watch_session_id=session_id,
                        watch_actor_ref=str(tgt_participant.id if tgt_participant else target.id),
                        outbound_message_id=outbound_a2a_message.id,
                    )
                except Exception as e:
                    logger.exception(f"[A2A] Failed to create trigger for delegate: {e}")
                    await db.delete(outbound_a2a_message)
                    await db.commit()
                    return (
                        "❌ The delegated message was recorded, but its reply subscription "
                        "could not be created. The target was not awakened; please retry."
                    )
                try:
                    from app.services.activity_logger import log_activity
                    await log_activity(
                        from_agent_id,
                        "agent_msg_sent",
                        f"Delegated task to {target.name}",
                        detail={"partner": target.name, "message": message_text[:200], "msg_type": "task_delegate"},
                    )
                except Exception:
                    pass
                try:
                    await _wake_agent_async(
                        target.id,
                        f"[From {source_name}] {message_text}",
                        from_agent_id=from_agent_id,
                        skip_dedup=True,
                        a2a_session_id=session_id,
                    )
                except Exception as e:
                    logger.warning(f"[A2A] Failed to wake {target.name} for delegate: {e}")
                return f"✅ Task delegated to {target.name}. You will be notified when they complete it."
            from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
            from app.models.llm import LLMModel
            from app.services.chat_history import (
                load_history_for_llm,
                persist_tool_call,
                strip_leading_orphan_tool_messages,
            )
            from app.services.llm import call_llm_with_failover
            target_model = None
            if target.primary_model_id:
                _m = await db.execute(select(LLMModel).where(LLMModel.id == target.primary_model_id))
                target_model = _m.scalar_one_or_none()
                if target_model and not target_model.enabled:
                    target_model = None
            target_fallback = None
            if target.fallback_model_id:
                _fb = await db.execute(select(LLMModel).where(LLMModel.id == target.fallback_model_id))
                target_fallback = _fb.scalar_one_or_none()
                if target_fallback and not target_fallback.enabled:
                    target_fallback = None
            if not target_model and target_fallback:
                target_model, target_fallback = target_fallback, None
            if not target_model:
                return f"⚠️ {target.name} has no LLM model configured"
            ctx_size = target.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
            history = await load_history_for_llm(
                db,
                agent_id=session_agent_id,
                conversation_id=session_id,
                ctx_size=ctx_size,
            )
            messages = strip_leading_orphan_tool_messages(history)
            turn_text = "[From " + source_name + "] " + message_text + "\n\n" + A2A_DELIVERY_GUIDANCE
            if messages and messages[-1].get("role") == "user":
                messages[-1] = {"role": "user", "content": turn_text}
            else:
                messages.append({"role": "user", "content": turn_text})
            from app.services.llm.turn_partition import effective_keep_recent_turns
            protected_keep_recent_turns = effective_keep_recent_turns(
                target_model,
                target_fallback,
            )
            async def _a2a_context_recovery(_recovery_model, dispatch_budget):
                from app.services.chat_history import load_recoverable_history_for_turn
                from app.services.llm.compactor import (
                    COMPACTION_NOT_APPLICABLE_REASONS,
                    ContextRecoveryMessages,
                    maybe_compact,
                )
                compacted = await maybe_compact(
                    agent_id=session_agent_id,
                    conversation_id=session_id,
                    model=_recovery_model,
                    last_prompt_tokens=getattr(dispatch_budget, "authoritative_prompt_tokens", None),
                    current_anchor_id=outbound_a2a_message.id,
                    force_required=getattr(dispatch_budget, "provider_overflow", False),
                    keep_recent_turns_override=(
                        getattr(dispatch_budget, "keep_recent_turns_override", None)
                        if getattr(dispatch_budget, "provider_overflow", False)
                        else protected_keep_recent_turns
                    ),
                )
                preflight_not_applicable = (
                    not compacted.triggered
                    and compacted.skipped_reason
                    in COMPACTION_NOT_APPLICABLE_REASONS
                    and dispatch_budget.fits
                )
                if not compacted.triggered and not preflight_not_applicable:
                    logger.warning(
                        f"[A2A] context recovery could not compact session={session_id}: {compacted.skipped_reason}"
                    )
                    return None
                async with async_session() as recovery_db:
                    recovered = await load_recoverable_history_for_turn(
                        recovery_db,
                        agent_id=session_agent_id,
                        conversation_id=session_id,
                        turn_anchor_id=outbound_a2a_message.id,
                        ctx_size=ctx_size,
                    )
                if not recovered:
                    logger.warning(f"[A2A] context recovery lost latest-anchor race session={session_id}")
                    return None
                return ContextRecoveryMessages(
                    recovered,
                    preflight_not_applicable=preflight_not_applicable,
                )
            async def _a2a_persist(evt: dict):
                await persist_tool_call(
                    async_session,
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    conversation_id=session_id,
                    evt=evt,
                    turn_anchor_id=outbound_a2a_message.id,
                )
            _a2a_thinking: list[str] = []
            async def _a2a_on_thinking(text: str):
                _a2a_thinking.append(text)
            from app.services.subagent_runtime import (
                build_parent_subagent_before_round,
            )
            before_round = build_parent_subagent_before_round(
                parent_session_id=session_id,
                active_turn_anchor_id=outbound_a2a_message.id,
                execution_agent_id=target.id,
                execution_user_id=owner_id,
                turn_anchor_agent_id=session_agent_id,
                include_turn_inbox=True,
            )
            target_reply = await call_llm_with_failover(
                primary_model=target_model,
                fallback_model=target_fallback,
                messages=messages,
                agent_name=target.name,
                role_description=target.role_description or "",
                agent_id=target.id,
                user_id=owner_id,
                session_id=session_id,
                on_tool_call=_a2a_persist,
                on_thinking=_a2a_on_thinking,
                turn_anchor_id=outbound_a2a_message.id,
                turn_anchor_agent_id=session_agent_id,
                context_recovery=_a2a_context_recovery,
                before_round=before_round,
            )
            if not target_reply:
                return f"⚠️ {target.name} did not respond (LLM returned empty)"
            async with async_session() as db2:
                part_r = await db2.execute(
                    select(Participant).where(Participant.type == "agent", Participant.ref_id == target.id)
                )
                tgt_part = part_r.scalar_one_or_none()
                from app.services.chat_history import (
                    persist_assistant_reply_row,
                )
                assistant_message_id = await persist_assistant_reply_row(
                    db2,
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    conversation_id=session_id,
                    content=target_reply,
                    turn_anchor_id=outbound_a2a_message.id,
                    sender_agent_id=target.id,
                    participant_id=tgt_part.id if tgt_part else None,
                    thinking="".join(_a2a_thinking),
                )
                await db2.commit()
            from app.services.conversation_turn_lifecycle import (
                publish_committed_turn_terminal,
            )
            await publish_committed_turn_terminal(
                agent_id=session_agent_id,
                conversation_id=session_id,
                turn_anchor_id=outbound_a2a_message.id,
                message_id=assistant_message_id,
                content=target_reply,
            )
            from app.services.activity_logger import log_activity
            await log_activity(
                target.id,
                "agent_msg_sent",
                f"Replied to message from {source_name}",
                detail={"partner": source_name, "message": message_text[:200], "reply": target_reply[:200]},
            )
            await log_activity(
                from_agent_id,
                "agent_msg_sent",
                f"Sent message to {target.name} and received reply",
                detail={"partner": target.name, "message": message_text[:200], "reply": target_reply[:200]},
            )
            return f"💬 {target.name} replied:\n{target_reply}"
    except Exception as e:
        from app.services.redis_lease_lock import RedisLeaseError
        if recoverable_anchor is not None and isinstance(e, RedisLeaseError):
            from app.services.turn_inbox import schedule_durable_turn_resume
            schedule_durable_turn_resume(recoverable_anchor)
        logger.exception(f"[A2A] send_message_to_agent failed: from={from_agent_id}, to={args.get('agent_id', '')}")
        error_type = type(e).__name__
        error_detail = (str(e) or "").strip()
        if not error_detail:
            timeout_types = {"ReadTimeout", "ConnectTimeout", "TimeoutException"}
            if error_type in timeout_types:
                error_detail = "LLM request timed out while waiting for target agent response"
            else:
                error_detail = "No detailed error message returned from upstream"
        return f"❌ Message send error ({error_type}): {error_detail[:200]}"
