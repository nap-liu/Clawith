"""Confirmation suspension, resolution, and loop re-entry."""

from app.services.confirmation_shared import *  # noqa: F401,F403



async def suspend_for_confirmation(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    chat_session_id: uuid.UUID | None,
    source_channel: str,
    user_id: uuid.UUID | None,
    intro_text: str | None,
    title: str,
    summary: str,
    action: dict | None,
    risk_level: str,
    buttons: list | None = None,
    force_confirmation: bool = True,
    turn_anchor_id: uuid.UUID | None = None,
    assistant_content: str | None = None,
    recovery_prefix_messages: list[dict[str, str]] | None = None,
    reasoning_content: str | None = None,
    round_id: str | None = None,
) -> uuid.UUID:
    """Suspend the turn on a request_confirmation tool_call and return the row id.

    Persists the agent's intro text (if any) BEFORE the pending tool_call row so a reload
    renders text-before-card, then delivers the card to the originating channel. The CALLER
    ends the turn (returns ""); the loop resumes later in resolve_confirmation.
    """
    from app.models.audit import ChatMessage
    from app.services.chat_history import persist_pending_confirmation_row

    args = _build_card_args(
        title,
        summary,
        action,
        risk_level,
        buttons,
        force_confirmation,
    )
    resolved_channel, ext_conv_id, is_group = await _resolve_session_channel(
        conversation_id, chat_session_id, source_channel
    )
    from app.services.user_output import sanitize_user_visible_text

    intro_text = sanitize_user_visible_text(intro_text or "").strip() or None

    # Intro text first (live web already streamed it; persisted here so it precedes the
    # card on reload). The pending tool_call row is the durable suspended state;
    # startup recovery sees it and leaves the turn waiting for the user's click.
    has_intro = bool(intro_text and intro_text.strip())
    created_at = datetime.now(timezone.utc)
    intro_message_id: uuid.UUID | None = None
    turn_snapshot = None
    async with async_session() as db:
        # The session row is the cross-process serialization boundary shared with
        # inbound message ingestion and confirmation resolution.  Either the user
        # message wins first and belongs to this turn, or the pending card wins and
        # every later message is rejected until the card is clicked.
        from app.models.chat_session import ChatSession

        try:
            sid = uuid.UUID(str(conversation_id))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "confirmation requires a normalized chat session"
            ) from exc
        locked_session_id = (
            await db.execute(
                select(ChatSession.id)
                .where(
                    ChatSession.id == sid,
                    ChatSession.agent_id == agent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_session_id is None:
            raise RuntimeError("confirmation chat session no longer exists")
        existing = await find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(conversation_id),
        )
        if existing is not None:
            logger.warning(
                "Confirmation %s already pending for conversation %s; "
                "reusing the existing suspended tool call",
                existing.row_id,
                conversation_id,
            )
            return existing.row_id
        if has_intro:
            from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

            intro_message_id = uuid.uuid4()
            intro_delivery = (
                IMDeliveryResult.pending("dingtalk")
                if resolved_channel == "dingtalk"
                else IMDeliveryResult.unsupported_delivery("web", "websocket")
            )
            db.add(
                ChatMessage(
                    id=intro_message_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content=intro_text,
                    conversation_id=str(conversation_id),
                    created_at=created_at,
                    message_meta=attach_delivery_to_meta(
                        {"artifact_role": "confirmation_intro"},
                        intro_delivery,
                    ),
                )
            )
        row_id = await persist_pending_confirmation_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(conversation_id),
            name=REQUEST_CONFIRMATION_TOOL_NAME,
            args=args,
            turn_anchor_id=turn_anchor_id,
            assistant_content=assistant_content,
            recovery_prefix_messages=recovery_prefix_messages,
            reasoning_content=reasoning_content,
            round_id=round_id,
            created_at=(
                created_at + timedelta(microseconds=1)
                if has_intro
                else None
            ),
        )
        from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

        confirmation_row = await db.get(ChatMessage, row_id)
        if confirmation_row is None:
            raise RuntimeError("confirmation row disappeared before delivery")
        confirmation_row.message_meta = attach_delivery_to_meta(
            confirmation_row.message_meta,
            IMDeliveryResult.pending("dingtalk" if resolved_channel == "dingtalk" else "web"),
        )
        if turn_anchor_id is not None:
            from app.services.conversation_turn_lifecycle import transition_conversation_turn

            turn_snapshot = await transition_conversation_turn(
                db,
                agent_id=agent_id,
                conversation_id=str(conversation_id),
                turn_anchor_id=turn_anchor_id,
                status="suspended",
            )
        await db.commit()

    # ALWAYS mirror the card to live web viewers — the card IS a tool_call, so broadcast it
    # as one regardless of origin channel (a web viewer watching a DingTalk session sees it
    # live, keyed on the row id so the later resolve flips this same card in place).
    from app.services.im_delivery import IMDeliveryResult, register_delivery

    try:
        payload = {
            "type": "tool_call",
            "name": REQUEST_CONFIRMATION_TOOL_NAME,
            "call_id": str(row_id),
            "args": args,
            "status": "running",
        }
        if turn_snapshot is not None:
            from app.services.conversation_turn_lifecycle import with_turn_envelope

            payload = with_turn_envelope(
                payload,
                turn_snapshot,
                event_kind="turn_suspended",
            )
        await _broadcast(
            agent_id,
            conversation_id,
            payload,
        )
        if resolved_channel != "dingtalk":
            await register_delivery(
                row_id,
                IMDeliveryResult.unsupported_delivery("web", "websocket"),
            )
    except Exception as exc:
        if resolved_channel != "dingtalk":
            await register_delivery(
                row_id,
                IMDeliveryResult.from_exception("web", exc),
            )
        raise
    # Additionally deliver to the originating IM channel. DingTalk doesn't stream, so send
    # the intro text as a message first, then the interactive card.
    if resolved_channel == "dingtalk":
        runtime = await load_turn_runtime(
            agent_id=agent_id,
            conversation_id=str(conversation_id),
        )

        async def _send_intro_then_card() -> bool:
            if has_intro and intro_message_id is not None:
                from app.services.im_delivery import deliver_persisted_message

                intro_result = await deliver_persisted_message(
                    message_id=intro_message_id,
                    agent_id=agent_id,
                    runtime=runtime,
                    message=intro_text or "",
                    dingtalk_lock_held=True,
                )
                if not intro_result.ok:
                    logger.warning(
                        "Confirmation %s: intro delivery failed; card suppressed",
                        row_id,
                    )
                    return False
            from app.services.im_delivery import (
                IMDeliveryPart,
                IMDeliveryResult,
                append_delivery_part,
                register_delivery,
            )

            pending_card_part = IMDeliveryPart(
                transport="dingtalk_interactive_card",
                provider_message_id=str(row_id),
                conversation_ref=ext_conv_id,
                artifact_role="confirmation_card",
                recallable=False,
                send_status="pending",
            )
            if not await append_delivery_part(row_id, pending_card_part):
                return False
            card_delivered = await _deliver_channel_card_unlocked(
                agent_id=agent_id,
                out_track_id=str(row_id),
                args=args,
                external_conv_id=ext_conv_id,
                is_group=is_group,
            )
            if card_delivered:
                await register_delivery(
                    row_id,
                    IMDeliveryResult.sent(
                        "dingtalk",
                        IMDeliveryPart(
                            transport="dingtalk_interactive_card",
                            provider_message_id=str(row_id),
                            conversation_ref=ext_conv_id,
                            artifact_role="confirmation_card",
                            recallable=False,
                        ),
                    ),
                )
            else:
                await register_delivery(
                    row_id,
                    IMDeliveryResult.failed("dingtalk", "confirmation_card_send_failed"),
                )
            return card_delivered

        await run_channel_send(
            _im_send_lock_key(str(conversation_id)),
            _send_intro_then_card,
        )
    logger.info("Confirmation suspended: row %s on channel %s (conv %s)", row_id, resolved_channel, conversation_id)
    return row_id


async def resolve_confirmation(
    *,
    agent_id: uuid.UUID,
    call_id: uuid.UUID,
    button_value: str,
    button_label: str | None,
    resolving_user_id: uuid.UUID,
    clicked_card_instance_id: str | None = None,
) -> str | None:
    """Record the clicked button as the tool_call's result, then resume the loop.

    ``call_id`` IS the pending request_confirmation tool_call row id. GENERIC: the platform
    executes nothing — it fills the tool result with a faithful relay of which button was
    pressed (+ validity), flips the row to ``done``, and re-enters the LLM loop so the agent
    reads the result and decides the next step itself. Idempotent; serialized per-session so
    a double-click resolves exactly once. Returns the result text (or None if already
    resolved / unknown), so the REST caller can surface the committed outcome.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    now = datetime.now(timezone.utc)
    result_text: str | None = None
    turn_snapshot = None

    async with async_session() as db:
        # The row's conversation_id is immutable — read it first (unlocked) so we know which
        # session to lock; the authoritative pending-check happens under the lock below.
        row = await db.get(ChatMessage, call_id)
        if row is None or row.role != "tool_call" or row.agent_id != agent_id:
            logger.warning("resolve_confirmation: no matching tool_call row %s", call_id)
            return None
        conversation_id = row.conversation_id

        # Lock the session row FOR UPDATE so concurrent resolves of the same card serialize:
        # the second blocks here, then (after refresh) re-reads a 'done' row and returns early.
        try:
            sid = uuid.UUID(str(conversation_id))
            await db.execute(select(ChatSession.id).where(ChatSession.id == sid).with_for_update())
            await db.refresh(row)
        except (ValueError, TypeError):
            pass  # non-session conversation — best-effort, no lock

        try:
            payload = json.loads(row.content or "{}")
        except Exception:
            return None
        if payload.get("name") != REQUEST_CONFIRMATION_TOOL_NAME or payload.get("status") != "pending":
            return None  # idempotent — already resolved (or not a confirmation)

        pending_meta = row.message_meta if isinstance(row.message_meta, dict) else {}
        latest_delivery_id = pending_meta.get("confirmation_delivery_id")
        try:
            _assert_confirmation_actor(row, pending_meta, resolving_user_id)
        except ConfirmationActorMismatch:
            logger.warning(
                "resolve_confirmation: actor %s rejected for row %s intended for %s",
                resolving_user_id,
                call_id,
                pending_meta.get("intended_user_id") or row.user_id,
            )
            raise
        try:
            turn_anchor_id = uuid.UUID(str(pending_meta.get("turn_anchor_id")))
        except (TypeError, ValueError):
            turn_anchor_id = None

        label = button_label or button_value or "按钮"
        ago = _ago(row.created_at, now)
        expired = bool(row.created_at and (now - row.created_at) > timedelta(hours=CONFIRMATION_EXPIRY_HOURS))
        if expired:
            result_text = (
                f"用户点了「{label}」(value={button_value}){ago},但确认卡已超过 {CONFIRMATION_EXPIRY_HOURS} "
                f"小时有效期,此响应不可靠——请勿据此执行不可逆/危险操作;如仍需要请重新发卡确认。"
            )
        else:
            result_text = f"用户点了「{label}」(value={button_value}){ago}、在有效期内。"

        payload["status"] = "done"
        payload["result"] = result_text
        row.content = json.dumps(payload, ensure_ascii=False, default=str)
        if turn_anchor_id is not None:
            from app.services.conversation_turn_lifecycle import (
                transition_conversation_turn,
            )

            turn_snapshot = await transition_conversation_turn(
                db,
                agent_id=agent_id,
                conversation_id=str(conversation_id),
                turn_anchor_id=turn_anchor_id,
                status="running",
            )
        await db.commit()
        if turn_anchor_id is not None:
            from app.services.conversation_turn_lifecycle import get_conversation_turn_snapshot

            turn_snapshot = await get_conversation_turn_snapshot(
                db,
                agent_id=agent_id,
                conversation_id=str(conversation_id),
                turn_anchor_id=turn_anchor_id,
            )

    # Flip the card for any live web viewer (the click optimistically updates the clicker's
    # own card; this updates other watchers + is the authoritative live signal).
    resolved_tool_payload = {
        "type": "tool_call",
        "name": REQUEST_CONFIRMATION_TOOL_NAME,
        "call_id": str(call_id),
        # Carry args so the live merge keeps the card's content (the web merge spreads
        # the event and would otherwise overwrite toolArgs with undefined).
        "args": payload.get("args"),
        "status": "done",
        "result": result_text,
    }
    resolved_payload = resolved_tool_payload
    if turn_snapshot is not None:
        from app.services.conversation_turn_lifecycle import with_turn_envelope

        resolved_payload = with_turn_envelope(
            resolved_payload,
            turn_snapshot,
            event_kind="turn_tool",
        )
    await _broadcast(
        agent_id,
        conversation_id,
        resolved_payload,
    )

    # Keep the origin IM card in sync: a card delivered to DingTalk — resolved here on web OR
    # via its own button — flips to a disabled, resolved state so it can't be clicked again
    # (no stale clicks). Best-effort; web-origin cards are a no-op.
    card_instance_ids = [str(call_id)]
    if latest_delivery_id:
        card_instance_ids.append(str(latest_delivery_id))
    if clicked_card_instance_id:
        card_instance_ids.append(clicked_card_instance_id)
    for card_instance_id in dict.fromkeys(card_instance_ids):
        await _update_origin_card(
            agent_id,
            conversation_id,
            conversation_id,
            card_instance_id,
            payload.get("args") or {},
            label,
            button_value,
        )

    # Resume the agent's loop from the now-complete tool result. Best-effort — its failure
    # must not mask the resolution (the REST caller still gets the committed outcome).
    try:
        await _reenter_loop(
            agent_id,
            str(conversation_id),
            resolving_user_id,
            turn_anchor_id=turn_anchor_id,
            resolved_tool_payload=resolved_tool_payload,
        )
    except Exception:
        logger.exception("Reenter after resolving confirmation %s failed; resolution stands.", call_id)
        if turn_anchor_id is not None:
            from app.services.conversation_turn_lifecycle import (
                publish_conversation_turn_event,
                transition_conversation_turn,
            )

            try:
                async with async_session() as lifecycle_db:
                    failed_snapshot = await transition_conversation_turn(
                        lifecycle_db,
                        agent_id=agent_id,
                        conversation_id=str(conversation_id),
                        turn_anchor_id=turn_anchor_id,
                        status="failed",
                    )
                    await lifecycle_db.commit()
                await publish_conversation_turn_event(
                    agent_id=agent_id,
                    conversation_id=str(conversation_id),
                    payload={"type": "error", "content": "确认后的处理未能继续。"},
                    snapshot=failed_snapshot,
                    event_kind="turn_terminal",
                )
            except Exception:
                logger.exception("Failed to close confirmation continuation %s", call_id)

    return result_text


async def _reenter_loop(
    agent_id: uuid.UUID,
    conversation_id: str,
    resolving_user_id: uuid.UUID,
    *,
    turn_anchor_id: uuid.UUID | None = None,
    resolved_tool_payload: dict | None = None,
) -> None:
    """Resume the agent's LLM loop from existing history (which now ends with the filled
    request_confirmation tool result) WITHOUT injecting a user message. Per-session lock via
    run_channel_message; persists + delivers the follow-up reply to the originating channel."""
    from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
    from app.models.agent import Agent as AgentModel
    from app.models.chat_session import ChatSession
    from app.services.channel_dispatch import (
        ChannelReactions,
        chat_session_lock_key,
        run_channel_message,
    )
    from app.services.channel_llm import _call_agent_llm
    from app.services.chat_history import (
        load_history_for_llm,
        persist_assistant_reply,
        persist_assistant_reply_and_complete_turn,
    )

    async def _work() -> str:
        if turn_anchor_id is not None:
            async with async_session() as lifecycle_check_db:
                from app.models.audit import ChatMessage

                lifecycle_anchor = await lifecycle_check_db.get(ChatMessage, turn_anchor_id)
                lifecycle_meta = (
                    lifecycle_anchor.message_meta
                    if lifecycle_anchor is not None
                    and isinstance(lifecycle_anchor.message_meta, dict)
                    else {}
                )
                if lifecycle_meta.get("turn_status") == "cancelled":
                    logger.info(
                        "Confirmation continuation skipped for cancelled turn %s",
                        turn_anchor_id,
                    )
                    return ""
            from app.services.conversation_turn_lifecycle import (
                publish_conversation_turn_event,
                transition_conversation_turn,
            )

            try:
                async with async_session() as lifecycle_db:
                    resumed_snapshot = await transition_conversation_turn(
                        lifecycle_db,
                        agent_id=agent_id,
                        conversation_id=conversation_id,
                        turn_anchor_id=turn_anchor_id,
                        status="running",
                    )
                    await lifecycle_db.commit()
                await publish_conversation_turn_event(
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    payload={"type": "turn_state"},
                    snapshot=resumed_snapshot,
                    event_kind="turn_lifecycle",
                )
            except LookupError:
                # Legacy confirmation rows may predate durable ChatSession rows.
                pass
        async with async_session() as db:
            if turn_anchor_id is not None:
                from app.models.audit import ChatMessage

                anchor = await db.get(ChatMessage, turn_anchor_id)
                anchor_meta = (
                    anchor.message_meta
                    if anchor is not None and isinstance(anchor.message_meta, dict)
                    else {}
                )
                if anchor_meta.get("turn_status") == "cancelled":
                    logger.info(
                        "Confirmation continuation skipped for cancelled turn %s",
                        turn_anchor_id,
                    )
                    return ""
            agent_obj = (
                await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            ).scalar_one_or_none()
            ctx_size = (agent_obj.context_window_size if agent_obj else None) or DEFAULT_CONTEXT_WINDOW_SIZE
            history = await load_history_for_llm(
                db, agent_id=agent_id, conversation_id=conversation_id, ctx_size=ctx_size
            )
            reply = await _call_agent_llm(
                db,
                agent_id,
                "",
                session_id=conversation_id,
                user_id=resolving_user_id,
                history=history,
                recovery_hint=None,
                continue_turn=True,
                turn_anchor_id=turn_anchor_id,
            )
        # An empty reply means the agent suspended AGAIN (chained confirmation) and the new
        # suspend already persisted/delivered everything — nothing to add here.
        if reply and reply.strip():
            from app.services.im_delivery import (
                IMDeliveryResult,
                attach_delivery_to_meta,
            )

            runtime = await load_turn_runtime(
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            pending_meta = attach_delivery_to_meta(
                {},
                IMDeliveryResult.pending(runtime.source_channel),
            )
            if turn_anchor_id is not None:
                assistant_message_id = await persist_assistant_reply_and_complete_turn(
                    async_session,
                    agent_id=agent_id,
                    user_id=resolving_user_id,
                    conversation_id=conversation_id,
                    content=reply,
                    turn_anchor_id=turn_anchor_id,
                    message_meta=pending_meta,
                )
            else:
                assistant_message_id = await persist_assistant_reply(
                    async_session,
                    agent_id=agent_id,
                    user_id=resolving_user_id,
                    conversation_id=conversation_id,
                    content=reply,
                    message_meta=pending_meta,
                    required=True,
                )
            delivered = await deliver_reply_to_origin(
                agent_id=agent_id,
                conversation_id=conversation_id,
                reply=reply,
                require_transport=True,
                message_id=assistant_message_id,
            )
            if not delivered:
                logger.warning(
                    "Confirmation reply persisted but origin delivery failed: "
                    "agent=%s conversation=%s",
                    agent_id,
                    conversation_id,
                )
        return reply

    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(str(conversation_id)))
        if session is not None and session.agent_id != agent_id:
            raise RuntimeError("Confirmation session changed owner")
        if session is not None and session.source_channel == "subagent":
            # Project group members, direct A2A targets, work-item Runs and
            # restored member generations all execute as durable SubagentRuns.
            # Resume through that worker so restart recovery, project lifecycle
            # and parent delivery remain on the canonical path.
            from app.services.subagent_runtime import (
                resume_subagent_after_confirmation,
            )

            await resume_subagent_after_confirmation(
                session.id,
                resolved_tool_payload=resolved_tool_payload,
                child_turn_anchor_id=turn_anchor_id,
            )
            return
        # Legacy confirmation rows can outlive a deleted/missing ChatSession.
        # Preserve their UUID lock identity; durable sessions use the same
        # canonical external-session key as ordinary channel messages.
        lock_key = chat_session_lock_key(session) if session is not None else str(conversation_id)

    await run_channel_message(
        lock_key,
        is_command=False,
        reactions=ChannelReactions(),
        work=_work,
    )


__all__ = [name for name in globals() if not name.startswith("__")]
