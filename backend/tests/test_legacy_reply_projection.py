"""Observable replay behavior for terminal rows written by the old tool merge."""

import json
import uuid

from app.services.chat_history import build_llm_messages_from_rows


def test_legacy_merged_tool_text_is_removed_only_from_provider_replay():
    anchor_id = uuid.uuid4()
    raw_terminal = "I will inspect the brief now.\n\nThe brief is approved."
    tool_row = type(
        "StoredToolRow",
        (),
        {
            "id": uuid.uuid4(),
            "role": "tool_call",
            "content": json.dumps(
                {
                    "name": "read_file",
                    "call_id": "call-legacy",
                    "args": {"path": "brief.md"},
                    "status": "done",
                    "result": "approved",
                    "assistant_content": "I will inspect the brief now.",
                    "round_id": "legacy-round",
                    "round_tool_index": 0,
                }
            ),
            "message_meta": {"turn_anchor_id": str(anchor_id)},
        },
    )()
    final_row = type(
        "StoredAssistantRow",
        (),
        {
            "id": uuid.uuid4(),
            "role": "assistant",
            "content": raw_terminal,
            "thinking": None,
            "message_meta": {
                "turn_anchor_id": str(anchor_id),
                "turn_status": "completed",
            },
        },
    )()
    replay = build_llm_messages_from_rows([tool_row, final_row])

    assert replay[-1] == {"role": "assistant", "content": "The brief is approved."}
    assert [message["role"] for message in replay] == ["assistant", "tool", "assistant"]
    assert final_row.content == raw_terminal


def test_legacy_progress_only_terminal_row_is_omitted_from_provider_replay():
    anchor_id = uuid.uuid4()
    progress = "I will inspect the brief now."
    tool_row = type(
        "StoredToolRow",
        (),
        {
            "id": uuid.uuid4(),
            "role": "tool_call",
            "content": json.dumps(
                {
                    "name": "read_file",
                    "call_id": "call-progress-only",
                    "args": {"path": "brief.md"},
                    "status": "done",
                    "result": "approved",
                    "assistant_content": progress,
                    "round_id": "progress-only-round",
                    "round_tool_index": 0,
                }
            ),
            "message_meta": {"turn_anchor_id": str(anchor_id)},
        },
    )()
    final_row = type(
        "StoredAssistantRow",
        (),
        {
            "id": uuid.uuid4(),
            "role": "assistant",
            "content": f"{progress}\n\n",
            "thinking": None,
            "message_meta": {
                "turn_anchor_id": str(anchor_id),
                "turn_status": "completed",
            },
        },
    )()

    replay = build_llm_messages_from_rows([tool_row, final_row])

    assert [message["role"] for message in replay] == ["assistant", "tool"]
    assert final_row.content == f"{progress}\n\n"
