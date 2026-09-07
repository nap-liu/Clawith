"""Conversation-turn partitioning shared by history loading and compaction.

The context window is measured in tokens, but memory safety is measured in
complete user turns.  In particular, a tool-heavy turn may occupy dozens of
``chat_messages`` rows and must never be split merely because a row-count
window was reached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from app.services.message_context_order import context_message_time, order_messages_for_context


MIN_PROTECTED_RECENT_TURNS = 3
TERMINAL_TURN_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_TOOL_STATUSES = {"done", "failed", "cancelled", "error"}
OPEN_TOOL_STATUSES = {"pending", "running"}


def effective_keep_recent_turns(*models: Any) -> int:
    """Return the immutable-turn floor shared by one primary/fallback pair."""
    return max(
        MIN_PROTECTED_RECENT_TURNS,
        *(
            int(getattr(model, "keep_recent_turns", 0) or 0)
            for model in models
            if model is not None
        ),
    )


def _role(row: Any) -> str:
    value = getattr(row, "role", "")
    return str(getattr(value, "value", value) or "")


def _meta(row: Any) -> dict:
    value = getattr(row, "message_meta", None)
    return value if isinstance(value, dict) else {}


def _row_id(row: Any) -> str:
    return str(getattr(row, "id", "") or "")


def _tool_work_state(rows: tuple[Any, ...]) -> tuple[bool, bool]:
    """Return ``(has_open_work, has_unknown_work)`` for persisted tool rows.

    Tool execution is append-only: a running row may be followed by a done row
    with the same call id. Merely seeing an earlier ``running`` record therefore
    does not make a cancelled historical turn incomplete, while an unmatched
    pending/running tail must remain protected.
    """

    open_call_ids: set[str] = set()
    unknown = False
    for row in rows:
        if _role(row) != "tool_call":
            continue
        try:
            payload = json.loads(getattr(row, "content", "") or "{}")
        except (TypeError, ValueError):
            unknown = True
            continue
        if not isinstance(payload, dict):
            unknown = True
            continue

        status = str(payload.get("status") or "")
        raw_call_id = payload.get("call_id")
        call_id = str(raw_call_id) if raw_call_id else f"row:{_row_id(row)}"
        if status in OPEN_TOOL_STATUSES:
            open_call_ids.add(call_id)
        elif status in TERMINAL_TOOL_STATUSES:
            if raw_call_id:
                open_call_ids.discard(call_id)
        else:
            unknown = True
    return bool(open_call_ids), unknown


@dataclass(frozen=True)
class ConversationTurn:
    """One normalized history group, usually a user row through its reply."""

    rows: tuple[Any, ...]
    user_row: Any | None
    closed: bool
    current: bool = False
    unknown: bool = False

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(_row_id(row) for row in self.rows)


@dataclass(frozen=True)
class TurnPartition:
    compactable: tuple[ConversationTurn, ...]
    protected: tuple[ConversationTurn, ...]
    current: ConversationTurn | None

    @property
    def compactable_rows(self) -> list[Any]:
        return [row for turn in self.compactable for row in turn.rows]

    @property
    def protected_rows(self) -> list[Any]:
        return [row for turn in self.protected for row in turn.rows]


def _turn_closed(rows: tuple[Any, ...], user_row: Any | None) -> tuple[bool, bool]:
    """Return ``(closed, unknown)`` using explicit metadata then legacy rules."""
    if not rows:
        return False, True

    if user_row is None:
        # A conversation may legitimately begin with one persisted assistant
        # greeting. It is an ordinary, complete history item even though no
        # model turn ran to produce it. Keep this rule channel- and feature-
        # neutral so downstream history/compaction never needs to understand
        # the business source of the message.
        is_initial_assistant = len(rows) == 1 and _role(rows[0]) == "assistant"
        return is_initial_assistant, not is_initial_assistant

    has_open_tool_work, has_unknown_tool_work = _tool_work_state(rows)
    if has_open_tool_work or has_unknown_tool_work:
        return False, has_unknown_tool_work

    # The normalized /stop path commits terminal state on the durable user
    # anchor and closes any running tools, but deliberately does not invent an
    # assistant chat row. Once its tool work is fully closed, that cancelled
    # generation is complete historical evidence and must not pin every later
    # turn outside compaction forever.
    if _meta(user_row).get("turn_status") == "cancelled":
        return True, False

    last = rows[-1]
    if _role(last) != "assistant":
        # Unanswered, non-terminal user turns stay raw.
        return False, True

    meta = _meta(last)
    status = meta.get("turn_status")
    anchor = str(meta.get("turn_anchor_id") or "")
    if status is not None or anchor:
        # A failed/cancelled run is still a complete historical turn. Keeping
        # it forever as an "incomplete" suffix would prevent every later turn
        # from ever becoming compactable.
        return status in TERMINAL_TURN_STATUSES and anchor == _row_id(user_row), False

    # Legacy rows predate turn metadata.  Only the conservative, unambiguous
    # shape "user ... final assistant" is treated as closed.
    return True, False


def _injected_root_id(row: Any) -> str:
    """Return the durable root for an in-turn injected user row, if valid."""
    meta = _meta(row)
    subagent_root = meta.get("subagent_turn_anchor_id")
    if subagent_root:
        return str(subagent_root)
    if meta.get("turn_inbox_state") == "delivered":
        return str(meta.get("turn_inbox_anchor_id") or "")
    return ""


def _same_turn_identity(root: Any, injected: Any) -> bool:
    """Reject forged/cross-scope root references when identity is available."""
    # The sender is deliberately not part of the execution scope. A group IM
    # participant or a child-agent projection can legitimately interject into
    # the same root run with a different ``user_id``. The durable internal
    # anchor remains bounded by agent + conversation, which are the actual
    # tenant/runtime isolation keys for one history stream.
    for attr in ("agent_id", "conversation_id"):
        root_value = getattr(root, attr, None)
        injected_value = getattr(injected, attr, None)
        if root_value is not None and injected_value is not None and root_value != injected_value:
            return False
    return True


def _group_turns(rows: Iterable[Any], current_anchor_id: str | None) -> list[ConversationTurn]:
    groups: list[list[Any]] = []
    root_group_indexes: dict[str, int] = {}
    for row in order_messages_for_context(rows):
        if _role(row) == "user" and groups:
            injected_root = _injected_root_id(row)
            target_idx = root_group_indexes.get(injected_root)
            # A durable delivered/subagent anchor is the historical truth even
            # after the root is later marked terminal. At replay time every
            # successfully completed root is terminal, so requiring it to look
            # live would split one real execution into several fake incomplete
            # turns. The target must still be the latest chronological group and
            # have the same identity; stale/non-latest/cross-scope references are
            # never merged.
            if (
                injected_root
                and target_idx == len(groups) - 1
                and _row_id(groups[target_idx][0]) == injected_root
                and _same_turn_identity(groups[target_idx][0], row)
            ):
                groups[target_idx].append(row)
                continue
        if _role(row) == "user" or not groups:
            groups.append([row])
            if _role(row) == "user":
                root_group_indexes[_row_id(row)] = len(groups) - 1
        else:
            groups[-1].append(row)

    # UUIDs are only a deterministic tie-breaker, not creation order. If one
    # timestamp straddles two user groups, the historical boundary is
    # unknowable; protect both groups instead of inventing an order.
    timestamp_owner: dict[Any, int] = {}
    ambiguous_groups: set[int] = set()
    for group_idx, group in enumerate(groups):
        for row in group:
            created_at = context_message_time(row)
            if created_at is None:
                continue
            owner = timestamp_owner.setdefault(created_at, group_idx)
            if owner != group_idx:
                ambiguous_groups.update({owner, group_idx})

    turns: list[ConversationTurn] = []
    anchor = str(current_anchor_id or "")
    for group in groups:
        packed = tuple(group)
        user_row = packed[0] if packed and _role(packed[0]) == "user" else None
        is_current = bool(anchor and user_row is not None and _row_id(user_row) == anchor)
        closed, unknown = _turn_closed(packed, user_row)
        if is_current:
            closed = False
        turns.append(
            ConversationTurn(
                rows=packed,
                user_row=user_row,
                closed=closed,
                current=is_current,
                unknown=unknown or len(turns) in ambiguous_groups,
            )
        )
    return turns


def partition_turns(
    rows: Iterable[Any],
    *,
    current_anchor_id: str | None = None,
    keep_recent_turns: int = MIN_PROTECTED_RECENT_TURNS,
    minimum_protected_turns: int = MIN_PROTECTED_RECENT_TURNS,
) -> TurnPartition:
    """Split rows into a compactable prefix and an immutable recent suffix.

    The current turn, at least the latest three completed runs, and every item
    after that suffix boundary are protected. Older interrupted/unknown runs
    are abandoned history and remain in the continuous losslessly-archived
    prefix; they are never resumed or re-executed.
    """
    turns = _group_turns(rows, current_anchor_id)
    current = next((turn for turn in turns if turn.current), None)
    historical = [turn for turn in turns if not turn.current]
    protected_count = max(
        int(keep_recent_turns or 0),
        max(0, int(minimum_protected_turns or 0)),
    )

    closed_indexes = [idx for idx, turn in enumerate(historical) if turn.closed]
    recent_closed_start = (
        len(historical)
        if protected_count == 0
        else closed_indexes[-protected_count]
        if len(closed_indexes) >= protected_count
        else 0
    )
    # Protect the most recent complete runs and every newer tail item. An old
    # interrupted/ambiguous run that has at least this many later completed
    # runs is conclusively abandoned history: it may be archived/summarized but
    # is never re-executed. Otherwise one legacy ``running`` row would pin every
    # later turn forever and make required compaction impossible.
    protected_start = recent_closed_start

    compactable = tuple(historical[:protected_start])
    protected = tuple(historical[protected_start:])

    return TurnPartition(compactable=compactable, protected=protected, current=current)


def protected_recent_rows(
    rows: Iterable[Any],
    *,
    keep_recent_turns: int = MIN_PROTECTED_RECENT_TURNS,
) -> list[Any]:
    partition = partition_turns(rows, keep_recent_turns=keep_recent_turns)
    return partition.protected_rows + (
        list(partition.current.rows) if partition.current is not None else []
    )
