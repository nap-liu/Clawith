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

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
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
        .order_by(ChatMessage.created_at.desc())
        .limit(ctx_size)
    )
    rows: list[Any] = list(reversed(rows_q.scalars().all()))

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
    result = payload["result"] or ""
    tc_id = f"call_{msg.id}"

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


async def persist_tool_call(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    evt: dict[str, Any],
) -> None:
    """Persist a completed tool call into chat history using the canonical
    ``name`` / ``args`` schema — the single writer shared by the web WebSocket
    path and every IM channel, so the web UI and the LLM-history replay both
    observe it identically.

    ``evt`` is the ``on_tool_call`` payload emitted by the LLM caller
    (``name`` / ``call_id`` / ``args`` / ``status`` / ``result`` /
    ``reasoning_content``). Only ``status == "done"`` is stored; running
    notifications are ignored. ``args`` are sanitized (secrets masked, base64
    images redacted) before storage, mirroring the web path. The result is
    stored verbatim (never truncated). Failures are swallowed — a best-effort
    audit write must never break the live conversation.
    """
    if (evt or {}).get("status") != "done":
        return

    from app.utils.sanitize import sanitize_tool_args

    content = json.dumps(
        {
            "name": evt.get("name", ""),
            "args": sanitize_tool_args(evt.get("args")),
            "status": "done",
            "result": evt.get("result") or "",
            "reasoning_content": evt.get("reasoning_content"),
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        async with db_session_factory() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="tool_call",
                    content=content,
                    conversation_id=conversation_id,
                )
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[chat_history] persist_tool_call failed (non-fatal): {e}")


async def persist_assistant_reply(
    db_session_factory,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    thinking: str | None = None,
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
            msg = ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content=content,
                conversation_id=conversation_id,
            )
            if thinking:
                msg.thinking = thinking
            db.add(msg)
            await db.commit()
    except Exception as e:
        logger.warning(f"[chat_history] persist_assistant_reply failed (non-fatal): {e}")


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
    return {
        "toolName": payload["name"],
        "toolArgs": payload["args"],
        "toolStatus": payload["status"] or "done",
        "toolResult": payload["result"] or "",
        "toolThinking": payload["reasoning_content"] or "",
    }


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

    # Build the LLM-ready history. tool_call rows are expanded into the same
    # assistant(tool_calls) + tool(result) pair the web client replays, so IM
    # channels preserve identical tool-call continuity instead of dropping it.
    history: list[dict[str, Any]] = []
    for m in rows:
        if m.role == "tool_call":
            history.extend(expand_tool_call_row(m))
            continue
        if wrap_users and m.role == "user" and m.user_id is not None:
            content = wrap_with_sender(m.content, m.user_id, name_map.get(m.user_id))
        else:
            content = m.content
        history.append({"role": m.role, "content": content})

    if rehydrate_images_max is not None:
        # Lazy import: image_context pulls in vision deps that not all
        # deployments need. Only loaded when a vision-capable channel asks.
        from app.services.image_context import rehydrate_image_messages

        history = rehydrate_image_messages(history, agent_id, max_images=rehydrate_images_max)

    return history
