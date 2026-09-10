"""TokenHub provider side effects preserve references and resume the same job."""

import json

import httpx
import pytest

from app.services import media_ai_provider as provider
from app.services.media_ai_io import MediaAIError, MediaInput


def config(model="hy-image-v3", **overrides):
    return provider.connection({
        "model_id": "configured-model", "provider": "tokenhub", "model": model,
        "base_url": "https://tokenhub.tencentmaas.com/v1", "api_key": "test-key",
        **overrides,
    })


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler),
    ))


@pytest.mark.asyncio
async def test_image_reference_options_and_usage_survive_native_transport(monkeypatch):
    refs = ["https://storage.example/one.png?signature=exact", "https://third.example/two.png"]

    def upstream(request):
        assert request.url.path == "/v1/wand/hunyuan-image/v3-generation"
        assert request.headers["authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "hy-image-v3", "prompt": "draw", "images": refs,
            "size": "1024x1024", "seed": 13, "revise": False, "footnote": "Example",
        }
        return httpx.Response(200, json={
            "data": [{"url": "https://output.example/image.png?signature=result"}],
            "tokenhub_usage": {"total_tokens": 12}, "request_id": "image-request",
        })

    mock_http(monkeypatch, upstream)
    path, body = provider.generation_payload(config(), {
        "prompt": "draw", "output_type": "image", "size": "1024*1024",
        "parameters": {"images": refs[:1], "seed": 13, "revise": False, "footnote": "Example"},
    }, [MediaInput("two", "image/png", url=refs[1])])
    result = await provider.request(config(), path, body)
    assert provider.result_url(result, "image") == "https://output.example/image.png?signature=result"
    assert result["usage"] == {"total_tokens": 12}
    assert result["request_id"] == "image-request"


@pytest.mark.asyncio
async def test_video_submission_and_poll_preserve_original_task_and_all_references(monkeypatch):
    calls = []
    refs = ["https://storage.example/first.png", "https://storage.example/last.png"]

    def upstream(request):
        calls.append((request.method, request.url.path))
        assert "X-DashScope-Async" not in request.headers
        if request.method == "POST":
            body = json.loads(request.content)
            assert request.url.path == "/v1/wand/kling/image-to-video"
            assert body["contents"] == [
                {"type": "prompt", "text": "move @subject"},
                {"type": "element", "element_id": "subject"},
                {"type": "first_frame", "url": refs[0]},
                {"type": "last_frame", "url": refs[1]},
            ]
            assert body["settings"] == {"multi_shot": False, "audio": "native", "duration": 15, "resolution": "4k"}
            assert body["options"] == {"watermark_info": {"enabled": True}}
            return httpx.Response(200, json={"code": 0, "data": {"id": "original-task", "status": "submitted"}})
        return httpx.Response(200, json={"code": 0, "data": [
            {"id": "unrelated-task", "status": "processing"},
            {"id": "original-task", "status": "succeeded", "outputs": [
                {"type": "video", "url": "https://output.example/result.mp4?signature=exact"},
            ]},
        ], "tokenhub_usage": {"total_tokens": 30}})

    mock_http(monkeypatch, upstream)
    cfg = config("kling-video-v3")
    path, body = provider.generation_payload(cfg, {
        "prompt": "move @subject", "output_type": "video", "duration": 15, "resolution": "4K",
        "parameters": {"contents": [{"type": "element", "element_id": "subject"}],
                       "settings": {"multi_shot": False, "audio": "native"},
                       "options": {"watermark_info": {"enabled": True}}},
    }, [MediaInput("first", "image/png", url=refs[0], role="first_frame"),
        MediaInput("last", "image/png", url=refs[1], role="last_frame")])
    task = await provider.request(cfg, path, body, asynchronous=True)
    assert task["output"] == {"task_id": "original-task", "task_status": "PENDING"}
    result = await provider.poll_generation(cfg, task["output"]["task_id"])
    assert result["output"]["task_status"] == "SUCCEEDED"
    assert provider.result_url(result, "video") == "https://output.example/result.mp4?signature=exact"
    assert result["usage"] == {"total_tokens": 30}
    assert calls == [("POST", "/v1/wand/kling/image-to-video"), ("GET", "/v1/wand/kling/tasks/original-task")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("processing", "RUNNING"), ("failed", "FAILED")])
async def test_video_pending_and_failure_are_task_states_not_replayed_submissions(monkeypatch, status, expected):
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"code": 0, "data": [{"id": "task", "status": status}]})

    mock_http(monkeypatch, upstream)
    result = await provider.poll_generation(config("kling-video-v3"), "task")
    assert result["output"]["task_status"] == expected


