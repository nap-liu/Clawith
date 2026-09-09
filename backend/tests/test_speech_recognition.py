from app.services.speech_recognition import (
    build_finish_task,
    build_run_task,
    parse_result_event,
)


def test_build_run_task_uses_chinese_pcm_chat_defaults():
    request = build_run_task("task-1")
    assert request["header"] == {"action": "run-task", "task_id": "task-1", "streaming": "duplex"}
    assert request["payload"]["model"] == "fun-asr-realtime"
    params = request["payload"]["parameters"]
    assert params["format"] == "pcm"
    assert params["sample_rate"] == 16000
    assert params["language_hints"] == ["zh"]
    assert params["semantic_punctuation_enabled"] is False


def test_build_finish_task_matches_duplex_protocol():
    assert build_finish_task("task-1") == {
        "header": {"action": "finish-task", "task_id": "task-1", "streaming": "duplex"},
        "payload": {"input": {}},
    }


def test_parse_result_event_distinguishes_partial_and_final():
    partial = parse_result_event(
        {
            "header": {"event": "result-generated"},
            "payload": {"output": {"sentence": {"text": "你好", "sentence_id": 2, "sentence_end": False}}},
        }
    )
    assert partial is not None
    assert partial.text == "你好"
    assert partial.sentence_id == 2
    assert partial.is_final is False

    final = parse_result_event(
        {
            "header": {"event": "result-generated"},
            "payload": {
                "output": {"sentence": {"text": "你好。", "sentence_id": 2, "sentence_end": True}},
                "usage": {"duration": 3},
            },
        }
    )
    assert final is not None
    assert final.is_final is True
    assert final.duration_seconds == 3


def test_parse_result_event_ignores_heartbeat_and_other_events():
    assert parse_result_event({"header": {"event": "task-started"}}) is None
    assert (
        parse_result_event(
            {
                "header": {"event": "result-generated"},
                "payload": {"output": {"sentence": {"text": "", "heartbeat": True}}},
            }
        )
        is None
    )
