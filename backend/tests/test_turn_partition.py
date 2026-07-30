from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.llm.turn_partition import effective_keep_recent_turns, partition_turns


_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(role: str, index: int, *, anchor=None, completed=False, status=None):
    row_id = uuid.uuid4()
    meta = {}
    if anchor is not None:
        meta["turn_anchor_id"] = str(anchor)
    if completed:
        meta["turn_status"] = "completed"
    content = ""
    if role == "tool_call":
        content = json.dumps({"status": status or "done"})
    return SimpleNamespace(
        id=row_id,
        role=role,
        content=content,
        message_meta=meta,
        created_at=_BASE + timedelta(microseconds=index),
    )


def _closed_turn(index: int, *, tools: int = 0):
    user = _row("user", index * 100)
    rows = [user]
    for tool_idx in range(tools):
        rows.append(
            _row(
                "tool_call",
                index * 100 + tool_idx + 1,
                anchor=user.id,
                status="done",
            )
        )
    rows.append(
        _row(
            "assistant",
            index * 100 + tools + 1,
            anchor=user.id,
            completed=True,
        )
    )
    return rows


def test_effective_minimum_protects_eight_complete_turns():
    rows = [row for index in range(9) for row in _closed_turn(index)]

    partition = partition_turns(rows, keep_recent_turns=2)

    assert len(partition.compactable) == 1
    assert len(partition.protected) == 8
    assert partition.protected_rows == rows[2:]


def test_configured_value_above_floor_is_honoured():
    rows = [row for index in range(14) for row in _closed_turn(index)]

    partition = partition_turns(rows, keep_recent_turns=12)

    assert len(partition.compactable) == 2
    assert len(partition.protected) == 12


def test_primary_and_fallback_use_the_larger_keep_value():
    primary = SimpleNamespace(keep_recent_turns=8)
    fallback = SimpleNamespace(keep_recent_turns=12)

    assert effective_keep_recent_turns(primary, fallback) == 12


def test_tool_heavy_turn_is_never_split():
    turns = [_closed_turn(index, tools=12) for index in range(9)]
    rows = [row for turn in turns for row in turn]

    partition = partition_turns(rows, keep_recent_turns=8)

    assert partition.compactable_rows == turns[0]
    assert partition.protected_rows == [row for turn in turns[1:] for row in turn]


def test_initial_assistant_message_is_normalized_as_closed_history():
    greeting = _row("assistant", 0)
    turns = [_closed_turn(index + 1) for index in range(9)]
    rows = [greeting, *(row for turn in turns for row in turn)]

    partition = partition_turns(rows, keep_recent_turns=8)

    assert partition.compactable_rows == [greeting, *turns[0]]
    assert partition.protected_rows == [row for turn in turns[1:] for row in turn]
    assert partition.compactable[0].closed is True
    assert partition.compactable[0].unknown is False


def test_incomplete_turn_and_every_later_turn_are_protected():
    completed = [_closed_turn(index) for index in range(10)]
    interrupted_user = _row("user", 1001)
    interrupted_tool = _row(
        "tool_call",
        1002,
        anchor=interrupted_user.id,
        status="running",
    )
    rows = [row for turn in completed for row in turn]
    rows.extend([interrupted_user, interrupted_tool])

    partition = partition_turns(rows, keep_recent_turns=8)

    assert partition.protected_rows[-2:] == [interrupted_user, interrupted_tool]
    assert interrupted_user not in partition.compactable_rows
    assert interrupted_tool not in partition.compactable_rows


def test_same_timestamp_across_turn_boundary_is_unknown_and_protected():
    first = _closed_turn(0)
    second = _closed_turn(1)
    second[0].created_at = first[-1].created_at
    rows = first + second + [row for index in range(2, 11) for row in _closed_turn(index)]

    partition = partition_turns(rows, keep_recent_turns=8)

    assert not partition.compactable
    assert partition.protected_rows == rows


def test_current_anchor_is_separate_and_never_compactable():
    historical = [row for index in range(10) for row in _closed_turn(index)]
    current = _row("user", 2000)

    partition = partition_turns(
        historical + [current],
        current_anchor_id=str(current.id),
        keep_recent_turns=8,
    )

    assert partition.current is not None
    assert list(partition.current.rows) == [current]
    assert current not in partition.compactable_rows
    assert current not in partition.protected_rows
