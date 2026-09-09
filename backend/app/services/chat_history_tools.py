"""Durable tool-call and confirmation row persistence."""

from __future__ import annotations

from app.services.chat_history_ingest import *  # noqa: F401,F403

async def persist_tool_call(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    evt: dict[str, Any],
    turn_anchor_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Persist a tool-call marker into chat history using the canonical
    ``name`` / ``args`` schema — the single writer shared by the web WebSocket
    path and every IM channel, so the web UI and the LLM-history replay both
    observe it identically.

    ``evt`` is the ``on_tool_call`` payload emitted by the LLM caller
    (``name`` / ``call_id`` / ``args`` / ``status`` / ``result`` /
    ``reasoning_content``). ``running`` is stored as an append-only recovery
    marker before the tool executes; ``done`` is stored as a later append-only
    result row. Only done rows replay to the LLM. ``args`` are stored RAW —
    this persisted row is the single source of truth the LLM replays
    (``expand_tool_call_row``), so
    masking secrets here poisons the model (it copied ``connection_string:
    "******"`` back into new tool calls and looped on "Unsupported database
    type"). Sanitization is an OUTPUT-BOUNDARY concern, applied only where a
    human can see it (``parse_tool_call_for_display`` and the live WS
    broadcast), never at storage. The result is stored verbatim (never
    truncated). Failures are swallowed — a best-effort audit write must never
    break the live conversation.
    """
    if (evt or {}).get("_durable_persisted"):
        return None
    try:
        async with db_session_factory() as db:
            row_id = await persist_tool_call_row(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                evt=evt,
                turn_anchor_id=turn_anchor_id,
            )
            await db.commit()
            return row_id
    except Exception as e:
        logger.warning(f"[chat_history] persist_tool_call failed (non-fatal): {e}")
        return None


async def persist_tool_call_row(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    evt: dict[str, Any],
    turn_anchor_id: uuid.UUID | None = None,
    turn_fence_locked: bool = False,
) -> uuid.UUID | None:
    """Persist one tool-call marker in the caller's transaction.

    Unlike ``persist_tool_call`` this is strict: DB errors propagate to the
    caller. The LLM loop uses it before executing tools so a missing durable
    marker prevents side effects instead of creating an unrecoverable gap.
    """
    status = (evt or {}).get("status")
    if status not in {"running", "done"}:
        return None
    if turn_anchor_id is not None and not turn_fence_locked:
        from app.services.conversation_turn_lifecycle import (
            lock_conversation_turn_running,
        )

        await lock_conversation_turn_running(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_anchor_id=turn_anchor_id,
        )

    content = json.dumps(
        {
            "name": evt.get("name", ""),
            "call_id": evt.get("call_id", ""),
            "args": evt.get("args"),
            "status": status,
            "result": evt.get("result") or "",
            "reasoning_content": evt.get("reasoning_content"),
            "assistant_content": evt.get("assistant_content"),
            "recovery_prefix_messages": evt.get("recovery_prefix_messages") or [],
            "round_id": evt.get("round_id"),
            "round_tool_index": evt.get("round_tool_index"),
        },
        ensure_ascii=False,
        default=str,
    )
    row = ChatMessage(
        agent_id=agent_id,
        user_id=user_id,
        role="tool_call",
        content=content,
        conversation_id=conversation_id,
        message_meta={
            **({"turn_anchor_id": str(turn_anchor_id)} if turn_anchor_id is not None else {}),
            **({"responses_snapshot": evt["responses_snapshot"]} if evt.get("responses_snapshot") else {}),
        },
    )
    db.add(row)
    await db.flush()
    return row.id


async def close_running_tool_calls_for_stop(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
) -> int:
    """Append one durable stopped result for each unclosed exact-turn tool."""

    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role == "tool_call",
                    ChatMessage.message_meta["turn_anchor_id"].as_string()
                    == str(turn_anchor_id),
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
                .with_for_update()
            )
        ).scalars()
    )
    open_calls: dict[str, tuple[ChatMessage, dict[str, Any]]] = {}
    for row in rows:
        payload = _parse_tool_call_payload(row.content)
        if payload is None:
            continue
        call_id = str(payload.get("call_id") or row.id)
        if payload.get("status") == "running":
            open_calls[call_id] = (row, payload)
        elif payload.get("status") == "done":
            open_calls.pop(call_id, None)

    for call_id, (row, payload) in open_calls.items():
        await persist_tool_call_row(
            db,
            agent_id=agent_id,
            user_id=row.user_id,
            conversation_id=conversation_id,
            evt={
                "name": payload.get("name") or "unknown",
                "call_id": call_id,
                "args": payload.get("args"),
                "status": "done",
                "result": "[Generation stopped]",
                "reasoning_content": payload.get("reasoning_content"),
                "assistant_content": payload.get("assistant_content"),
                "responses_snapshot": (row.message_meta or {}).get("responses_snapshot"),
                "recovery_prefix_messages": payload.get("recovery_prefix_messages") or [],
                "round_id": payload.get("round_id"),
                "round_tool_index": payload.get("round_tool_index"),
            },
            turn_anchor_id=turn_anchor_id,
            turn_fence_locked=True,
        )
    return len(open_calls)


async def rewrite_tool_call_done_results(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    replacements: dict[uuid.UUID, tuple[str, str]],
    turn_anchor_id: uuid.UUID | None = None,
    expected_results: dict[uuid.UUID, str] | None = None,
) -> None:
    """Atomically replace results on exact durable ``done`` tool rows.

    A round-level output budget is applied after individual tool results have
    already been committed for crash recovery. This helper reconciles those
    exact rows before the shaped transcript is sent to the provider. It never
    appends correction rows because replay would interpret each additional
    ``done`` row as another assistant/tool pair.
    """
    if not replacements:
        return

    row_ids = list(replacements)
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.id.in_(row_ids),
            ChatMessage.agent_id == agent_id,
            ChatMessage.user_id == user_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "tool_call",
        )
        .with_for_update()
    )
    rows_by_id = {row.id: row for row in result.scalars().all()}
    missing = set(row_ids) - set(rows_by_id)
    if missing:
        raise RuntimeError(
            "tool result reconciliation could not find owned durable rows: "
            + ",".join(sorted(str(row_id) for row_id in missing))
        )

    for row_id, (expected_call_id, final_result) in replacements.items():
        row = rows_by_id[row_id]
        try:
            payload = json.loads(row.content or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"durable tool row {row_id} has invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"durable tool row {row_id} has a non-object payload")
        if payload.get("status") != "done":
            raise RuntimeError(f"durable tool row {row_id} is not done")
        stored_call_id = str(payload.get("call_id") or payload.get("tool_call_id") or "")
        if stored_call_id != expected_call_id:
            raise RuntimeError(
                f"durable tool row {row_id} call_id mismatch: "
                f"expected={expected_call_id!r} stored={stored_call_id!r}"
            )
        if expected_results is not None and str(payload.get("result") or "") != expected_results.get(row_id):
            raise RuntimeError(
                f"durable tool row {row_id} changed during out-of-transaction materialization"
            )
        if turn_anchor_id is not None:
            stored_anchor = str((row.message_meta or {}).get("turn_anchor_id") or "")
            if stored_anchor != str(turn_anchor_id):
                raise RuntimeError(
                    f"durable tool row {row_id} turn anchor mismatch: "
                    f"expected={turn_anchor_id} stored={stored_anchor or '<missing>'}"
                )

        payload["result"] = final_result
        row.content = json.dumps(payload, ensure_ascii=False, default=str)

    await db.flush()


def _pending_confirmation_payload(
    *,
    name: str,
    args: dict | None,
    turn_anchor_id: uuid.UUID | None,
    assistant_content: str | None = None,
    recovery_prefix_messages: list[dict[str, str]] | None = None,
    reasoning_content: str | None = None,
    round_id: str | None = None,
    call_id: str | None = None,
) -> str:
    payload = {
        "name": name,
        "args": args,
        "status": "pending",
        "result": "",
        "assistant_content": assistant_content,
        "recovery_prefix_messages": recovery_prefix_messages or [],
        "reasoning_content": reasoning_content,
        "round_id": round_id,
        "round_tool_index": 0,
        "call_id": call_id,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


async def persist_pending_confirmation_row(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    conversation_id: str,
    name: str,
    args: dict | None,
    turn_anchor_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    assistant_content: str | None = None,
    recovery_prefix_messages: list[dict[str, str]] | None = None,
    reasoning_content: str | None = None,
    round_id: str | None = None,
    responses_snapshot: dict | None = None,
    call_id: str | None = None,
) -> uuid.UUID:
    content = _pending_confirmation_payload(
        name=name,
        args=args,
        turn_anchor_id=turn_anchor_id,
        assistant_content=assistant_content,
        recovery_prefix_messages=recovery_prefix_messages,
        reasoning_content=reasoning_content,
        round_id=round_id,
        call_id=call_id,
    )
    row = ChatMessage(
        agent_id=agent_id,
        user_id=user_id,
        role="tool_call",
        content=content,
        conversation_id=conversation_id,
        message_meta={
            **(
                {
                    "turn_anchor_id": str(turn_anchor_id),
                    "turn_status": "suspended",
                }
                if turn_anchor_id is not None
                else {}
            ),
            **({"intended_user_id": str(user_id)} if user_id is not None else {}),
            **({"responses_snapshot": responses_snapshot} if responses_snapshot else {}),
        },
        created_at=created_at,
    )
    db.add(row)
    await db.flush()
    return row.id


async def persist_pending_confirmation(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    conversation_id: str,
    name: str,
    args: dict | None,
    turn_anchor_id: uuid.UUID | None = None,
    assistant_content: str | None = None,
    recovery_prefix_messages: list[dict[str, str]] | None = None,
    reasoning_content: str | None = None,
    round_id: str | None = None,
    responses_snapshot: dict | None = None,
    call_id: str | None = None,
) -> uuid.UUID:
    """Persist a SUSPENDED confirmation tool_call and return its row id.

    The agent called ``request_confirmation``; we record it as a normal ``tool_call``
    row with ``status="pending"`` / empty result and END the turn, waiting for the user.
    The row id IS the confirmation handle — the web card and the DingTalk ``outTrackId``
    reference it, and ``resolve`` fills THIS row's result + flips status to ``done`` to
    resume the loop (``expand_tool_call_row`` replays the pair). Unlike ``persist_tool_call``
    this is NOT best-effort: the caller needs the id to deliver the card, so errors raise.
    """
    async with db_session_factory() as db:
        row_id = await persist_pending_confirmation_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name=name,
            args=args,
            turn_anchor_id=turn_anchor_id,
            assistant_content=assistant_content,
            recovery_prefix_messages=recovery_prefix_messages,
            reasoning_content=reasoning_content,
            round_id=round_id,
            responses_snapshot=responses_snapshot,
            call_id=call_id,
        )
        await db.commit()
        return row_id




__all__ = [name for name in globals() if not name.startswith("__")]