@pytest.mark.asyncio
async def test_video_query_rejects_another_tasks_result(monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={
        "code": 0, "data": [{"id": "different", "status": "succeeded", "outputs": [
            {"type": "video", "url": "https://output.example/other.mp4"},
        ]}],
    }))
    with pytest.raises(MediaAIError, match="providerFailed"):
        await provider.poll_generation(config("kling-video-v3"), "requested")


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["hex", "url"])
async def test_speech_preserves_native_controls_and_normalizes_audio(monkeypatch, output_format):
    raw = b"ID3audio"
    url = "https://output.example/audio.mp3?signature=exact"

    def upstream(request):
        assert request.url.path == "/v1/wand/minimax-tts/sync_tts"
        body = json.loads(request.content)
        assert body["text"] == "hello"
        assert body["voice_setting"] == {"voice_id": "chosen-voice", "speed": 1.2, "emotion": "happy"}
        assert body["audio_setting"] == {"format": "mp3", "sample_rate": 24000}
        assert body["pronunciation_dict"] == {"tone": ["word/replacement"]}
        return httpx.Response(200, json={
            "data": {"status": 2, "audio": url if output_format == "url" else raw.hex()},
            "base_resp": {"status_code": 0}, "trace_id": "speech-request", "usage": {"total_token": 3},
        })

    mock_http(monkeypatch, upstream)
    cfg = config("minimax-speech-2.8-turbo")
    path, body = provider.generation_payload(cfg, {
        "prompt": "hello", "output_type": "audio", "voice": "chosen-voice",
        "parameters": {"voice_setting": {"speed": 1.2, "emotion": "happy"},
                       "audio_setting": {"format": "mp3", "sample_rate": 24000},
                       "pronunciation_dict": {"tone": ["word/replacement"]}, "output_format": output_format},
    }, [])
    result = await provider.request(cfg, path, body)
    assert result["request_id"] == "speech-request"
    assert result["usage"] == {"total_token": 3}
    if output_format == "hex":
        assert result["_bytes"] == raw
    else:
        assert provider.result_url(result, "audio") == url


@pytest.mark.asyncio
async def test_business_errors_keep_safe_codes(monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={
        "base_resp": {"status_code": 1008, "status_msg": "test-key should not escape"}, "data": None,
    }))
    cfg = config("minimax-speech-2.8-turbo")
    path, body = provider.generation_payload(cfg, {"prompt": "hi", "output_type": "audio"}, [])
    with pytest.raises(MediaAIError) as caught:
        await provider.request(cfg, path, body)
    assert caught.value.provider_code == "1008"
    assert "test-key" not in str(caught.value)


def test_platform_detection_does_not_hijack_other_hunyuan_endpoints():
    args = {"prompt": "draw", "output_type": "image"}
    path, _ = provider.generation_payload(config(provider="hunyuan"), args, [])
    assert path == "/wand/hunyuan-image/v3-generation"
    path, _ = provider.generation_payload(config(
        provider="hunyuan", base_url="https://api.hunyuan.cloud.tencent.com/v1",
    ), args, [])
    assert path == "/images/generations"
    path, _ = provider.generation_payload(config(base_url="https://gateway.example/prefix/v1"), args, [])
    assert path == "/wand/hunyuan-image/v3-generation"


@pytest.mark.parametrize("format", ["pcm", "pcmu_raw"])
def test_speech_raw_formats_fail_before_provider_submission(format):
    with pytest.raises(MediaAIError, match="unsupportedOptions"):
        provider.generation_payload(config("minimax-speech-2.8-turbo"), {
            "prompt": "hello", "output_type": "audio",
            "parameters": {"audio_setting": {"format": format}},
        }, [])


def test_requested_speech_subtitle_fails_before_provider_submission():
    with pytest.raises(MediaAIError, match="unsupportedOptions"):
        provider.generation_payload(config("minimax-speech-2.8-turbo"), {
            "prompt": "hello", "output_type": "audio", "parameters": {"subtitle_enable": True},
        }, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,history_video,expected_protocol", [
    ("video", False, "chat"), ("image", True, "chat"), ("image", False, "responses"),
])
async def test_kimi_video_uses_chat_before_submission_but_images_keep_responses(
    monkeypatch, kind, history_video, expected_protocol,
):
    calls = []
    part = {"type": "video_url", "video_url": {"url": "https://media.example/video.mp4"}}

    def upstream(request):
        calls.append(request.url.path)
        body = json.loads(request.content)
        if expected_protocol == "chat":
            assert request.url.path == "/v1/chat/completions"
            assert any(part in message["content"] for message in body["messages"]
                       if isinstance(message.get("content"), list))
            chunk = {"choices": [{"delta": {"content": "Observed"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        assert request.url.path == "/v1/responses"
        event = {"type": "response.completed", "response": {
            "id": "resp-media", "status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Observed"}]}],
        }}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")

    mock_http(monkeypatch, upstream)
    cfg = config("kimi-k3", api_protocol="openai_responses")
    response = await provider.understand_response(cfg, "Describe", [MediaInput(
        kind, "video/mp4" if kind == "video" else "image/png",
        url="https://media.example/video.mp4" if kind == "video" else "https://media.example/image.png",
    )], history=[{"role": "user", "content": [part]}] if history_video else [])
    assert response.content == "Observed"
    assert len(calls) == 1
    assert cfg["api_protocol"] == "openai_responses"
