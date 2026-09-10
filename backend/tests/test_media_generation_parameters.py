"""Native generation controls reach the provider without silent loss."""

import json
import uuid

import httpx
import pytest

from app.services import media_ai_provider as provider
from app.services.media_ai_io import MediaAIError
from test_media_ai_provider import WAV, mock_http


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,args,options,expected", [
    ("image", {"size": "768*1024"}, {"size": "1024*1024", "seed": 17, "watermark": False},
     {"n": 1, "size": "768*1024", "seed": 17, "watermark": False}),
    ("video", {"duration": 9}, {"duration": 5, "resolution": "1080P", "seed": 17, "audio": False},
     {"resolution": "1080P", "ratio": "16:9", "duration": 9, "seed": 17, "audio": False}),
    ("audio", {"voice": "Cherry"}, {"voice": "Serena", "language_type": "Chinese", "instructions": "Calm"},
     {"text": "hello", "voice": "Cherry", "language_type": "Chinese", "instructions": "Calm"}),
])
async def test_bailian_native_options_reach_http_with_explicit_args_winning(monkeypatch, kind, args, options, expected):
    def upstream(request):
        body = json.loads(request.content)
        assert body["input" if kind == "audio" else "parameters"] == expected
        return httpx.Response(200, json={"output": {"task_id": "accepted"}})

    mock_http(monkeypatch, upstream)
    config = provider.connection({"api_key": "test-key"})
    path, payload = provider.generation_payload(config, {
        "prompt": "hello", "output_type": kind, "parameters": options, **args,
    }, [])
    assert (await provider.request(config, path, payload))["output"]["task_id"] == "accepted"


@pytest.mark.parametrize("platform", ["qwen", "custom", "tokenhub"])
def test_multiple_output_requests_are_rejected_before_provider_submission(platform):
    config = provider.connection({"model_id": "model", "provider": platform,
                                  "model": "hy-image-v3", "image_model": "qwen-image-2.0",
                                  "api_key": "test-key", "base_url": "https://gateway.example/v1"})
    with pytest.raises(MediaAIError, match="unsupportedOptions"):
        provider.generation_payload(config, {"prompt": "hello", "output_type": "image", "parameters": {"n": 2}}, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("format,raw,mime,extension", [
    ("mp3", b"ID3audio", "audio/mpeg", "mp3"),
    ("wav", WAV, "audio/wav", "wav"),
    ("flac", b"fLaCaudio", "audio/flac", "flac"),
    ("opus", b"OggSOpusHead", "audio/ogg", "ogg"),
    ("aac", b"\xff\xf1audio", "audio/aac", "aac"),
])
async def test_standard_speech_format_reaches_provider_and_saved_mime_matches(monkeypatch, format, raw, mime, extension):
    from app.services import media_ai_jobs

    def upstream(request):
        assert json.loads(request.content)["response_format"] == format
        return httpx.Response(200, content=raw)

    saved = []

    async def store(agent_id, path, data, *, content_type):
        saved.append((path, data, content_type))

    mock_http(monkeypatch, upstream)
    monkeypatch.setattr(media_ai_jobs, "store_agent_bytes", store)
    config = provider.connection({"model_id": "model", "provider": "custom", "model": "speech-model",
                                  "api_key": "test-key", "base_url": "https://gateway.example/v1"})
    path, payload = provider.generation_payload(config, {
        "prompt": "hello", "output_type": "audio", "parameters": {"response_format": format},
    }, [])
    result = await provider.request(config, path, payload)
    job = {"execution_agent_id": str(uuid.uuid4()), "output_type": "audio", "path_base": "workspace/media/speech"}
    await media_ai_jobs.save_generated_bytes(job, result["_bytes"])
    assert saved == [(f"workspace/media/speech.{extension}", raw, mime)]
    assert job["file"]["mime_type"] == mime


def test_raw_pcm_is_rejected_before_payment_without_silently_changing_format():
    config = provider.connection({"model_id": "model", "provider": "custom", "model": "speech-model",
                                  "api_key": "test-key", "base_url": "https://gateway.example/v1"})
    with pytest.raises(MediaAIError, match="unsupportedOptions"):
        provider.generation_payload(config, {
            "prompt": "hello", "output_type": "audio", "parameters": {"response_format": "pcm"},
        }, [])
