import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.speech_recognition import (
    SpeechCredentialUnavailable,
    build_finish_task,
    build_run_task,
    parse_result_event,
    resolve_speech_credentials,
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


@pytest.mark.asyncio
async def test_resolve_speech_credentials_uses_only_independent_speech_config(monkeypatch):
    config = SimpleNamespace(
        enabled=True,
        provider="aliyun_dashscope",
        model="fun-asr-realtime",
        api_key_encrypted="encrypted-speech-key",
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: config)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    monkeypatch.setattr("app.services.speech_recognition.decrypt_data", lambda *_args: "speech-key")

    credentials = await resolve_speech_credentials(db, uuid.uuid4())

    assert credentials.api_key == "speech-key"
    assert credentials.provider == "aliyun_dashscope"
    assert credentials.model == "fun-asr-realtime"
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_speech_credentials_requires_explicit_config():
    result = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(SpeechCredentialUnavailable, match="尚未配置"):
        await resolve_speech_credentials(db, uuid.uuid4())
