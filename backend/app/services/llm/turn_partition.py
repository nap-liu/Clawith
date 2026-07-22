"""Conversation-turn partitioning shared by history loading and compaction.

The context window is measured in tokens, but memory safety is measured in
complete user turns.  In particular, a tool-heavy turn may occupy dozens of
``chat_messages`` rows and must never be split merely because a row-count
window was reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


MIN_PROTECTED_RECENT_TURNS = 8


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


def _tool_status(row: Any) -> str:
    if _role(row) != "tool_call":
        return ""
    try:
        import json

        payload = json.loads(getattr(row, "content", "") or "{}")
        return str(payload.get("status") or "") if isinstance(payload, dict) else ""
    except Exception:
        return ""


@dataclass(frozen=True)
class ConversationTurn:
    """One persisted user turn and every row up to the next user row."""

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
    if user_row is None or not rows:
        return False, True

    last = rows[-1]
    if _role(last) != "assistant":
        # Pending/running tool tails and interrupted user turns stay raw.
        return False, _tool_status(last) not in {"pending", "running"}

    meta = _meta(last)
    status = meta.get("turn_status")
    anchor = str(meta.get("turn_anchor_id") or "")
    if status is not None or anchor:
        return status == "completed" and anchor == _row_id(user_row), False

    # Legacy rows predate turn metadata.  Only the conservative, unambiguous
    # shape "user ... final assistant" is treated as closed.
    return True, False


def _group_turns(rows: Iterable[Any], current_anchor_id: str | None) -> list[ConversationTurn]:
    groups: list[list[Any]] = []
    for row in rows:
        if _role(row) == "user" or not groups:
            groups.append([row])
        else:
            groups[-1].append(row)

    # UUIDs are only a deterministic tie-breaker, not creation order. If one
    # timestamp straddles two user groups, the historical boundary is
    # unknowable; protect both groups instead of inventing an order.
    timestamp_owner: dict[Any, int] = {}
    ambiguous_groups: set[int] = set()
    for group_idx, group in enumerate(groups):
        for row in group:
            created_at = getattr(row, "created_at", None)
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
) -> TurnPartition:
    """Split rows into a compactable prefix and an immutable recent suffix.

    The current turn, every incomplete/unknown turn, and at least the latest
    eight completed turns are protected.  Consequently ``compactable`` is
    always a continuous prefix of complete turns.
    """
    turns = _group_turns(rows, current_anchor_id)
    current = next((turn for turn in turns if turn.current), None)
    historical = [turn for turn in turns if not turn.current]
    protected_count = max(int(keep_recent_turns or 0), MIN_PROTECTED_RECENT_TURNS)

    closed_indexes = [idx for idx, turn in enumerate(historical) if turn.closed]
    recent_closed_start = (
        closed_indexes[-protected_count]
        if len(closed_indexes) >= protected_count
        else 0
    )
    unsafe_indexes = [
        idx for idx, turn in enumerate(historical) if not turn.closed or turn.unknown
    ]
    unsafe_start = min(unsafe_indexes) if unsafe_indexes else len(historical)
    protected_start = min(recent_closed_start, unsafe_start)

    compactable = tuple(historical[:protected_start])
    protected = tuple(historical[protected_start:])
    # Defense in depth: no ambiguous turn may ever enter the summary input.
    if any(not turn.closed or turn.unknown for turn in compactable):
        first_unsafe = next(
            idx for idx, turn in enumerate(compactable) if not turn.closed or turn.unknown
        )
        protected = compactable[first_unsafe:] + protected
        compactable = compactable[:first_unsafe]

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
