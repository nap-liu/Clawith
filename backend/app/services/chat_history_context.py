"""Display parsing and final LLM history loaders."""

from __future__ import annotations

from app.services.chat_history_assistant import *  # noqa: F401,F403

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
    if payload.get("session_ref"):
        display["toolSessionRef"] = payload["session_ref"]
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


__all__ = [name for name in globals() if not name.startswith("__")]
