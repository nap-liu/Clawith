"""Trigger delivery targeting and durable execution bookkeeping."""

from app.services.trigger_daemon_evaluation import *  # noqa: F401,F403

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

        db.add(
            AuditLog(
                agent_id=agent_id,
                action="webhook_session_failed",
                details={"trigger_name": name, "payload": str(detail)[:2000]},
            )
        )
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


async def _finalize_invocation_executions(
    execution_ids: list[uuid.UUID],
    triggers: list[AgentTrigger],
    reply: str | None,
    invocation_error: str | None,
    invocation_retryable: bool,
    conversation_id: uuid.UUID | None,
) -> None:
    """Finalize durable executions and webhook queue state atomically.

    Webhook append, claim, and advance all read-modify-write the same JSONB
    config.  Advance therefore holds the trigger row lock, and its execution
    terminal state is committed in the same transaction.  A rollback leaves
    both the active batch and its processing execution recoverable together.
    """
    if not execution_ids:
        return

    now = datetime.now(timezone.utc)
    async with async_session() as db:
        valid_conversation_id: uuid.UUID | None = None
        if conversation_id is not None:
            from app.models.chat_session import ChatSession

            if await db.get(ChatSession, conversation_id) is not None:
                valid_conversation_id = conversation_id
            else:
                logger.warning(
                    "Skipping stale conversation link %s while finalizing executions %s",
                    conversation_id,
                    execution_ids,
                )
        executions = (
            (await db.execute(select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids)).with_for_update()))
            .scalars()
            .all()
        )
        for execution in executions:
            if valid_conversation_id is not None:
                execution.conversation_id = valid_conversation_id
            if invocation_error is None:
                execution.status = "completed"
                execution.finished_at = now
                execution.last_error = None
            elif invocation_retryable:
                execution.status = "pending"
                execution.finished_at = None
                execution.last_error = invocation_error
            else:
                execution.status = "failed"
                execution.finished_at = now
                execution.last_error = invocation_error
            execution.lease_owner = None
            execution.lease_expires_at = None

        for runtime_trigger in triggers:
            if runtime_trigger.type != "webhook":
                continue
            stored_trigger = (
                await db.execute(select(AgentTrigger).where(AgentTrigger.id == runtime_trigger.id).with_for_update())
            ).scalar_one_or_none()
            if stored_trigger is not None:
                _advance_webhook_trigger(db, stored_trigger, reply)

        await db.commit()


async def _link_invocation_executions(
    execution_ids: list[uuid.UUID],
    conversation_id: uuid.UUID,
) -> None:
    """Expose a canonical conversation as soon as execution starts."""
    if not execution_ids:
        return
    async with async_session() as db:
        from app.models.chat_session import ChatSession

        session = await db.get(ChatSession, conversation_id)
        if session is None:
            raise RuntimeError("Origin conversation no longer exists")
        executions = (
            (await db.execute(select(TriggerExecution).where(TriggerExecution.id.in_(execution_ids)))).scalars().all()
        )
        for execution in executions:
            if session.agent_id != execution.agent_id and session.peer_agent_id != execution.agent_id:
                raise RuntimeError("Origin conversation belongs to a different agent")
            execution.conversation_id = conversation_id
        await db.commit()


_ONMESSAGE_TURN_NAMESPACE = uuid.UUID("1cb1fc5c-c7c4-4f02-aa83-fab956557622")


class RetryableOnMessageError(RuntimeError):
    """A durable on_message turn completed locally but delivery should retry."""


def _is_retryable_invocation_error(error: Exception) -> bool:
    """Return whether a durable trigger execution should return to pending."""

    return isinstance(
        error,
        (RetryableOnMessageError, WorkloadOverloadedError, RedisLeaseError),
    )


