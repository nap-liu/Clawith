"""Model replay order, separate from immutable inbound/audit timestamps."""

from datetime import UTC, datetime
from typing import Any, Iterable


def _meta(row: Any) -> dict:
    value = getattr(row, "message_meta", None)
    return value if isinstance(value, dict) else {}


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def context_message_time(row: Any) -> datetime | None:
    meta = _meta(row)
    if meta.get("turn_inbox_state") == "delivered":
        consumed_at = _timestamp(meta.get("turn_inbox_consumed_at"))
        if consumed_at is not None:
            return consumed_at
    return _timestamp(getattr(row, "created_at", None))


def order_messages_for_context(rows: Iterable[Any]) -> list[Any]:
    """Replay delivered input where consumed, keeping each batch's FIFO order.

    Old rows without consumption timestamps retain their recorded order, except
    an injection preceding its own root is placed immediately after that root.
    No audit row or arrival timestamp is rewritten.
    """
    rows = list(rows)
    roots = {str(row.id): (index, row) for index, row in enumerate(rows)
             if getattr(row, "role", None) == "user" and getattr(row, "id", None)}
    minimum = datetime.min.replace(tzinfo=UTC)
    positions = []

    for index, row in enumerate(rows):
        position = index
        when = minimum
        meta = _meta(row)
        target = roots.get(str(meta.get("turn_inbox_anchor_id") or ""))
        if meta.get("turn_inbox_state") == "delivered" and target is not None:
            root_index, root = target
            same_scope = all(
                getattr(root, attr, None) == getattr(row, attr, None)
                for attr in ("agent_id", "conversation_id")
            )
            consumed_at = _timestamp(meta.get("turn_inbox_consumed_at"))
            if same_scope and root is not row:
                if consumed_at is not None:
                    when = consumed_at
                    position = len(rows)
                    for next_index in range(root_index + 1, len(rows)):
                        later = rows[next_index]
                        later_meta = _meta(later)
                        if later_meta.get("turn_inbox_state") == "delivered":
                            continue
                        later_time = context_message_time(later)
                        starts_next_turn = (
                            getattr(later, "role", None) == "user"
                            and str(later_meta.get("subagent_turn_anchor_id") or "") != str(root.id)
                        )
                        if starts_next_turn or (
                            later_time is not None and later_time > consumed_at
                        ):
                            position = next_index - 0.5
                            break
                elif index < root_index:
                    position = root_index + 0.5
        positions.append((position, when, index, row))
    # Ordinary rows retain their existing relative order, including recovery's
    # causal projection of a prior turn's late tool/assistant completion.
    return [row for _, _, _, row in sorted(positions, key=lambda item: item[:3])]
