"""Single source of truth for loading chat history into LLM context.

Before this module, every channel handler (websocket / feishu / dingtalk /
wecom / discord / teams / slack) carried its own history query and row slice.
The duplication made it hard to evolve history-handling (complete-turn
protection, compaction, attachment handling, …) without touching seven files at
once.

The module exposes two layers:

* ``load_messages_for_session`` — returns the chronologically-ordered
  message stream, with compaction-aware injection: rows whose
  ``compacted_into`` is non-NULL are filtered out, and the active
  ``ChatCompaction`` marker (if any) is prepended as a synthetic
  user-role message wrapped in ``<conversation-summary>``. Channels
  that need raw access (websocket splits ``tool_call`` rows into
  assistant + tool pairs) call this and shape themselves.
* ``load_history_for_llm`` — convenience wrapper that produces the
  provider-neutral message shape used by the IM channels.

The summary is reconstructed at load time from ``chat_compactions``;
it is **not** persisted to ``chat_messages`` (the chat UI shows the
original rows; only the LLM context sees the synthetic summary).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.chat_attachments import normalize_chat_message_attachments
from app.services.sender_attribution import wrap_with_sender
from app.services.user_output import sanitize_user_visible_text

if TYPE_CHECKING:
    from app.services.confirmation_service import PendingConfirmation

HIDDEN_ONBOARDING_ANCHOR_KIND = "onboarding_turn_anchor"
HIDDEN_PROJECT_CONTINUATION_ANCHOR_KIND = "project_subagent_external_continuation"
HIDDEN_RUNTIME_ANCHOR_KINDS = frozenset(
    {
        HIDDEN_ONBOARDING_ANCHOR_KIND,
        HIDDEN_PROJECT_CONTINUATION_ANCHOR_KIND,
    }
)
INTERMEDIATE_ASSISTANT_ARTIFACT_ROLE = "intermediate_assistant"


def _is_hidden_runtime_anchor(row: Any) -> bool:
    metadata = getattr(row, "message_meta", None)
    return bool(
        isinstance(metadata, dict)
        and metadata.get("kind") in HIDDEN_RUNTIME_ANCHOR_KINDS
    )


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
    sender_user_id: uuid.UUID | None = None
    sender_agent_id: uuid.UUID | None = None
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
    token_attribute = (
        f' tokens="{marker.summary_tokens}"'
        if marker.summary_tokens is not None
        else ""
    )
    body = (
        f'<conversation-summary epoch="{marker.epoch}"{token_attribute} '
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


def _prepend_compaction_context(
    rows: list[Any],
    *,
    marker: ChatCompaction,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> None:
    prefix: list[Any] = [
        _build_summary_message(
            marker=marker,
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
    ]
    rows[0:0] = prefix


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
            select(ChatSession).where(ChatSession.id == session.id).with_for_update()
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
    if not meta.get("scene_key"):
        from app.services.scene_service import (
            SCENE_SESSION_CONFIG_KEY,
            SCENE_STATUS_OK,
            resolve_scene_for_activation,
            scene_message_meta,
        )

        active_scene_key = str(
            (locked_session.im_config or {}).get(SCENE_SESSION_CONFIG_KEY) or ""
        )
        if active_scene_key:
            resolved_scene = await resolve_scene_for_activation(
                db,
                agent_id,
                active_scene_key,
            )
            if resolved_scene.status == SCENE_STATUS_OK:
                meta.update(scene_message_meta(resolved_scene.manifest))
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

    if not matched.consumed and is_turn_inbox_channel(source_channel):
        from app.services.conversation_turn_lifecycle import (
            ACTIVE_TURN_STATUS,
            conversation_turn_snapshot_for_session,
            transition_conversation_turn,
        )

        snapshot = conversation_turn_snapshot_for_session(locked_session)
        if snapshot.status == ACTIVE_TURN_STATUS and snapshot.anchor_id is not None:
            active_anchor = await db.get(ChatMessage, snapshot.anchor_id)
            active_meta = (
                dict(active_anchor.message_meta or {})
                if active_anchor is not None
                else {}
            )
            active_execution_agent_id = str(
                active_meta.get("execution_agent_id")
                or (active_anchor.agent_id if active_anchor is not None else "")
            )
            incoming_execution_agent_id = str(
                dict(row.message_meta or {}).get("execution_agent_id")
                or row.agent_id
            )
            same_execution_identity = bool(
                active_anchor is not None
                and active_anchor.user_id == row.user_id
                and active_execution_agent_id == incoming_execution_agent_id
            )
            row.message_meta = {
                **dict(row.message_meta or {}),
                "turn_inbox_state": "pending",
                "turn_inbox_anchor_id": str(snapshot.anchor_id),
                "turn_inbox_generation": snapshot.generation,
                # A different group sender must not inherit the active human's
                # execution identity. It remains durable and is promoted only
                # after the current generation terminates.
                "turn_inbox_mode": (
                    "current_turn" if same_execution_identity else "next_turn"
                ),
            }
            await db.flush()
            queued_to_running_turn = True
            from app.services.channel_dispatch import (
                mark_channel_turn_admitted,
                register_channel_receipt_anchor,
            )

            await mark_channel_turn_admitted()
            if same_execution_identity:
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
        message_meta=(
            {"turn_anchor_id": str(turn_anchor_id)}
            if turn_anchor_id is not None
            else {}
        ),
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
) -> uuid.UUID:
    content = _pending_confirmation_payload(
        name=name,
        args=args,
        turn_anchor_id=turn_anchor_id,
        assistant_content=assistant_content,
        recovery_prefix_messages=recovery_prefix_messages,
        reasoning_content=reasoning_content,
        round_id=round_id,
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
) -> tuple[str, list[uuid.UUID]]:
    """Remove already-durable intermediate segments from a terminal reply.

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

    prefix_parts: list[str] = []
    for row in rows:
        meta = dict(row.message_meta or {})
        prefix_parts.append(str(meta.get("visible_joiner_before") or ""))
        prefix_parts.append(str(row.content or ""))
    prefix = "".join(prefix_parts)
    if not content.startswith(prefix):
        logger.error(
            "[chat_history] terminal reply does not match durable intermediate "
            f"prefix anchor={turn_anchor_id}; refusing to split"
        )
        return content, []

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
    return tail, [row.id for row in rows]


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
    """Persist final assistant reply.

    Completion is represented by the assistant row itself. ``turn_anchor_id`` is
    accepted for older callers but no longer persists turn state.
    """
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
        ctx_size: Retained for caller compatibility; no row-level cut is made.
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
        user_ids = {
            getattr(m, "sender_user_id", None) or getattr(m, "user_id", None)
            for m in rows
            if m.role == "user"
            and (getattr(m, "sender_user_id", None) or getattr(m, "user_id", None)) is not None
        }
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

    return history