async def _create_failed_trigger_conversation(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    error: str,
) -> uuid.UUID | None:
    """Persist a failed run in the same conversation store used by successful runs."""
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant

    try:
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
            if agent is None:
                return None
            participant = (
                await db.execute(
                    select(Participant).where(
                        Participant.type == "agent",
                        Participant.ref_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            names = [trigger.name for trigger in triggers] or ["trigger"]
            source_lines = [
                f"Trigger: {trigger.name} ({trigger.type})\nReason: {trigger.reason or '-'}" for trigger in triggers
            ]
            now = datetime.now(timezone.utc)
            session = ChatSession(
                agent_id=agent_id,
                user_id=None,
                participant_id=participant.id if participant else None,
                source_channel="trigger",
                title=f"Execution failed: {', '.join(names)}"[:200],
                last_message_at=now,
            )
            db.add(session)
            await db.flush()
            db.add_all(
                [
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=str(session.id),
                        role="user",
                        content="===== Wake Context =====\n" + "\n---\n".join(source_lines),
                        participant_id=participant.id if participant else None,
                    ),
                    ChatMessage(
                        agent_id=agent_id,
                        conversation_id=str(session.id),
                        role="assistant",
                        content=f"Execution failed before completion.\n\n{error}",
                        participant_id=participant.id if participant else None,
                    ),
                ]
            )
            await db.commit()
            return session.id
    except Exception as persist_error:
        logger.warning(
            "Failed to persist failed trigger conversation for agent {}: {}",
            agent_id,
            persist_error,
        )
        return None


async def _resume_origin_session_for_on_message(
    agent_id: uuid.UUID,
    trigger: AgentTrigger,
    *,
    tenant_id: uuid.UUID | str | None = None,
) -> str:
    """Start one real event turn inside the exact originating ChatSession.

    An on_message trigger is a subscription.  The inbound row can therefore
    match several triggers, but every (trigger, inbound event) execution gets
    its own idempotent event turn carrying the trigger's arm-time context.
    The original send turn is never resumed or reused as the new anchor.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.services.channel_dispatch import (
        ChannelReactions,
        chat_session_lock_key,
        run_channel_message,
    )
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import (
        load_recoverable_history_for_turn,
        persist_assistant_reply_row,
    )

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
                origin.source_channel == "agent" and agent_id in {origin.agent_id, origin.peer_agent_id}
            )
            if not owns_origin:
                raise RuntimeError("on_message origin session belongs to another agent")
            expected_channel = str(cfg.get("_origin_source_channel") or "").strip()
            if expected_channel and origin.source_channel != expected_channel:
                raise RuntimeError("on_message origin source channel changed")
            if "_origin_external_conv_id" in cfg and origin.external_conv_id != cfg.get("_origin_external_conv_id"):
                raise RuntimeError("on_message origin session generation changed")
            expected_watch = str(cfg.get("_watch_session_id") or cfg.get("_matched_session_id") or "")
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
                if origin_turn_anchor is None or origin_turn_anchor.conversation_id != str(origin.id):
                    raise RuntimeError("on_message origin turn anchor changed")
                completion_query = select(ChatMessage.id).where(
                    ChatMessage.conversation_id == str(origin.id),
                    ChatMessage.role == "assistant",
                    ChatMessage.message_meta["turn_anchor_id"].as_string() == str(origin_turn_anchor_id),
                    ChatMessage.message_meta["turn_status"].as_string() == "completed",
                )
                completion_id = (
                    await db.execute(completion_query.order_by(ChatMessage.created_at.asc()).limit(1))
                ).scalar_one_or_none()
                if completion_id is None:
                    raise RetryableOnMessageError("on_message origin turn has not completed yet")

            # Validate the immutable origin envelope before every retry.  A
            # persisted final from an older session generation must never be
            # delivered after /new rotates the external conversation id.
            existing_final = await db.get(ChatMessage, final_id)
            if existing_final is not None:
                from app.services.llm.failure_outcome import (
                    MODEL_RESPONSE_IDLE_TIMEOUT_CODE,
                    model_response_idle_timeout_failure,
                )

                if (existing_final.message_meta or {}).get("error_code") == MODEL_RESPONSE_IDLE_TIMEOUT_CODE:
                    return model_response_idle_timeout_failure()
                return existing_final.content

            owner_user_id = origin.user_id
            configured_user = cfg.get("_execution_user_id") or cfg.get("_origin_user_id")
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
            trigger_context = (
                cfg.get("_trigger_context")
                or cfg.get("_set_trigger_context")
                or {
                    "name": trigger.name,
                    "type": trigger.type,
                    "reason": trigger.reason,
                    "focus_ref": trigger.focus_ref or "",
                    "config": {key: value for key, value in cfg.items() if not str(key).startswith("_")},
                }
            )
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
                    external_event_key=(f"onmessage-wake:{trigger.id}:{matched.id}"[:500]),
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
                model_override_id=trigger.model_id,
                temperature_override=trigger.temperature,
                reasoning_effort_override=trigger.reasoning_effort,
                include_soul=trigger.soul,
                include_memory=trigger.memory,
            )
            if reply and reply.strip():
                final_meta = {
                    "kind": "on_message_final",
                    "trigger_execution_id": str(execution_id),
                    "matched_message_id": str(matched_id),
                }
                from app.services.im_delivery import (
                    IMDeliveryResult,
                    attach_delivery_to_meta,
                )

                final_meta = attach_delivery_to_meta(
                    final_meta,
                    IMDeliveryResult.pending(str(origin.source_channel or "web")),
                )
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
                                origin_agent_participant.id if origin_agent_participant is not None else agent_id
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
            from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

            await publish_committed_turn_terminal(
                agent_id=history_agent_id,
                conversation_id=str(origin.id),
                turn_anchor_id=anchor_id,
                message_id=final_id,
                content=reply,
            )
            return reply

    async def _work_and_deliver() -> str:
        """Keep turn creation and origin transport ordering in one session lock."""
        reply = await _work()
        if not reply or not reply.strip():
            return reply

        async with async_session() as db:
            final_row = await db.get(ChatMessage, final_id)
            final_meta = (
                final_row.message_meta if final_row is not None and isinstance(final_row.message_meta, dict) else {}
            )
            if final_meta.get("origin_delivery_status") == "delivered":
                return reply

        from app.services.turn_runtime import (
            deliver_recovered_reply_to_origin,
            load_turn_runtime,
        )

        runtime = await load_turn_runtime(
            agent_id=agent_id,
            conversation_id=str(origin_id),
        )
        if (
            runtime.source_channel != str(cfg.get("_origin_source_channel") or "")
            or (
                "_origin_external_conv_id" in cfg
                and runtime.external_conv_id != cfg.get("_origin_external_conv_id")
            )
        ):
            raise RetryableOnMessageError("on_message origin generation changed")
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
            message_id=final_id,
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

    async with async_session() as db:
        origin_session = await db.get(ChatSession, origin_id)
        if origin_session is None:
            raise RuntimeError("on_message origin session no longer exists")
        lock_key = chat_session_lock_key(origin_session)

    return await run_channel_message(
        lock_key,
        is_command=False,
        reactions=ChannelReactions(),
        work=_work_and_deliver,
        distributed=True,
        workload_kind=WorkloadKind.SCHEDULED,
        tenant_id=tenant_id or cfg.get("_execution_user_id") or agent_id,
    )

__all__ = [name for name in globals() if not name.startswith("__")]
