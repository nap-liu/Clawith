"""Incoming chat message persistence and ingest orchestration."""

from __future__ import annotations

from app.services.chat_history_loading import *  # noqa: F401,F403

async def persist_incoming_user_message(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    participant_id: uuid.UUID | None = None,
    external_event_key: str | None = None,
    message_meta: dict[str, Any] | None = None,
    message_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> ChatMessage:
    """Persist an incoming user message.

    Restart recovery treats this ordinary append-only row as the natural turn
    anchor when it belongs to a recent incomplete message tail.
    """
    row = ChatMessage(
        id=message_id or uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        sender_user_id=user_id,
        role="user",
        content=content,
        conversation_id=conversation_id,
        participant_id=participant_id,
        external_event_key=external_event_key,
        message_meta=message_meta or {},
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    await db.flush()
    return row


async def mark_latest_incomplete_turn_cancelled(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    reason: str,
) -> uuid.UUID | None:
    """Persist the control-plane cancellation of the current conversation turn.

    ``/stop`` and ``/new`` cancel the in-process coroutine immediately, while
    startup recovery infers interrupted turns from durable chat rows. Marking
    the latest unanswered user anchor keeps those two views consistent.
    """
    from app.services.conversation_turn_lifecycle import TURN_LIFECYCLE_KEY

    try:
        session_id = uuid.UUID(conversation_id)
    except (TypeError, ValueError):
        session_id = None
    if session_id is not None:
        session = (
            await db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            return None
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        # This helper exists only for pre-lifecycle recovery rows. Once a
        # canonical pointer exists, active and terminal generations are owned
        # exclusively by the normalized STOP transaction.
        if conversation_turn_snapshot_for_session(session).status != "idle":
            return None
    latest = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.compacted_into.is_(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None or (
        latest.role == "assistant" and not is_incomplete_delivery_progress(latest)
    ):
        return None
    latest_meta = latest.message_meta if isinstance(latest.message_meta, dict) else {}
    if (
        latest_meta.get("consumed_by_onmessage")
        or latest_meta.get("kind") == "on_message_event"
    ):
        # TriggerExecution owns these event turns and its lease/reclaim path;
        # channel startup recovery and /stop must not claim their anchors.
        return None
    anchor = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == "user",
                ChatMessage.compacted_into.is_(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if anchor is None:
        return None
    anchor_meta = dict(anchor.message_meta or {})
    if anchor_meta.get(TURN_LIFECYCLE_KEY) is True:
        return None

    latest_anchor_tool = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == "tool_call",
                ChatMessage.compacted_into.is_(None),
                ChatMessage.message_meta["turn_anchor_id"].as_string()
                == str(anchor.id),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_tool_payload = (
        _parse_tool_call_payload(latest_anchor_tool.content)
        if latest_anchor_tool is not None
        else None
    )
    if (
        latest_tool_payload
        and latest_tool_payload.get("name") == "request_confirmation"
        and latest_tool_payload.get("status") == "pending"
    ):
        # A confirmation card is deliberately suspended, not running. /stop
        # must leave it resolvable instead of creating a cancelled anchor that
        # the confirmation lifecycle does not consume.
        return None

    completed = (
        await db.execute(
            select(ChatMessage.id)
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string()
                == str(anchor.id),
                ChatMessage.message_meta["turn_status"].as_string()
                == "completed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if completed is not None:
        return None

    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    try:
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_anchor_id=anchor.id,
            status="cancelled",
        )
    except LookupError:
        # Pre-ChatSession channel histories retain their legacy cancellation
        # marker but cannot participate in the normalized generation contract.
        pass
    meta = dict(anchor.message_meta or {})
    meta.update(
        {
            "turn_status": "cancelled",
            "cancel_reason": reason,
            "cancelled_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    anchor.message_meta = meta
    await db.flush()
    return anchor.id


async def mark_turn_cancelled(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    reason: str,
) -> uuid.UUID | None:
    """Mark one exact active user anchor cancelled without guessing latest state."""

    try:
        session_id = uuid.UUID(conversation_id)
    except (TypeError, ValueError):
        session_id = None
    if session_id is not None:
        await db.execute(
            select(ChatSession.id)
            .where(ChatSession.id == session_id, ChatSession.agent_id == agent_id)
            .with_for_update()
        )
    anchor = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.id == turn_anchor_id,
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == "user",
                ChatMessage.compacted_into.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if anchor is None:
        return None

    if await turn_has_completed_reply(
        db,
        agent_id=agent_id,
        conversation_id=conversation_id,
        turn_anchor_id=turn_anchor_id,
    ):
        return None

    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    try:
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_anchor_id=anchor.id,
            status="cancelled",
        )
    except LookupError:
        pass
    meta = dict(anchor.message_meta or {})
    meta.update(
        {
            "turn_status": "cancelled",
            "cancel_reason": reason,
            "cancelled_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    anchor.message_meta = meta
    await db.flush()
    return anchor.id


async def turn_has_completed_reply(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
) -> bool:
    """Return whether an exact turn anchor already has its terminal reply."""

    completed = await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_anchor_id"].as_string()
            == str(turn_anchor_id),
            ChatMessage.message_meta["turn_status"].as_string() == "completed",
        )
        .limit(1)
    )
    return completed is not None


def build_external_event_key(
    *,
    agent_id: uuid.UUID,
    source_channel: str,
    provider_event_id: str | None,
    channel_config_id: uuid.UUID | str | None = None,
) -> str | None:
    """Build the globally namespaced key used for durable inbound deduplication.

    Provider message ids are only unique inside a bot/application account.  The
    agent and channel-config namespace therefore form part of the key.  Callers
    without a stable provider/client event id intentionally receive ``None`` and
    keep the historical append-only behaviour rather than deduplicating by text.
    """
    event_id = str(provider_event_id or "").strip()
    if not event_id:
        return None
    account = str(channel_config_id or "platform")
    raw_key = f"{agent_id}\x1f{source_channel}\x1f{account}\x1f{event_id}"
    # Hash the complete provider id instead of truncating it. Two unusually
    # long ids with the same prefix must never collapse into one inbound event.
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"inbound:{source_channel}:{digest}"


async def persist_incoming_user_message_once(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    participant_id: uuid.UUID | None = None,
    external_event_key: str | None = None,
    message_meta: dict[str, Any] | None = None,
    message_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> tuple[ChatMessage, bool]:
    """Persist one inbound event and report whether this caller created it.

    A nested transaction contains the unique-key race so a duplicate delivery
    does not roll back the caller's surrounding session/identity work.  The
    database unique index remains the authority across backend replicas.
    """
    if external_event_key:
        existing = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == external_event_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    try:
        async with db.begin_nested():
            row = await persist_incoming_user_message(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                content=content,
                participant_id=participant_id,
                external_event_key=external_event_key,
                message_meta=message_meta,
                message_id=message_id,
                created_at=created_at,
            )
        return row, True
    except IntegrityError:
        if not external_event_key:
            raise
        existing = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == external_event_key))
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False


@dataclass(frozen=True)
class IncomingMessageIngestResult:
    message: ChatMessage
    created: bool
    consumed_by_onmessage: bool
    execution_ids: tuple[uuid.UUID, ...] = ()
    blocked_by_confirmation: bool = False
    pending_confirmation: PendingConfirmation | None = None
    ignored_confirmation: PendingConfirmation | None = None
    ignored_confirmation_result: str | None = None
    queued_to_running_turn: bool = False


def _turn_inbox_state(meta: dict[str, Any]) -> bool:
    return str(meta.get("turn_inbox_state") or "") in {
        "pending",
        "processing",
        "delivered",
        "cancelled",
    }


async def finish_blocked_confirmation_ingest(
    db: AsyncSession,
    result: IncomingMessageIngestResult,
) -> bool:
    """Finish an IM ingress rejected by the pending-confirmation hard gate.

    The caller's legitimate setup work (identity/session creation) is committed,
    while no inbound message exists to broadcast or execute.  After releasing the
    session lock, create a fresh transport instance for the same durable card on
    supported IM channels.
    """
    if not result.blocked_by_confirmation or result.pending_confirmation is None:
        return False
    await db.commit()
    from app.services.confirmation_service import redeliver_pending_confirmation

    await redeliver_pending_confirmation(result.pending_confirmation)
    return True


async def finish_ignored_confirmation_ingest(
    result: IncomingMessageIngestResult,
) -> bool:
    """Publish a committed non-blocking confirmation's negative tool result."""
    if (
        result.ignored_confirmation is None
        or not result.ignored_confirmation_result
    ):
        return False
    from app.services.confirmation_service import publish_ignored_confirmation

    await publish_ignored_confirmation(
        result.ignored_confirmation,
        result.ignored_confirmation_result,
    )
    return True


async def ingest_incoming_chat_message(
    db: AsyncSession,
    *,
    session,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    source_channel: str,
    allow_turn_inbox: bool = True,
    provider_event_id: str | None = None,
    channel_config_id: uuid.UUID | str | None = None,
    actor_ref: str | None = None,
    reply_to_external_message_id: str | None = None,
    participant_id: uuid.UUID | None = None,
    message_meta: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> IncomingMessageIngestResult:
    """Durably ingest, deduplicate and route one channel/web inbound event.

    The caller keeps ownership of the outer transaction.  Exact on_message
    matching therefore commits atomically with the durable inbound row and its
    TriggerExecution records; adapters can skip their ordinary remote-session
    LLM turn when the subscription consumes the event.
    """
    event_key = build_external_event_key(
        agent_id=agent_id,
        source_channel=source_channel,
        provider_event_id=provider_event_id,
        channel_config_id=channel_config_id,
    )

    # Provider retries are deduplicated before the pending-confirmation gate.  If
    # the original event was accepted before the card appeared, retrying that same
    # event must not be mistaken for a new attempt and must never re-run its turn.
    if event_key:
        existing = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == event_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing_meta = (
                existing.message_meta if isinstance(existing.message_meta, dict) else {}
            )
            execution_ids: list[uuid.UUID] = []
            for raw_id in existing_meta.get("onmessage_execution_ids") or []:
                try:
                    execution_ids.append(uuid.UUID(str(raw_id)))
                except (TypeError, ValueError):
                    continue
            return IncomingMessageIngestResult(
                message=existing,
                created=False,
                consumed_by_onmessage=True,
                execution_ids=tuple(execution_ids),
                queued_to_running_turn=_turn_inbox_state(existing_meta),
            )

    # Use the normal ChatSession row as the cross-process ordering boundary.  The
    # confirmation writer takes the same lock, so a message can never slip between
    # "agent requested confirmation" and "pending row became visible".
    from app.models.chat_session import ChatSession

    locked_session = (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.id == session.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_session is None:
        raise RuntimeError("chat session no longer exists")

    from app.services.confirmation_service import find_pending_confirmation

    # Confirmation cards are currently a product contract for Web/H5 and
    # DingTalk only. Do not silently impose an unclickable hard gate on other IM
    # transports until they implement the same card lifecycle.
    confirmation_gated = source_channel in {
        "web",
        "miniprogram",
        "wechat_miniprogram",
        "dingtalk",
    }
    pending = (
        await find_pending_confirmation(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )
        if confirmation_gated
        else None
    )
    ignored_confirmation = None
    ignored_confirmation_result = None
    if pending is not None and pending.force_confirmation:
        pending_row = await db.get(ChatMessage, pending.row_id)
        if pending_row is None:
            raise RuntimeError("pending confirmation message no longer exists")
        return IncomingMessageIngestResult(
            message=pending_row,
            created=False,
            consumed_by_onmessage=True,
            blocked_by_confirmation=True,
            pending_confirmation=pending,
        )
    if pending is not None:
        from app.services.confirmation_service import (
            ignore_pending_confirmation_for_new_input,
        )

        ignored_confirmation_result = await ignore_pending_confirmation_for_new_input(
            db,
            pending,
        )
        if ignored_confirmation_result is None:
            raise RuntimeError("non-blocking pending confirmation could not be closed")
        ignored_confirmation = pending

    meta = {
        **(message_meta or {}),
        "direction": "inbound",
        "source_channel": source_channel,
        "actor_ref": str(actor_ref or user_id),
    }
    # Protocol marker: every newly ingested message has authoritative attachment
    # metadata, including an empty list. Rows without this key are therefore
    # unambiguously legacy and may use the historical [file:...] parser.
    meta.setdefault("attachments", [])
    # Snapshot the published scene while holding the same session-row lock used
    # to order inbound events.  A /scene command racing with an older in-flight
    # turn can therefore affect only messages ingested after the command wins
    # this lock.  Explicit Web/H5 scene metadata remains authoritative.
    if not meta.get("scene_resolved") and not meta.get("scene_key"):
        from app.services.scene_activation import resolve_session_scene
        from app.services.scene_service import scene_message_meta

        manifest = await resolve_session_scene(db, agent_id, locked_session)
        meta.update(scene_message_meta(manifest))
        meta["scene_resolved"] = True
    if not meta.get("model_id"):
        from app.services.chat_model_selection import MODEL_SESSION_CONFIG_KEY

        active_model_id = str(
            (locked_session.im_config or {}).get(MODEL_SESSION_CONFIG_KEY) or ""
        )
        try:
            if active_model_id:
                meta["model_id"] = str(uuid.UUID(active_model_id))
        except ValueError:
            logger.warning(
                "Ignoring invalid session model override session={}",
                locked_session.id,
            )
    if "reasoning_effort" not in meta:
        from app.services.chat_model_selection import REASONING_SESSION_CONFIG_KEY
        from app.services.llm.reasoning import validate_reasoning_effort

        active_reasoning_effort = (locked_session.im_config or {}).get(
            REASONING_SESSION_CONFIG_KEY
        )
        if active_reasoning_effort is not None:
            try:
                meta["reasoning_effort"] = validate_reasoning_effort(active_reasoning_effort)
            except ValueError:
                logger.warning(
                    "Ignoring invalid session reasoning override session={}",
                    locked_session.id,
                )
    if reply_to_external_message_id:
        meta["reply_to_external_message_id"] = str(reply_to_external_message_id)

    row, created = await persist_incoming_user_message_once(
        db,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=str(session.id),
        content=content,
        participant_id=participant_id,
        external_event_key=event_key,
        message_meta=meta,
        created_at=created_at,
    )
    if not created:
        existing_meta = row.message_meta if isinstance(row.message_meta, dict) else {}
        execution_ids: list[uuid.UUID] = []
        for raw_id in existing_meta.get("onmessage_execution_ids") or []:
            try:
                execution_ids.append(uuid.UUID(str(raw_id)))
            except (TypeError, ValueError):
                continue
        return IncomingMessageIngestResult(
            message=row,
            created=False,
            # A provider retry must never start a second ordinary LLM turn,
            # whether or not the first delivery matched a subscription.
            consumed_by_onmessage=True,
            execution_ids=tuple(execution_ids),
            queued_to_running_turn=_turn_inbox_state(existing_meta),
        )

    from app.services.trigger_runtime.evaluator import match_incoming_chat_message

    matched = await match_incoming_chat_message(db, row, session)
    queued_to_running_turn = False
    from app.services.turn_inbox import is_turn_inbox_channel

    if allow_turn_inbox and not matched.consumed and is_turn_inbox_channel(source_channel):
        from app.services.conversation_turn_lifecycle import (
            ACTIVE_TURN_STATUS,
            conversation_turn_snapshot_for_session,
            transition_conversation_turn,
        )

        snapshot = conversation_turn_snapshot_for_session(locked_session)
        if snapshot.status == ACTIVE_TURN_STATUS and snapshot.anchor_id is not None:
            active_anchor = await db.get(ChatMessage, snapshot.anchor_id)
            # A2A stores both employees' directions in one conversation. Route
            # by the executing employee, never by the human sender.
            same_employee = active_anchor is not None and str(
                (active_anchor.message_meta or {}).get("execution_agent_id")
                or active_anchor.agent_id
            ) == str((row.message_meta or {}).get("execution_agent_id") or row.agent_id)
            active_meta = (
                dict(active_anchor.message_meta or {})
                if active_anchor is not None
                else {}
            )
            if same_employee:
                incoming_meta = dict(row.message_meta or {})
                for key in ("scene_key", "scene_revision", "activation_source", "model_id", "reasoning_effort"):
                    incoming_meta.pop(key, None)
                    if key in active_meta:
                        incoming_meta[key] = active_meta[key]
                row.message_meta = incoming_meta
            row.message_meta = {
                **dict(row.message_meta or {}),
                "turn_inbox_state": "pending",
                "turn_inbox_anchor_id": str(snapshot.anchor_id),
                "turn_inbox_generation": snapshot.generation,
                # One conversation has one inbox, regardless of its sender.
                "turn_inbox_mode": "current_turn" if same_employee else "next_turn",
            }
            await db.flush()
            queued_to_running_turn = True
            from app.services.channel_dispatch import (
                mark_channel_turn_admitted,
                register_channel_receipt_anchor,
            )

            await mark_channel_turn_admitted()
            if same_employee:
                await register_channel_receipt_anchor(row.id)
        else:
            await transition_conversation_turn(
                db,
                agent_id=agent_id,
                conversation_id=str(locked_session.id),
                turn_anchor_id=row.id,
                status="running",
            )
            from app.services.channel_dispatch import (
                is_channel_turn_interjection,
                mark_channel_promoted_turn,
                mark_channel_turn_admitted,
            )

            await mark_channel_turn_admitted()
            if is_channel_turn_interjection():
                row.message_meta = {
                    **dict(row.message_meta or {}),
                    "turn_inbox_state": "promoted",
                    "turn_inbox_anchor_id": str(row.id),
                    "turn_inbox_generation": conversation_turn_snapshot_for_session(
                        locked_session
                    ).generation,
                    "turn_inbox_mode": "current_turn",
                }
                await db.flush()
                queued_to_running_turn = True
                mark_channel_promoted_turn(agent_id, str(locked_session.id))
    return IncomingMessageIngestResult(
        message=row,
        created=True,
        consumed_by_onmessage=matched.consumed or queued_to_running_turn,
        execution_ids=matched.execution_ids,
        ignored_confirmation=ignored_confirmation,
        ignored_confirmation_result=ignored_confirmation_result,
        queued_to_running_turn=queued_to_running_turn,
    )




__all__ = [name for name in globals() if not name.startswith("__")]
