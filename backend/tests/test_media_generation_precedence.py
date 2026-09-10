"""Continuation keeps current native controls ahead of inherited defaults."""

import json
import uuid

import pytest

from app.services import media_ai_runtime
from app.services.media_ai_tools import execute_media_tool
from test_media_ai_provider import MP4, PNG, WAV
from test_media_ai_sessions import run_task
from test_media_model_selection import make_model
import test_media_ai_runtime as runtime_tests

context = runtime_tests.context
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("provider,model,kind,prior,current,expected", [
    ("qwen", "wan3.0-video", "video", {"duration": 5, "ratio": "16:9", "resolution": "720P"},
     {"parameters": {"duration": 3, "ratio": "9:16", "resolution": "480P"}},
     {"duration": 3, "ratio": "9:16", "resolution": "480P"}),
    ("custom", "standard-video", "video", {"duration": 5, "ratio": "16:9", "resolution": "720P"},
     {"parameters": {"seconds": "3", "size": "720x1280"}},
     {"seconds": "3", "size": "720x1280"}),
    ("tokenhub", "kling-video-v3", "video", {"duration": 5, "ratio": "16:9", "resolution": "720P"},
     {"parameters": {"settings": {"duration": 3, "aspect_ratio": "9:16", "resolution": "1080p"}}},
     {"duration": 3, "aspect_ratio": "9:16", "resolution": "1080p"}),
    ("tokenhub", "minimax-speech-2.8-turbo", "audio", {"voice": "prior-voice"},
     {"parameters": {"voice_setting": {"voice_id": "current-voice", "speed": 0.9}}},
     {"voice_id": "current-voice", "speed": 0.9}),
    ("qwen", "qwen-image-2.0", "image", {"size": "1024*1024"},
     {"parameters": {"size": "768*1024"}}, {"size": "768*1024"}),
    ("qwen", "wan3.0-video", "video", {"duration": 5},
     {"duration": 7, "parameters": {"duration": 3}}, {"duration": 7}),
])
async def test_two_turn_generation_current_controls_reach_provider(context, monkeypatch, provider, model, kind, prior, current, expected):
    selected = await make_model(context, provider=provider, model=model, purposes=[f"{kind}_generation"],
                                base_url="https://provider.example/v1")
    calls = []

    async def generate(config, path, payload, **kwargs):
        calls.append(payload)
        return {"_bytes": {"image": PNG, "audio": WAV, "video": MP4}[kind]}

    monkeypatch.setattr(media_ai_runtime, "request", generate)
    context.arguments = {"output_type": kind, "prompt": "Create media", "model_id": str(selected.id), "files": [], **prior}
    first = json.loads(await execute_media_tool(context))
    context.tool_call_id = uuid.uuid4().hex
    context.arguments = {"output_type": kind, "prompt": "Revise media", "session_id": first["session_id"], "files": [], **current}
    second = json.loads(await execute_media_tool(context))
    results = await run_task(first)
    assert len(results) == 2
    assert all(row.message_meta["media_result"]["status"] == "completed" for row in results)
    assert results[-1].message_meta["media_result"]["task_id"] == second["task_id"]
    values = calls[-1]
    if provider == "qwen":
        values = values["parameters"]
    elif provider == "tokenhub":
        values = values["settings" if kind == "video" else "voice_setting"]
    assert {key: values[key] for key in expected} == expected
