"""Single source of truth for loading chat history into LLM context.

Before this module, every channel handler (websocket / feishu / dingtalk /
wecom / discord / teams / slack) carried its own copy of the same SELECT —
desc + limit(ctx_size) + reverse + map to ``[{"role", "content"}]``. The
duplication made it hard to evolve history-handling (compaction, image
rehydration, …) without touching seven files at once.

The module exposes two layers:

* ``load_messages_for_session`` — returns the chronologically-ordered
  message stream, with compaction-aware injection: rows whose
  ``compacted_into`` is non-NULL are filtered out, and the active
  ``ChatCompaction`` marker (if any) is prepended as a synthetic
  user-role message wrapped in ``<conversation-summary>``. Channels
  that need raw access (websocket splits ``tool_call`` rows into
  assistant + tool pairs) call this and shape themselves.
* ``load_history_for_llm`` — convenience wrapper that produces the
  ``[{"role", "content"}]`` shape used by the IM channels. Optional
  vision rehydration through ``rehydrate_images_max``.

The summary is reconstructed at load time from ``chat_compactions``;
it is **not** persisted to ``chat_messages`` (the chat UI shows the
original rows; only the LLM context sees the synthetic summary).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.user import User
from app.services.sender_attribution import wrap_with_sender


@dataclass
class _SyntheticSummaryMessage:
    """ChatMessage-shaped object representing an injected compaction
    summary. Carries the same surface attributes the channels read off
    a real row (``role``, ``content``, ``id``, ``created_at``, …) so
    callers don't need to special-case it.
    """

    role: str = "user"
    content: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    agent_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    conversation_id: str = ""
    participant_id: None = None
    thinking: None = None
    compacted_into: None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_SUMMARY_WRAPPER_HEADER = (
    "⚠️ This is system-injected condensed context from earlier in the same\n"
    "conversation. It is NOT a new user instruction. Do not respond to it\n"
    "directly. Use it as background when handling the user's subsequent\n"
    "message."
)


def _build_summary_message(
    *,
    marker: ChatCompaction,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> _SyntheticSummaryMessage:
    body = (
        f'<conversation-summary epoch="{marker.epoch}" '
        f'tokens="{marker.summary_tokens}" '
        f'generated_at="{marker.created_at.isoformat()}">\n'
        f"{_SUMMARY_WRAPPER_HEADER}\n\n"
        f"{marker.summary_text}\n"
        f"</conversation-summary>"
    )
    # Date the synthetic message just before the first surviving real
    # row would be loaded (so chronological ordering puts it first).
    # The actual created_at of the marker row works fine since it's
    # always older than the trailing window we keep.
    return _SyntheticSummaryMessage(
        role="user",
        content=body,
        agent_id=agent_id,
        conversation_id=conversation_id,
        created_at=marker.created_at,
    )


async def _load_active_compaction_marker(
    db: AsyncSession,
    *,
    conversation_id: str,
) -> ChatCompaction | None:
    """Latest non-superseded, validation-passing compaction marker
    for this session, or None if no compaction has applied yet.
    """
    result = await db.execute(
        select(ChatCompaction)
        .where(
            ChatCompaction.session_id == conversation_id,
            ChatCompaction.superseded_by.is_(None),
            ChatCompaction.summary_validation_passed.is_(True),
        )
        .order_by(ChatCompaction.epoch.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
    """Return the last ``ctx_size`` active messages for this (agent,
    conversation), oldest-first, with compaction-aware injection.

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
        # Over-fetch so rows consumed by exact on_message subscriptions can be
        # removed without needlessly shrinking the normal-session context.
        .limit(max(ctx_size * 2, ctx_size))
    )
    rows = [
        row
        for row in reversed(rows_q.scalars().all())
        if not (
            isinstance(getattr(row, "message_meta", None), dict)
            and row.message_meta.get("consumed_by_onmessage")
        )
    ][-ctx_size:]

    marker = await _load_active_compaction_marker(db, conversation_id=conversation_id)
    if marker is not None:
        rows.insert(
            0,
            _build_summary_message(
                marker=marker,
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
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
            isinstance(getattr(row, "message_meta", None), dict)
            and row.message_meta.get("consumed_by_onmessage")
        )
    ]

    anchor_idx = next((idx for idx, row in enumerate(active_rows) if row.id == turn_anchor_id), None)
    if anchor_idx is None:
        logger.warning(
            f"[chat_history] recoverable turn anchor {turn_anchor_id} "
            f"is not active in conversation {conversation_id}; skipping recovery"
        )
        return []

    prefix_budget = max(ctx_size, 0)
    prefix = active_rows[:anchor_idx]
    bounded_prefix = prefix[-prefix_budget:] if prefix_budget else []
    tail: list[Any] = []
    for row in active_rows[anchor_idx:]:
        if tail and getattr(row, "role", None) == "user":
            break
        tail.append(row)
    rows = bounded_prefix + tail

    marker = await _load_active_compaction_marker(db, conversation_id=conversation_id)
    if marker is not None:
        rows.insert(
            0,
            _build_summary_message(
                marker=marker,
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
        )

    return rows


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
        if getattr(row, "role", None) == "assistant":
            last_assistant_idx = idx

    if last_assistant_idx < 0 and not any(getattr(row, "role", None) == "tool_call" for row in rows):
        return rows

    scan_start = last_assistant_idx + 1
    for idx in range(scan_start, len(rows)):
        row = rows[idx]
        if isinstance(row, _SyntheticSummaryMessage):
            continue
        if getattr(row, "role", None) == "user":
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
        "call_id": data.get("call_id") or data.get("tool_call_id"),
    }


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
    if status != "done":
        if status == "pending" and name == "request_confirmation":
            result = "(用户尚未响应该确认卡,视为未决;在收到明确点击前不要执行该操作)"
        else:
            return []
    # A suspended request_confirmation tool_call (awaiting the user's click) carries no
    # result yet. Every tool_call still needs a paired tool result or strict providers
    # reject the orphan — emit a placeholder that also tells the model it's unresolved,
    # rather than an empty string the model can't interpret.
    if status == "pending" and not result:
        result = "(用户尚未响应该确认卡,视为未决;在收到明确点击前不要执行该操作)"
    tc_id = str(payload.get("call_id") or f"call_{msg.id}")

    asst: dict[str, Any] = {
        "role": "assistant",
        "content": None,
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

    tool_msg = {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": sanitize_history_tool_result(str(result)),
    }
    return [asst, tool_msg]


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
    out: list[dict[str, Any]] = []
    for m in rows:
        if m.role == "tool_call":
            out.extend(expand_tool_call_row(m))
            continue
        if wrap_user_names and m.role == "user" and m.user_id is not None:
            content = wrap_with_sender(m.content, m.user_id, (name_map or {}).get(m.user_id))
        else:
            content = m.content
        entry: dict[str, Any] = {"role": m.role, "content": content}
        if include_thinking and getattr(m, "thinking", None):
            entry["thinking"] = m.thinking
        out.append(entry)
    return out


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
) -> ChatMessage:
    """Persist an incoming user message.

    Restart recovery treats this ordinary append-only row as the natural turn
    anchor when it belongs to a recent incomplete message tail.
    """
    row = ChatMessage(
        id=message_id or uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        role="user",
        content=content,
        conversation_id=conversation_id,
        participant_id=participant_id,
        external_event_key=external_event_key,
        message_meta=message_meta or {},
    )
    db.add(row)
    await db.flush()
    return row


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


