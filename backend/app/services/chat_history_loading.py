"""History loading and provider-neutral message expansion."""

from __future__ import annotations

from app.services.chat_history_shared import *  # noqa: F401,F403

async def _batch_load_display_names(
    db: AsyncSession,
    user_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Batch lookup display_name for a set of user ids. Missing ids are
    absent from the result dict (caller decides fallback)."""
    if not user_ids:
        return {}

    rows = await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))
    return {row.id: row.display_name for row in rows.all()}


async def load_messages_for_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    ctx_size: int,
) -> list[Any]:
    """Return active context for this session with complete-turn protection.

    ``ctx_size`` is retained for API compatibility but is not applied as a row
    cut. Results are oldest-first and compaction-aware.

    "Active" = ``compacted_into IS NULL`` — rows folded into a prior
    compaction's summary are skipped. If a compaction marker exists
    for this session, a synthetic ``_SyntheticSummaryMessage`` carrying
    the wrapped summary is prepended to the list.

    Returned items are duck-typed as ``ChatMessage`` (same attribute
    surface), so channels that need raw access (e.g. ``websocket.py``
    splitting ``tool_call`` rows into assistant + tool pairs) keep
    working unchanged.
    """
    rows_q = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.compacted_into.is_(None),
        )
        # id is the deterministic tiebreak: tool_call + assistant rows are each
        # written in their own transaction, so created_at (PostgreSQL now() =
        # txn-start) can tie within the same microsecond. Without a secondary
        # sort the order would flap between reloads.
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    all_active_rows = [
        row
        for row in reversed(rows_q.scalars().all())
        if not (
            _is_hidden_runtime_anchor(row)
            or
            isinstance(getattr(row, "message_meta", None), dict)
            and (
                row.message_meta.get("consumed_by_onmessage")
                or row.message_meta.get("delivery_claim")
                or row.message_meta.get("kind") == "subagent_parent_message"
                or row.message_meta.get("turn_inbox_state")
                in {"pending", "processing", "cancelled"}
            )
        )
    ]

    marker = await _load_active_compaction_marker(db, conversation_id=conversation_id)
    # Never apply a row-count cut here. Before the first compaction, the exact
    # dispatch guard decides whether the full active history fits and compacts
    # only complete old turns when necessary. After compaction, all surviving
    # rows are the protected suffix. Either way, slicing by rows could split a
    # tool-heavy turn or silently violate a model's larger keep_recent_turns.
    rows = all_active_rows

    if marker is not None:
        _prepend_compaction_context(
            rows,
            marker=marker,
            agent_id=agent_id,
            conversation_id=conversation_id,
        )

    return rows


async def load_recoverable_messages_for_turn(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    ctx_size: int,
) -> list[Any]:
    """Return a compaction-aware history window that preserves a recovery tail.

    Normal LLM history loading trims interrupted user tails. Startup recovery
    needs the opposite: keep the original compacted prefix shape, bound only the
    closed history before the anchor, and include every active row from the
    interrupted user anchor onward.
    """
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.compacted_into.is_(None),
        )
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
    )
    active_rows: list[Any] = [
        row
        for row in result.scalars().all()
        if row.id == turn_anchor_id
        or not (
            _is_hidden_runtime_anchor(row)
            or
            is_incomplete_delivery_progress(row)
            or (
                isinstance(getattr(row, "message_meta", None), dict)
                and (
                    row.message_meta.get("consumed_by_onmessage")
                    or row.message_meta.get("kind") == "subagent_parent_message"
                    or row.message_meta.get("turn_inbox_state")
                    in {"pending", "processing", "cancelled"}
                )
            )
        )
    ]

    anchor_idx = next((idx for idx, row in enumerate(active_rows) if row.id == turn_anchor_id), None)
    if anchor_idx is None:
        logger.warning(
            f"[chat_history] recoverable turn anchor {turn_anchor_id} "
            f"is not active in conversation {conversation_id}; skipping recovery"
        )
        return []

    prefix = active_rows[:anchor_idx]
    causally_prior_rows: list[Any] = []
    tail: list[Any] = []
    for row in active_rows[anchor_idx:]:
        meta = (
            row.message_meta
            if isinstance(getattr(row, "message_meta", None), dict)
            else {}
        )
        owned_turn_anchor_id = (
            meta.get("turn_anchor_id")
            or meta.get("subagent_turn_anchor_id")
            or (
                meta.get("turn_inbox_anchor_id")
                if meta.get("turn_inbox_state") == "delivered"
                else None
            )
        )
        if (
            tail
            and owned_turn_anchor_id
            and str(owned_turn_anchor_id) != str(turn_anchor_id)
        ):
            # A next-turn input is persisted while the previous turn is still
            # running. Tool/assistant rows from that previous turn can therefore
            # have later timestamps than this anchor. Restore causal turn order
            # without modifying any durable row: previous-turn completion first,
            # then the promoted anchor and its own continuation.
            causally_prior_rows.append(row)
            continue
        if tail and getattr(row, "role", None) == "user":
            # Subagent parent messages are inserted into the currently running
            # logical turn at an LLM round boundary.  They are separate durable
            # user rows, but carry the original turn anchor so a crashed worker
            # can rebuild the exact same multi-round tail instead of truncating
            # at the first injected message and replaying the task from scratch.
            injected_anchor_id = (
                meta.get("subagent_turn_anchor_id")
                or (
                    meta.get("turn_inbox_anchor_id")
                    if meta.get("turn_inbox_state") == "delivered"
                    else None
                )
            )
            if str(injected_anchor_id or "") != str(turn_anchor_id):
                break
        tail.append(row)
    # Startup recovery must not invent a row boundary either. It cannot safely
    # compact/replay an already-started turn, so retain the full active prefix;
    # the stateless dispatch guard will stop if that exact continuation cannot
    # fit.
    rows = prefix + causally_prior_rows + tail

    marker = await _load_active_compaction_marker(db, conversation_id=conversation_id)
    if marker is not None:
        _prepend_compaction_context(
            rows,
            marker=marker,
            agent_id=agent_id,
            conversation_id=conversation_id,
        )

    return rows


def is_incomplete_delivery_progress(row: Any) -> bool:
    """Return whether an assistant row is visible progress, not turn completion."""
    if getattr(row, "role", None) != "assistant":
        return False
    meta = row.message_meta if isinstance(getattr(row, "message_meta", None), dict) else {}
    return (
        meta.get("artifact_role") in {"thinking", "streaming_card"}
        and meta.get("turn_status") != "completed"
    )


def _trim_incomplete_user_turn_tail_rows(rows: list[Any]) -> list[Any]:
    """Drop a trailing interrupted user turn before replaying history to LLMs.

    IM ``/stop`` cancels the running coroutine, but the current user row is
    persisted before the LLM call and the assistant row is persisted only after
    a clean finish. If cancellation leaves ``user`` (and possibly completed
    ``tool_call`` rows) after the last real assistant reply, the next turn
    should start from the last complete exchange instead of replaying that
    half-finished tail.

    Pending confirmation cards are preserved: they append a ``tool_call`` row
    after an assistant intro without a trailing user row, and that is a valid
    suspended state.
    """
    if not rows:
        return []

    last_assistant_idx = -1
    for idx, row in enumerate(rows):
        if (
            getattr(row, "role", None) == "assistant"
            and not is_incomplete_delivery_progress(row)
        ):
            last_assistant_idx = idx

    if last_assistant_idx < 0 and not any(getattr(row, "role", None) == "tool_call" for row in rows):
        return rows

    scan_start = last_assistant_idx + 1
    for idx in range(scan_start, len(rows)):
        row = rows[idx]
        if isinstance(row, _SyntheticSummaryMessage):
            continue
        if getattr(row, "role", None) == "user":
            # A confirmation tool call is the one normalized tool lifecycle that
            # deliberately ends the current model run before a final assistant reply:
            # pending waits for a click, and done is replayed when that click resumes
            # the same turn. Ordinary completed tool rows without a final assistant
            # are still an interrupted /stop tail and must be discarded.
            next_user_idx = next(
                (
                    later_idx
                    for later_idx in range(idx + 1, len(rows))
                    if getattr(rows[later_idx], "role", None) == "user"
                ),
                len(rows),
            )
            if any(
                (
                    (
                        getattr(rows[later_idx], "role", None) == "assistant"
                        and not is_incomplete_delivery_progress(rows[later_idx])
                    )
                    or _is_confirmation_tool_call_row(rows[later_idx])
                )
                for later_idx in range(idx + 1, next_user_idx)
                if not isinstance(rows[later_idx], _SyntheticSummaryMessage)
            ):
                continue
            if idx < len(rows):
                logger.info(
                    "[chat_history] dropped incomplete stopped-turn tail "
                    f"from LLM replay: {len(rows) - idx} row(s)"
                )
            return rows[:idx]
    return rows


def _parse_tool_call_payload(content: str) -> dict[str, Any] | None:
    """Normalize a stored ``tool_call`` row's JSON into one shape that both
    readers build on — ``expand_tool_call_row`` (LLM replay) and
    ``parse_tool_call_for_display`` (web UI). Tolerates the canonical
    ``name`` / ``args`` schema and the legacy Feishu ``tool_name`` /
    ``arguments`` schema. Returns ``None`` for malformed / non-dict content;
    callers apply their own field defaults.
    """
    try:
        data = json.loads(content or "{}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    args = data.get("args")
    if args is None:
        args = data.get("arguments")
    return {
        "name": data.get("name") or data.get("tool_name") or "",
        "args": args,
        "status": data.get("status"),
        "result": data.get("result"),
        "reasoning_content": data.get("reasoning_content"),
        "assistant_content": data.get("assistant_content"),
        "recovery_prefix_messages": data.get("recovery_prefix_messages"),
        "call_id": data.get("call_id") or data.get("tool_call_id"),
        "round_id": data.get("round_id"),
        "round_tool_index": data.get("round_tool_index"),
    }


def _is_confirmation_tool_call_row(row: Any) -> bool:
    if getattr(row, "role", None) != "tool_call":
        return False
    payload = _parse_tool_call_payload(getattr(row, "content", ""))
    return bool(
        payload
        and payload.get("name") == "request_confirmation"
        and payload.get("status") in {"pending", "done"}
    )


def _validated_recovery_prefix(payload: dict[str, Any]) -> list[dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    raw_prefix = payload.get("recovery_prefix_messages")
    if not isinstance(raw_prefix, list):
        return prefix
    for item in raw_prefix:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"assistant", "user"} and isinstance(content, str):
            prefix.append({"role": role, "content": content})
    return prefix


def expand_tool_call_row(msg: Any) -> list[dict[str, Any]]:
    """Expand one persisted ``tool_call`` row into an OpenAI ``assistant``
    (carrying ``tool_calls``) + ``tool`` (result) message pair.

    This is the single source of truth shared by the web WebSocket path and
    the IM channels, so both replay identical tool-call history into the
    model. The mapping is byte-for-byte the historical websocket.py behaviour.
    Returns ``[]`` for malformed rows so callers skip them.
    """
    payload = _parse_tool_call_payload(msg.content)
    if payload is None:
        return []

    name = payload["name"] or "unknown"
    args = payload["args"] if payload["args"] is not None else {}
    status = payload["status"] or "done"
    result = payload["result"] or ""
    if status not in {"done", "pending"}:
        return []
    tc_id = str(payload.get("call_id") or f"call_{msg.id}")

    asst: dict[str, Any] = {
        "role": "assistant",
        "content": payload.get("assistant_content") or None,
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": (json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)),
                },
            }
        ],
    }
    if payload["reasoning_content"]:
        asst["reasoning_content"] = payload["reasoning_content"]

    # Lazy import: vision_inject pulls in optional deps; only needed here.
    from app.services.vision_inject import sanitize_history_tool_result

    prefix_messages = _validated_recovery_prefix(payload)

    if status == "pending":
        # A confirmation suspends the turn with an intentionally unpaired tool call.
        # No provider request is allowed while it remains pending; the real tool result
        # is created only by a button click or by an explicitly non-blocking card being
        # ignored through a later user message.
        return [*prefix_messages, asst]

    tool_msg = {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": sanitize_history_tool_result(str(result)),
    }
    return [*prefix_messages, asst, tool_msg]


def expand_tool_call_round(messages: list[Any]) -> list[dict[str, Any]]:
    """Rebuild one model-issued multi-tool response without splitting it.

    The live provider sequence is one assistant message containing every tool
    call, followed by the matching tool results in order. Persisted rows stay
    independently crash-recoverable, while this mapper restores that exact
    provider shape for future turns.
    """
    parsed: list[tuple[Any, dict[str, Any]]] = []
    for message in messages:
        payload = _parse_tool_call_payload(getattr(message, "content", ""))
        if payload is not None and payload.get("status") in {"done", "pending"}:
            parsed.append((message, payload))
    if not parsed:
        return []

    source_payload = next(
        (
            payload
            for _, payload in parsed
            if payload.get("assistant_content") is not None
            or payload.get("reasoning_content")
            or payload.get("recovery_prefix_messages")
        ),
        parsed[0][1],
    )
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": source_payload.get("assistant_content") or None,
        "tool_calls": [],
    }
    if source_payload.get("reasoning_content"):
        assistant["reasoning_content"] = source_payload["reasoning_content"]

    for message, payload in parsed:
        args = payload.get("args") if payload.get("args") is not None else {}
        assistant["tool_calls"].append(
            {
                "id": str(payload.get("call_id") or f"call_{message.id}"),
                "type": "function",
                "function": {
                    "name": payload.get("name") or "unknown",
                    "arguments": (
                        json.dumps(args, ensure_ascii=False)
                        if isinstance(args, dict)
                        else str(args)
                    ),
                },
            }
        )

    from app.services.vision_inject import sanitize_history_tool_result

    results = [
        {
            "role": "tool",
            "tool_call_id": str(payload.get("call_id") or f"call_{message.id}"),
            "content": sanitize_history_tool_result(str(payload.get("result") or "")),
        }
        for message, payload in parsed
        if payload.get("status") == "done"
    ]
    return [*_validated_recovery_prefix(source_payload), assistant, *results]


def build_llm_message_from_row(
    message: Any,
    *,
    wrap_user_names: bool = False,
    name_map: dict | None = None,
    include_thinking: bool = False,
) -> dict[str, Any]:
    """Build one provider-neutral message with structured attachment metadata."""
    content = message.content
    meta = (
        message.message_meta
        if isinstance(getattr(message, "message_meta", None), dict)
        else {}
    )
    delivery = meta.get("delivery") if isinstance(meta.get("delivery"), dict) else {}
    recall = delivery.get("recall") if isinstance(delivery.get("recall"), dict) else {}
    recalled = message.role in {"assistant", "tool_call"} and recall.get("status") == "recalled"
    if recalled:
        content = "[该消息已撤回，不应视为仍对用户可见]"
    attachments: list[dict[str, Any]] = []
    has_attachment_protocol = False
    if message.role == "user":
        from app.services.chat_attachments import extract_image_data_markers

        has_attachment_protocol = "attachments" in meta
        source_channel = meta.get("source_channel") if isinstance(meta, dict) else None
        content, attachments = normalize_chat_message_attachments(
            message.content,
            meta,
            source_channel,
        )
        from app.services.quoted_message import render_quoted_message_for_llm

        content = render_quoted_message_for_llm(
            content,
            meta.get("quoted_message"),
        )
        legacy_image_markers = (
            extract_image_data_markers(message.content)
            if "attachments" not in (meta if isinstance(meta, dict) else {})
            else []
        )
        if legacy_image_markers:
            content = "\n".join([content, *legacy_image_markers]).strip()
        sender_user_id = getattr(message, "sender_user_id", None) or getattr(message, "user_id", None)
        if wrap_user_names and sender_user_id is not None:
            content = wrap_with_sender(
                content,
                sender_user_id,
                (name_map or {}).get(sender_user_id),
            )

    entry: dict[str, Any] = {"role": message.role, "content": content}
    if attachments or has_attachment_protocol:
        entry["attachments"] = attachments
    if include_thinking and not recalled and getattr(message, "thinking", None):
        entry["thinking"] = message.thinking
    return entry


def build_llm_messages_from_rows(
    rows: list[Any],
    *,
    wrap_user_names: bool = False,
    name_map: dict | None = None,
    include_thinking: bool = False,
) -> list[dict[str, Any]]:
    """Map persisted ChatMessage rows into LLM-ready message dicts — the single
    source of truth for the rows→messages shape, shared by the IM history loader
    (``load_history_for_llm``) and the web conversation builder (websocket).

    - ``tool_call`` rows expand to the assistant(tool_calls) + tool(result) pair
      via ``expand_tool_call_row``.
    - ``user`` rows get a ``<sender …>`` prefix when ``wrap_user_names`` is set
      (group chats); other roles are never wrapped.
    - model ``thinking`` is carried only when ``include_thinking`` is set (the
      web client replays it into context; IM history intentionally does not).
    """
    # A tool-owned delivery (for example send_media) may promote its original
    # ``running`` row to ``done`` in place, while sibling results are appended
    # later. Group each typed round at its first physical row and replay the
    # model-issued order, independent of equal timestamps or random UUIDs.
    round_rows: dict[str, list[tuple[int, Any, dict[str, Any]]]] = {}
    for position, row in enumerate(rows):
        if getattr(row, "role", None) != "tool_call":
            continue
        payload = _parse_tool_call_payload(getattr(row, "content", ""))
        round_id = str((payload or {}).get("round_id") or "")
        if round_id and (payload or {}).get("status") in {"done", "pending"}:
            round_rows.setdefault(round_id, []).append((position, row, payload or {}))

    out: list[dict[str, Any]] = []
    emitted_rounds: set[str] = set()
    for m in rows:
        meta = m.message_meta if isinstance(getattr(m, "message_meta", None), dict) else {}
        # This visible row mirrors assistant_content on the confirmation tool
        # row. Keep it for UI rendering, but avoid replaying both copies.
        if m.role == "assistant" and meta.get("artifact_role") == "confirmation_intro":
            continue
        delivery = meta.get("delivery") if isinstance(meta.get("delivery"), dict) else {}
        recall = delivery.get("recall") if isinstance(delivery.get("recall"), dict) else {}
        if m.role == "tool_call" and recall.get("status") == "recalled":
            out.append({"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"})
            continue
        if m.role == "tool_call":
            payload = _parse_tool_call_payload(getattr(m, "content", ""))
            round_id = str((payload or {}).get("round_id") or "")
            if round_id:
                if round_id in emitted_rounds:
                    continue
                emitted_rounds.add(round_id)
                ordered = sorted(
                    round_rows.get(round_id, []),
                    key=lambda item: (
                        item[2].get("round_tool_index")
                        if isinstance(item[2].get("round_tool_index"), int)
                        else 2**31,
                        item[0],
                    ),
                )
                recalled = False
                ordered_rows: list[Any] = []
                for _, round_row, _ in ordered:
                    round_meta = (
                        round_row.message_meta
                        if isinstance(getattr(round_row, "message_meta", None), dict)
                        else {}
                    )
                    round_delivery = (
                        round_meta.get("delivery")
                        if isinstance(round_meta.get("delivery"), dict)
                        else {}
                    )
                    round_recall = (
                        round_delivery.get("recall")
                        if isinstance(round_delivery.get("recall"), dict)
                        else {}
                    )
                    if round_recall.get("status") == "recalled":
                        recalled = True
                        out.append({"role": "assistant", "content": "[该消息已撤回，不应视为仍对用户可见]"})
                    else:
                        ordered_rows.append(round_row)
                if not recalled:
                    out.extend(expand_tool_call_round(ordered_rows))
                else:
                    for round_row in ordered_rows:
                        out.extend(expand_tool_call_row(round_row))
                continue
            out.extend(expand_tool_call_row(m))
            continue
        out.append(
            build_llm_message_from_row(
                m,
                wrap_user_names=wrap_user_names,
                name_map=name_map,
                include_thinking=include_thinking,
            )
        )
        if (
            m.role == "assistant"
            and meta.get("artifact_role") == INTERMEDIATE_ASSISTANT_ARTIFACT_ROLE
            and isinstance(meta.get("max_output_resume_prompt"), str)
            and meta["max_output_resume_prompt"]
        ):
            # A max-output continuation prompt is provider control state, not a
            # human-authored ChatMessage.  Keep it on the durable intermediate
            # row and restore the exact assistant/user pair only for LLM replay.
            out.append(
                {
                    "role": "user",
                    "content": meta["max_output_resume_prompt"],
                }
            )
    return out




__all__ = [name for name in globals() if not name.startswith("__")]
