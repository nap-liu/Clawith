from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.llm.turn_partition import (
    current_turn_compactable_rows,
    effective_keep_recent_turns,
    partition_turns,
)


_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(
    role: str,
    index: int,
    *,
    anchor=None,
    completed=False,
    status=None,
    call_id=None,
    round_id=None,
):
    row_id = uuid.uuid4()
    meta = {}
    if anchor is not None:
        meta["turn_anchor_id"] = str(anchor)
    if completed:
        meta["turn_status"] = "completed"
    content = ""
    if role == "tool_call":
        content = json.dumps(
            {
                "status": status or "done",
                **({"call_id": call_id} if call_id else {}),
                **({"round_id": round_id} if round_id else {}),
            }
        )
    return SimpleNamespace(
        id=row_id,
        agent_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        conversation_id="conversation-a",
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
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


def test_effective_minimum_protects_three_complete_turns():
    turns = [_closed_turn(index) for index in range(4)]
    rows = [row for turn in turns for row in turn]

    partition = partition_turns(rows, keep_recent_turns=2)

    assert len(partition.compactable) == 1
    assert len(partition.protected) == 3
    assert partition.protected_rows == [row for turn in turns[1:] for row in turn]


def test_configured_value_above_floor_is_honoured():
    rows = [row for index in range(14) for row in _closed_turn(index)]

    partition = partition_turns(rows, keep_recent_turns=12)

    assert len(partition.compactable) == 2
    assert len(partition.protected) == 12


def test_primary_and_fallback_use_the_larger_keep_value():
    primary = SimpleNamespace(keep_recent_turns=8)
    fallback = SimpleNamespace(keep_recent_turns=12)

    assert effective_keep_recent_turns(primary, fallback) == 12


def test_primary_and_fallback_never_reduce_three_turn_floor():
    assert effective_keep_recent_turns(
        SimpleNamespace(keep_recent_turns=1),
        SimpleNamespace(keep_recent_turns=2),
    ) == 3


def test_tool_heavy_turn_is_never_split_and_latest_three_are_raw():
    turns = [_closed_turn(index, tools=30) for index in range(4)]
    rows = [row for turn in turns for row in turn]

    partition = partition_turns(rows, keep_recent_turns=3)

    assert partition.compactable_rows == turns[0]
    assert partition.protected_rows == [row for turn in turns[1:] for row in turn]
    assert all(len(turn.rows) == 32 for turn in partition.protected)


def test_failed_and_cancelled_terminal_turns_do_not_block_future_compaction():
    turns = [_closed_turn(index) for index in range(8)]
    turns[1][-1].message_meta["turn_status"] = "failed"
    turns[2][-1].message_meta["turn_status"] = "cancelled"
    rows = [row for turn in turns for row in turn]

    partition = partition_turns(rows, keep_recent_turns=3)

    assert partition.compactable_rows == [row for turn in turns[:5] for row in turn]
    assert partition.protected_rows == [row for turn in turns[5:] for row in turn]


def test_cancelled_user_anchor_with_closed_tool_tail_is_complete_history():
    before = _closed_turn(0)
    cancelled_user = _row("user", 100)
    cancelled_user.message_meta["turn_status"] = "cancelled"
    running = _row(
        "tool_call",
        101,
        anchor=cancelled_user.id,
        status="running",
        call_id="cancelled-call",
    )
    done = _row(
        "tool_call",
        102,
        anchor=cancelled_user.id,
        status="done",
        call_id="cancelled-call",
    )
    cancelled = [cancelled_user, running, done]
    later = [_closed_turn(index) for index in range(2, 6)]
    rows = [*before, *cancelled, *(row for turn in later for row in turn)]

    partition = partition_turns(rows, keep_recent_turns=3)

    assert partition.compactable_rows == [*before, *cancelled, *later[0]]
    assert partition.protected_rows == [row for turn in later[1:] for row in turn]


def test_abandoned_open_tool_tail_compacts_after_three_later_complete_runs():
    before = _closed_turn(0)
    cancelled_user = _row("user", 100)
    cancelled_user.message_meta["turn_status"] = "cancelled"
    running = _row(
        "tool_call",
        101,
        anchor=cancelled_user.id,
        status="running",
        call_id="still-running",
    )
    later = [_closed_turn(index) for index in range(2, 7)]
    rows = [*before, cancelled_user, running, *(row for turn in later for row in turn)]

    partition = partition_turns(rows, keep_recent_turns=3)

    assert partition.compactable_rows == [
        *before,
        cancelled_user,
        running,
        *later[0],
        *later[1],
    ]
    assert partition.protected_rows == [row for turn in later[2:] for row in turn]


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


def test_old_ambiguous_timestamp_is_archived_after_eight_later_complete_runs():
    first = _closed_turn(0)
    second = _closed_turn(1)
    second[0].created_at = first[-1].created_at
    later = [_closed_turn(index) for index in range(2, 11)]
    rows = first + second + [row for turn in later for row in turn]

    partition = partition_turns(rows, keep_recent_turns=8)

    assert partition.compactable_rows == first + second + later[0]
    assert partition.protected_rows == rows[len(partition.compactable_rows):]


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


def test_completed_old_rounds_inside_current_turn_can_be_compacted():
    current = _row("user", 2000)
    rows = [current]
    for index in range(6):
        row = _row(
            "tool_call", 2001 + index, anchor=current.id,
            status="done", call_id=f"call-{index}",
        )
        payload = json.loads(row.content)
        payload["round_id"] = f"round-{index}"
        row.content = json.dumps(payload)
        rows.append(row)
    partition = partition_turns(
        rows, current_anchor_id=str(current.id), keep_recent_turns=3,
    )

    assert current_turn_compactable_rows(partition.current) == rows[1:4]
    assert current_turn_compactable_rows(
        partition.current, keep_recent_rounds=0,
    ) == rows[1:]
    assert current not in current_turn_compactable_rows(partition.current)


def test_current_turn_compaction_never_crosses_open_tool_round():
    current = _row("user", 3000)
    closed = [
        _row("tool_call", 3001 + index, anchor=current.id, status="done")
        for index in range(5)
    ]
    pending = _row("tool_call", 3010, anchor=current.id, status="pending")
    later = _row("tool_call", 3011, anchor=current.id, status="done")
    partition = partition_turns(
        [current, *closed, pending, later],
        current_anchor_id=str(current.id),
        keep_recent_turns=3,
    )

    assert current_turn_compactable_rows(partition.current) == closed[:2]


def test_current_turn_compaction_uses_final_state_of_persisted_tool_calls():
    current = _row("user", 4000)
    first_round = [
        _row(
            "tool_call", 4001 + index, anchor=current.id, status=status,
            call_id=call_id, round_id="round-1",
        )
        for index, (call_id, status) in enumerate((
            ("call-a", "running"),
            ("call-b", "running"),
            ("call-a", "done"),
            ("call-b", "done"),
        ))
    ]
    second_running = _row(
        "tool_call", 4010, anchor=current.id, status="running",
        call_id="call-c", round_id="round-2",
    )
    partition = partition_turns(
        [current, *first_round, second_running],
        current_anchor_id=str(current.id),
    )

    assert current_turn_compactable_rows(
        partition.current, keep_recent_rounds=0,
    ) == first_round


def test_current_turn_compaction_protects_injected_user_and_later_rows():
    current = _row("user", 5000)
    first_round = [
        _row(
            "tool_call", 5001 + index, anchor=current.id, status=status,
            call_id="call-a", round_id="round-1",
        )
        for index, status in enumerate(("running", "done"))
    ]
    injected = _row("user", 5003)
    later = _row(
        "tool_call", 5004, anchor=current.id, status="done",
        call_id="call-b", round_id="round-2",
    )
    partition = partition_turns(
        [current, *first_round, injected, later],
        current_anchor_id=str(current.id),
    )

    assert current_turn_compactable_rows(
        partition.current, keep_recent_rounds=0,
    ) == first_round


def test_current_turn_compaction_rejects_cross_anchor_tool_rows():
    current = _row("user", 6000)
    late_prior = _row(
        "tool_call", 6001, anchor=uuid.uuid4(), status="done",
        call_id="prior-call", round_id="prior-round",
    )
    partition = partition_turns(
        [current, late_prior],
        current_anchor_id=str(current.id),
    )

    assert current_turn_compactable_rows(
        partition.current, keep_recent_rounds=0,
    ) == []


@pytest.mark.parametrize("injection_kind", ["im", "subagent"])
def test_in_turn_injected_user_stays_inside_one_complete_root_turn(injection_kind):
    turns = [_closed_turn(index) for index in range(3)]
    root = _row("user", 400)
    first_tool = _row("tool_call", 401, anchor=root.id, status="done", call_id="before")
    injected = _row("user", 402)
    if injection_kind == "im":
        injected.message_meta.update(
            {
                "turn_inbox_state": "delivered",
                "turn_inbox_anchor_id": str(root.id),
            }
        )
    else:
        injected.message_meta["subagent_turn_anchor_id"] = str(root.id)
    injected.user_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    second_tool = _row("tool_call", 403, anchor=root.id, status="done", call_id="after")
    final = _row("assistant", 404, anchor=root.id, completed=True)
    # This is how the persisted snapshot looks after the full runtime turn has
    # finished. The delivered anchor must still reconstruct the original turn.
    root.message_meta["turn_status"] = "completed"
    injected_turn = [root, first_tool, injected, second_tool, final]
    turns.extend([injected_turn, _closed_turn(5), _closed_turn(6), _closed_turn(7)])
    rows = [row for turn in turns for row in turn]

    partition = partition_turns(rows, keep_recent_turns=3)

    assert partition.compactable_rows == [row for turn in turns[:4] for row in turn]
    assert partition.compactable[-1].rows == tuple(injected_turn)
    assert partition.compactable[-1].closed is True
    assert partition.compactable[-1].unknown is False


def test_injected_reference_to_terminal_or_nonlatest_root_is_not_merged():
    old = _closed_turn(0)
    current = _closed_turn(1)
    forged = _row("user", 300)
    forged.message_meta.update(
        {
            "turn_inbox_state": "delivered",
            "turn_inbox_anchor_id": str(old[0].id),
        }
    )

    partition = partition_turns(old + current + [forged], keep_recent_turns=3)

    assert partition.current is None
    assert list(partition.protected[-1].rows) == [forged]
    assert partition.protected[-1].closed is False


def test_injected_reference_cannot_cross_agent_or_conversation_scope():
    for changed_attr, changed_value in (
        ("agent_id", uuid.UUID("00000000-0000-0000-0000-000000000009")),
        ("conversation_id", "conversation-b"),
    ):
        root = _row("user", 0)
        injected = _row("user", 1)
        injected.message_meta["subagent_turn_anchor_id"] = str(root.id)
        setattr(injected, changed_attr, changed_value)
        final = _row("assistant", 2, anchor=root.id, completed=True)

        partition = partition_turns([root, injected, final], keep_recent_turns=3)

        assert len(partition.protected) == 2
        assert list(partition.protected[0].rows) == [root]
        assert list(partition.protected[1].rows) == [injected, final]