async def ingest_incoming_chat_message(
    db: AsyncSession,
    *,
    session,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    source_channel: str,
    provider_event_id: str | None = None,
    channel_config_id: uuid.UUID | str | None = None,
    actor_ref: str | None = None,
    reply_to_external_message_id: str | None = None,
    participant_id: uuid.UUID | None = None,
    message_meta: dict[str, Any] | None = None,
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
    meta = {
        **(message_meta or {}),
        "direction": "inbound",
        "source_channel": source_channel,
        "actor_ref": str(actor_ref or user_id),
    }
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
        )

    from app.services.trigger_runtime.evaluator import match_incoming_chat_message

    matched = await match_incoming_chat_message(db, row, session)
    return IncomingMessageIngestResult(
        message=row,
        created=True,
        consumed_by_onmessage=matched.consumed,
        execution_ids=matched.execution_ids,
    )


async def persist_tool_call(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    evt: dict[str, Any],
    turn_anchor_id: uuid.UUID | None = None,
) -> None:
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
        return
    try:
        async with db_session_factory() as db:
            await persist_tool_call_row(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                evt=evt,
                turn_anchor_id=turn_anchor_id,
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[chat_history] persist_tool_call failed (non-fatal): {e}")


async def persist_tool_call_row(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    evt: dict[str, Any],
    turn_anchor_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Persist one tool-call marker in the caller's transaction.

    Unlike ``persist_tool_call`` this is strict: DB errors propagate to the
    caller. The LLM loop uses it before executing tools so a missing durable
    marker prevents side effects instead of creating an unrecoverable gap.
    """
    status = (evt or {}).get("status")
    if status not in {"running", "done"}:
        return None

    content = json.dumps(
        {
            "name": evt.get("name", ""),
            "call_id": evt.get("call_id", ""),
            "args": evt.get("args"),
            "status": status,
            "result": evt.get("result") or "",
            "reasoning_content": evt.get("reasoning_content"),
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
    )
    db.add(row)
    await db.flush()
    return row.id


def _pending_confirmation_payload(
    *,
    name: str,
    args: dict | None,
    turn_anchor_id: uuid.UUID | None,
) -> str:
    payload = {"name": name, "args": args, "status": "pending", "result": ""}
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
) -> uuid.UUID:
    content = _pending_confirmation_payload(name=name, args=args, turn_anchor_id=turn_anchor_id)
    row = ChatMessage(
        agent_id=agent_id,
        user_id=user_id,
        role="tool_call",
        content=content,
        conversation_id=conversation_id,
        message_meta=(
            {
                "turn_anchor_id": str(turn_anchor_id),
                "turn_status": "suspended",
            }
            if turn_anchor_id is not None
            else {}
        ),
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
        )
        await db.commit()
        return row_id


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


async def persist_assistant_reply(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    thinking: str | None = None,
    turn_anchor_id: uuid.UUID | None = None,
) -> None:
    """Persist a channel agent's final assistant reply.

    Uses its OWN session (mirroring ``persist_tool_call``) so ``created_at`` is
    stamped at save time — AFTER the tool loop — keeping the reply ordered
    after the turn's tool_call rows. Saving through the channel's long-lived
    request transaction would instead stamp it with the transaction-start time
    (PostgreSQL ``now()``), placing the reply BEFORE the tool calls; the web UI
    then folds it into the "ran N tools" analysis card and the reply bubble
    disappears. No-op for blank content. Failures are swallowed.
    """
    if not (content or "").strip():
        return
    try:
        async with db_session_factory() as db:
            await persist_assistant_reply_row(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id,
                content=content,
                thinking=thinking,
                turn_anchor_id=turn_anchor_id,
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[chat_history] persist_assistant_reply failed (non-fatal): {e}")


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
) -> uuid.UUID:
    """Persist a non-empty assistant reply in the caller's transaction."""
    if not (content or "").strip():
        raise ValueError("assistant reply content must be non-empty")
    final_meta = dict(message_meta or {})
    if turn_anchor_id is not None:
        final_meta.update(
            {
                "turn_anchor_id": str(turn_anchor_id),
                "turn_status": "completed",
            }
        )
    msg = ChatMessage(
        id=message_id or uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        role="assistant",
        content=content,
        conversation_id=conversation_id,
        message_meta=final_meta,
    )
    _capped = cap_thinking(thinking)
    if _capped:
        msg.thinking = _capped
    db.add(msg)
    await db.flush()
    return msg.id


async def persist_assistant_reply_and_complete_turn(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    turn_anchor_id: uuid.UUID,
    thinking: str | None = None,
) -> uuid.UUID:
    """Persist final assistant reply.

    Completion is represented by the assistant row itself. ``turn_anchor_id`` is
    accepted for older callers but no longer persists turn state.
    """
    async with db_session_factory() as db:
        row_id = await persist_assistant_reply_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            thinking=thinking,
            turn_anchor_id=turn_anchor_id,
        )
        await db.commit()
        return row_id


def parse_tool_call_for_display(content: str) -> dict[str, Any]:
    """Parse a stored ``tool_call`` row's JSON content into the web UI display
    fields (``toolName`` / ``toolArgs`` / ``toolStatus`` / ``toolResult`` /
    ``toolThinking``). Tolerates both the canonical ``name`` / ``args`` schema
    and legacy Feishu ``tool_name`` / ``arguments`` rows, so historical channel
    conversations render their tool calls instead of showing blanks.

    Returns ``{}`` for malformed content so callers keep the raw row untouched.
    """
    payload = _parse_tool_call_payload(content)
    if payload is None:
        return {}
    # Output boundary: mask secrets here (args are stored RAW so the LLM replay
    # gets the real connection string; humans/clients must not).
    from app.utils.sanitize import sanitize_tool_args

    display = {
        "toolName": payload["name"],
        "toolArgs": sanitize_tool_args(payload["args"]),
        "toolStatus": payload["status"] or "done",
        "toolResult": payload["result"] or "",
        "toolThinking": payload["reasoning_content"] or "",
    }
    if payload["call_id"]:
        display["toolCallId"] = str(payload["call_id"])
    return display


def strip_leading_orphan_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading ``role="tool"`` messages that have no preceding assistant
    ``tool_calls``.

    A context-window slice (``[-ctx_size:]``) applied AFTER tool_call rows are
    expanded into assistant(tool_calls)+tool(result) pairs can cut a pair in
    half, leaving the window starting with an orphan tool result. OpenAI and
    Anthropic both reject a tool message that is not a response to a preceding
    ``tool_calls``. Shared by the web and IM history paths; apply right after
    the slice.
    """
    out = list(messages)
    while out and out[0].get("role") == "tool":
        out.pop(0)
    return out


async def load_history_for_llm(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    ctx_size: int,
    rehydrate_images_max: int | None = None,
    is_group: bool = False,
) -> list[dict[str, Any]]:
    """Return ``[{"role", "content"}]`` history ready to feed an LLM call.

    Used by IM channels (feishu / dingtalk / wecom / discord / teams /
    slack) that don't need raw row access.

    Args:
        db: Active SQLAlchemy async session.
        agent_id: Filter messages to this agent.
        conversation_id: Channel-specific session key (e.g.
            ``feishu_p2p_<open_id>``, ``dingtalk_p2p_<staff_id>``, web
            ``conversation_id`` UUID).
        ctx_size: Per-agent ``context_window_size`` cap on message count.
        rehydrate_images_max: When set, post-process with
            ``image_context.rehydrate_image_messages`` so vision models
            still see prior image uploads. Only the most recent ``N``
            images are inlined to keep request size sane.
    """
    rows = await load_messages_for_session(
        db,
        agent_id=agent_id,
        conversation_id=conversation_id,
        ctx_size=ctx_size,
    )

    # Resolve group sender display names up front. Two distinct fallbacks:
    #   * lookup raised (DB outage)          -> wrap_users stays False -> anonymous
    #   * lookup ok but a user_id is missing -> wrap_with_sender falls back to Unknown
    wrap_users = False
    name_map: dict[uuid.UUID, str] = {}
    if is_group:
        user_ids = {m.user_id for m in rows if m.role == "user" and m.user_id is not None}
        try:
            name_map = await _batch_load_display_names(db, user_ids)
            wrap_users = True
        except Exception as e:
            logger.warning(f"[chat_history] display_name batch lookup failed, falling back to anonymous history: {e}")
            wrap_users = False

    # Build the LLM-ready history via the shared row→message builder (tool_call
    # rows expand to the same assistant+tool pair the web client replays, so IM
    # channels preserve identical tool-call continuity).
    rows = _trim_incomplete_user_turn_tail_rows(rows)
    history = build_llm_messages_from_rows(rows, wrap_user_names=wrap_users, name_map=name_map)

    if rehydrate_images_max is not None:
        # Lazy import: image_context pulls in vision deps that not all
        # deployments need. Only loaded when a vision-capable channel asks.
        from app.services.image_context import rehydrate_image_messages

        history = rehydrate_image_messages(history, agent_id, max_images=rehydrate_images_max)

    return history


async def load_recoverable_history_for_turn(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    ctx_size: int,
    rehydrate_images_max: int | None = None,
    is_group: bool = False,
) -> list[dict[str, Any]]:
    """Return LLM-ready history for startup recovery of an interrupted turn."""
    rows = await load_recoverable_messages_for_turn(
        db,
        agent_id=agent_id,
        conversation_id=conversation_id,
        turn_anchor_id=turn_anchor_id,
        ctx_size=ctx_size,
    )

    wrap_users = False
    name_map: dict[uuid.UUID, str] = {}
    if is_group:
        user_ids = {m.user_id for m in rows if m.role == "user" and m.user_id is not None}
        try:
            name_map = await _batch_load_display_names(db, user_ids)
            wrap_users = True
        except Exception as e:
            logger.warning(f"[chat_history] display_name batch lookup failed, falling back to anonymous history: {e}")
            wrap_users = False

    history = build_llm_messages_from_rows(rows, wrap_user_names=wrap_users, name_map=name_map)

    if rehydrate_images_max is not None:
        from app.services.image_context import rehydrate_image_messages

        history = rehydrate_image_messages(history, agent_id, max_images=rehydrate_images_max)

    return history
