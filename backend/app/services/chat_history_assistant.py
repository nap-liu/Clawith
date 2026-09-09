"""Assistant reply persistence and turn finalization."""

from __future__ import annotations

from app.services.chat_history_tools import *  # noqa: F401,F403

THINKING_MAX_CHARS = 64_000


def cap_thinking(thinking: str | None) -> str | None:
    """Trim oversized thinking/reasoning before persistence.

    ``thinking`` is a UI-only field (shown when viewing a session, never fed
    back into ``call_llm``); on long tool loops it can grow to many tens of KB.
    Cap it so a single row can't store a pathological multi-100KB blob.
    Returns ``None`` for blank input so callers can pass it straight through.
    """
    if not thinking:
        return None
    return thinking[:THINKING_MAX_CHARS]


async def lock_turn_anchor_for_finalization(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    allow_cancelled: bool = False,
) -> ChatMessage | None:
    """Serialize a terminal write with MCP stop and reject a won cancellation."""

    from app.services.active_turns import wait_for_current_turn_stop_resolution

    await wait_for_current_turn_stop_resolution(allow_cancelled=allow_cancelled)
    try:
        session_id = uuid.UUID(conversation_id)
    except (TypeError, ValueError):
        session_id = None
    if session_id is not None:
        # Every lifecycle writer uses the same session -> anchor lock order.
        # The session row is also the admission mutex for the conversation.
        await db.execute(
            select(ChatSession.id)
            .where(
                ChatSession.id == session_id,
                ChatSession.agent_id == agent_id,
            )
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
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        not allow_cancelled
        and anchor is not None
        and (anchor.message_meta or {}).get("turn_status") == "cancelled"
    ):
        raise asyncio.CancelledError
    return anchor


async def persist_assistant_reply(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    thinking: str | None = None,
    message_meta: dict[str, Any] | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    required: bool = False,
) -> uuid.UUID | None:
    """Persist a channel agent's final assistant reply.

    Uses its OWN session (mirroring ``persist_tool_call``) so ``created_at`` is
    stamped at save time — AFTER the tool loop — keeping the reply ordered
    after the turn's tool_call rows. Saving through the channel's long-lived
    request transaction would instead stamp it with the transaction-start time
    (PostgreSQL ``now()``), placing the reply BEFORE the tool calls; the web UI
    then folds it into the "ran N tools" analysis card and the reply bubble
    disappears. No-op for blank content. Failures are swallowed unless
    ``required`` is set for a delivery lifecycle that must persist its anchor
    before creating a provider-visible side effect.
    """
    if not (content or "").strip():
        if required:
            raise ValueError("required assistant reply content must be non-empty")
        return None
    try:
        async with db_session_factory() as db:
            message_id = await persist_assistant_reply_row(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                content=content,
                thinking=thinking,
                message_meta=message_meta,
                turn_anchor_id=turn_anchor_id,
            )
            await db.commit()
    except Exception as e:
        if required:
            raise
        logger.warning(f"[chat_history] persist_assistant_reply failed (non-fatal): {e}")
        return None

    # Persistence succeeded. Observer lookup/publication is deliberately
    # isolated so a disconnected viewer can never make a caller retry the
    # already committed assistant row.
    if turn_anchor_id is not None:
        from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

        await publish_committed_turn_terminal(
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_anchor_id=turn_anchor_id,
            message_id=message_id,
            content=content,
        )
    return message_id