async def load_history_prefix_before_anchor(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    ctx_size: int,
    is_group: bool = False,
    include_thinking: bool = False,
) -> list[dict[str, Any]] | None:
    """Reload the compacted persisted prefix for one fresh user turn.

    Recovery is fail-safe: the exact anchor must still be present in active
    history. Only rows before it are returned, so later durable inputs queued
    on the same session cannot leak into this turn's frozen history. Those
    inputs remain available to the caller's standard round-boundary inbox.

    A compaction summary may precede the anchor and is retained. If compaction
    consumed the anchor itself, its exact boundary can no longer be recovered
    from the summary, so this function returns ``None``.
    """
    rows = await load_messages_for_session(
        db,
        agent_id=agent_id,
        conversation_id=conversation_id,
        ctx_size=ctx_size,
    )
    anchor_idx = next(
        (
            idx
            for idx, row in enumerate(rows)
            if not isinstance(row, _SyntheticSummaryMessage)
            and str(row.id) == str(turn_anchor_id)
        ),
        None,
    )
    if anchor_idx is None:
        return None

    prefix_rows = rows[:anchor_idx]
    wrap_users = False
    name_map: dict[uuid.UUID, str] = {}
    if is_group:
        user_ids = {
            getattr(row, "sender_user_id", None) or getattr(row, "user_id", None)
            for row in prefix_rows
            if row.role == "user"
            and (getattr(row, "sender_user_id", None) or getattr(row, "user_id", None))
            is not None
        }
        try:
            name_map = await _batch_load_display_names(db, user_ids)
            wrap_users = True
        except Exception as exc:
            logger.warning(
                "[chat_history] prefix display-name lookup failed: "
                f"{type(exc).__name__}: {exc}"
            )

    history = build_llm_messages_from_rows(
        prefix_rows,
        wrap_user_names=wrap_users,
        name_map=name_map,
        include_thinking=include_thinking,
    )
    return history


async def load_recoverable_history_for_turn(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    turn_anchor_id: uuid.UUID,
    ctx_size: int,
    is_group: bool = False,
    include_thinking: bool = False,
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
        user_ids = {
            getattr(m, "sender_user_id", None) or getattr(m, "user_id", None)
            for m in rows
            if m.role == "user"
            and (getattr(m, "sender_user_id", None) or getattr(m, "user_id", None)) is not None
        }
        try:
            name_map = await _batch_load_display_names(db, user_ids)
            wrap_users = True
        except Exception as e:
            logger.warning(f"[chat_history] display_name batch lookup failed, falling back to anonymous history: {e}")
            wrap_users = False

    history = build_llm_messages_from_rows(
        rows,
        wrap_user_names=wrap_users,
        name_map=name_map,
        include_thinking=include_thinking,
    )

    return history