async def persist_intermediate_assistant_reply(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    turn_anchor_id: uuid.UUID,
    thinking: str | None = None,
    visible_joiner_before: str = "",
    max_output_resume_prompt: str | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Commit one provider-emitted assistant segment without ending the turn.

    The row is written in an independent short transaction so an inbox drain
    can commit it before claiming a later IM/Subagent injection.  It carries the
    root anchor for recovery, but deliberately does not transition the durable
    turn lifecycle.
    """

    content = sanitize_user_visible_text(content or "")
    if not content.strip():
        raise ValueError("intermediate assistant reply content must be non-empty")
    async with db_session_factory() as db:
        anchor = await db.get(ChatMessage, turn_anchor_id)
        if (
            anchor is None
            or anchor.agent_id != agent_id
            or anchor.conversation_id != conversation_id
            or anchor.role not in {"user", "system"}
        ):
            raise LookupError("intermediate assistant turn anchor not found")
        anchor_meta = dict(anchor.message_meta or {})
        if anchor_meta.get("turn_status") != "running":
            raise asyncio.CancelledError
        row = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="assistant",
            content=content,
            conversation_id=conversation_id,
            thinking=(
                cap_thinking(sanitize_user_visible_text(thinking))
                if thinking
                else None
            ),
            message_meta={
                "artifact_role": INTERMEDIATE_ASSISTANT_ARTIFACT_ROLE,
                "turn_anchor_id": str(turn_anchor_id),
                "turn_status": "running",
                "visible_joiner_before": visible_joiner_before,
                **(
                    {"max_output_resume_prompt": max_output_resume_prompt}
                    if max_output_resume_prompt
                    else {}
                ),
            },
            **({"created_at": created_at} if created_at is not None else {}),
        )
        db.add(row)
        await db.commit()
        return row.id


async def terminal_assistant_tail(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    content: str,
    turn_terminal_status: str,
) -> tuple[str, list[uuid.UUID]]:
    """Finalize intermediate segments and remove them from a terminal reply.

    ``call_llm`` still returns the complete visible A1+A2 value for transport.
    The final writer calls this helper so the terminal row stores only A2 and
    the durable transcript remains root,A1,injection,A2.
    """

    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role == "assistant",
                    ChatMessage.message_meta["turn_anchor_id"].as_string()
                    == str(turn_anchor_id),
                    ChatMessage.message_meta["artifact_role"].as_string()
                    == INTERMEDIATE_ASSISTANT_ARTIFACT_ROLE,
                    ChatMessage.message_meta["turn_status"].as_string()
                    == "running",
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).scalars()
    )
    if not rows:
        return content, []

    intermediate_ids = [row.id for row in rows]
    if turn_terminal_status in {"failed", "cancelled"}:
        for row in rows:
            row.message_meta = {
                **dict(row.message_meta or {}),
                "turn_status": turn_terminal_status,
            }

    prefix_parts: list[str] = []
    for row in rows:
        meta = dict(row.message_meta or {})
        prefix_parts.append(str(meta.get("visible_joiner_before") or ""))
        prefix_parts.append(str(row.content or ""))
    prefix = "".join(prefix_parts)
    if not content.startswith(prefix):
        if turn_terminal_status == "completed":
            logger.error(
                "[chat_history] terminal reply does not match durable intermediate "
                f"prefix anchor={turn_anchor_id}; refusing to split"
            )
        return content, intermediate_ids

    tail = content[len(prefix) :]
    # Separate provider rounds are joined for external presentation only.  The
    # durable row boundary already represents that separation.  A max-output
    # partial is different: its continuation is concatenated byte-for-byte, so
    # leading newlines in that continuation belong to the provider response.
    last_meta = dict(rows[-1].message_meta or {})
    if not last_meta.get("max_output_resume_prompt") and tail.startswith("\n\n"):
        tail = tail[2:]
    if not tail.strip():
        raise ValueError("terminal assistant tail must be non-empty")
    return tail, intermediate_ids


async def persist_assistant_reply_row(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    thinking: str | None = None,
    message_id: uuid.UUID | None = None,
    message_meta: dict[str, Any] | None = None,
    turn_anchor_id: uuid.UUID | None = None,
    sender_agent_id: uuid.UUID | None = None,
    participant_id: uuid.UUID | None = None,
    turn_terminal_status: str = "completed",
) -> uuid.UUID:
    """Persist a non-empty assistant reply in the caller's transaction."""
    from app.services.llm.failure_outcome import llm_failure_code, llm_failure_meta

    failure_code = llm_failure_code(content)
    failure_meta = llm_failure_meta(content)
    if failure_code and turn_terminal_status == "completed":
        turn_terminal_status = "failed"
    content = sanitize_user_visible_text(content or "")
    if not content.strip():
        raise ValueError("assistant reply content must be non-empty")
    turn_anchor = None
    if turn_anchor_id is not None:
        turn_anchor = await lock_turn_anchor_for_finalization(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_anchor_id=turn_anchor_id,
        )
    final_meta = dict(message_meta or {})
    if failure_code:
        final_meta.update(failure_meta)
    final_meta.setdefault("attachments", [])
    if turn_anchor_id is not None:
        if turn_terminal_status not in {"completed", "failed", "cancelled"}:
            raise ValueError("assistant reply turn status must be terminal")
        content, intermediate_ids = await terminal_assistant_tail(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_anchor_id=turn_anchor_id,
            content=content,
            turn_terminal_status=turn_terminal_status,
        )
        final_meta.update(
            {
                "turn_anchor_id": str(turn_anchor_id),
                "turn_status": turn_terminal_status,
                **(
                    {
                        "intermediate_assistant_ids": [
                            str(message_id) for message_id in intermediate_ids
                        ]
                    }
                    if intermediate_ids
                    else {}
                ),
            }
        )
    msg = ChatMessage(
        id=message_id or uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        sender_agent_id=sender_agent_id,
        participant_id=participant_id,
        role="assistant",
        content=content,
        conversation_id=conversation_id,
        message_meta=final_meta,
    )
    _capped = cap_thinking(sanitize_user_visible_text(thinking)) if thinking else None
    if _capped:
        msg.thinking = _capped
    db.add(msg)
    if turn_anchor_id is not None:
        from app.services.conversation_turn_lifecycle import transition_conversation_turn

        try:
            await transition_conversation_turn(
                db,
                agent_id=agent_id,
                conversation_id=conversation_id,
                turn_anchor_id=turn_anchor_id,
                status=turn_terminal_status,
            )
            if turn_terminal_status in {"completed", "failed"}:
                from app.services.turn_inbox import promote_next_turn_inbox

                try:
                    durable_session_id = uuid.UUID(str(conversation_id))
                except (TypeError, ValueError):
                    durable_session_id = None
                session = (
                    await db.get(ChatSession, durable_session_id)
                    if durable_session_id is not None
                    else None
                )
                if session is not None:
                    await promote_next_turn_inbox(
                        db,
                        session=session,
                    )
            msg.message_meta = {
                **dict(msg.message_meta or {}),
                "turn_terminal_published_by_lifecycle": True,
            }
        except LookupError:
            # Historical confirmation rows may outlive their pre-ChatSession
            # conversation. Preserve their existing terminal assistant write,
            # but never downgrade a lifecycle that was already admitted.
            if (getattr(turn_anchor, "message_meta", None) or {}).get(
                "conversation_turn_lifecycle"
            ) is True:
                raise
    from app.services.turn_delivery_recovery import prepare_terminal_delivery

    await prepare_terminal_delivery(db, msg)
    await db.flush()
    return msg.id


async def persist_initial_assistant_message_if_pristine(
    db: AsyncSession,
    *,
    session,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    message_meta: dict[str, Any] | None = None,
) -> ChatMessage | None:
    """Persist one ordinary assistant-first message before the first user row.

    The caller must hold the session row lock and commit this row in the same
    transaction as the first real incoming user message. This keeps an opened
    but unused session empty while still using the standard message model.
    """
    existing = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(session.id))
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    message_id = await persist_assistant_reply_row(
        db,
        agent_id=agent_id,
        user_id=user_id,
        content=content,
        conversation_id=str(session.id),
        message_meta=message_meta,
    )
    return await db.get(ChatMessage, message_id)


async def persist_assistant_reply_and_complete_turn(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    turn_anchor_id: uuid.UUID,
    thinking: str | None = None,
    message_meta: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Persist a final assistant reply and its durable Turn terminal state."""
    row_id = await persist_assistant_reply(
        db_session_factory,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        content=content,
        thinking=thinking,
        message_meta=message_meta,
        turn_anchor_id=turn_anchor_id,
        required=True,
    )
    assert row_id is not None
    return row_id




__all__ = [name for name in globals() if not name.startswith("__")]
