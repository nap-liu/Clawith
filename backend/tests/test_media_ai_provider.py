"""Provider payloads, streaming, file typing and safe downloads."""

import json

import httpx
import pytest

from app.services import media_ai_io, media_ai_provider
from app.services.media_ai_io import MediaAIError, MediaInput, media_mime
from app.services.media_url_source import MediaUrlError

PNG = b"\x89PNG\r\n\x1a\n" + b"image"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(40)
MP4 = b"\x00\x00\x00\x18ftypisom" + bytes(12) + b"hdlr" + bytes(8) + b"vide"


def config():
    return media_ai_provider.connection({"api_key": "test-key"})


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport))


@pytest.mark.asyncio
@pytest.mark.parametrize("mime,raw,block", [("image/png", PNG, "image_url"),
                                          ("audio/wav", WAV, "input_audio"),
                                          ("video/mp4", MP4, "video_url")])
async def test_understanding_sends_original_media_and_collects_text_usage(monkeypatch, mime, raw, block):
    def upstream(request):
        body = json.loads(request.content)
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert body["stream"] is True
        assert body["modalities"] == ["text"]
        assert body["messages"][0]["content"][1]["type"] == block
        assert "data:" + mime in json.dumps(body)
        events = [
            {"choices": [{"delta": {"content": "observed "}}]},
            {"choices": [{"delta": {"content": "content"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"total_tokens": 42}},
        ]
        stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=stream)
    mock_http(monkeypatch, upstream)
    text, usage = await media_ai_provider.understand(config(), "Summarize", [MediaInput("file", mime, raw)])
    assert text == "observed content"
    assert usage["total_tokens"] == 42


@pytest.mark.asyncio
async def test_interrupted_analysis_is_not_reported_complete(monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))
    with pytest.raises(MediaAIError, match="providerFailed"):
        await media_ai_provider.understand(config(), "Summarize", [MediaInput("file", "image/png", PNG)])


@pytest.mark.parametrize("kind,files", [("image", []), ("image", [MediaInput("image.png", "image/png", PNG)]),
                                        ("audio", []), ("video", []), ("video", [MediaInput("image.png", "image/png", PNG)])])
def test_generation_payload_matches_native_contract(kind, files):
    path, body = media_ai_provider.generation_payload(config(), {"output_type": kind, "prompt": "hello"}, files)
    if kind == "video":
        assert path.endswith("video-generation/video-synthesis")
        assert body["parameters"]["duration"] == 5
        if files:
            assert body["input"]["media"][0]["type"] == "first_frame"
    elif kind == "audio":
        assert body["input"] == {"text": "hello", "voice": "Cherry", "language_type": "Auto"}
    else:
        assert body["parameters"]["n"] == 1
        assert body["input"]["messages"][0]["content"][-1] == {"text": "hello"}
        assert len(body["input"]["messages"][0]["content"]) == len(files) + 1


@pytest.mark.asyncio
async def test_video_submission_sets_async_header(monkeypatch):
    def upstream(request):
        assert request.headers["X-DashScope-Async"] == "enable"
        return httpx.Response(200, json={"output": {"task_id": "task-123"}})
    mock_http(monkeypatch, upstream)
    result = await media_ai_provider.request(config(), "/video", {}, asynchronous=True)
    assert result["output"]["task_id"] == "task-123"


@pytest.mark.asyncio
async def test_provider_error_keeps_code_without_echoing_key(monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(400, json={"code": "InvalidParameter", "message": "test-key"}))
    with pytest.raises(MediaAIError) as caught:
        await media_ai_provider.request(config(), "/video", {})
    assert caught.value.provider_code == "InvalidParameter"
    assert "test-key" not in str(caught.value)


def test_mp4_with_tail_metadata_is_recognized():
    data = MP4[:24] + bytes(3 * 1024 * 1024) + MP4[24:]
    assert media_mime(data) == "video/mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["<html>Bad gateway</html>", "[]"])
async def test_task_query_retries_non_object_gateway_response(monkeypatch, body):
    from datetime import datetime, timezone
    from app.services import media_ai_jobs

    attempts = []
    def upstream(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(502, text=body)
        return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED"}})
    async def check():
        return None
    async def no_wait(_):
        return None
    mock_http(monkeypatch, upstream)
    monkeypatch.setattr(media_ai_jobs.asyncio, "sleep", no_wait)
    result = await media_ai_jobs.retry_read(
        lambda: media_ai_provider.request(config(), "/api/v1/tasks/original-task"),
        check, datetime.now(timezone.utc).isoformat(),
    )
    assert result["output"]["task_status"] == "SUCCEEDED"
    assert len(attempts) == 2
    assert all(item.method == "GET" and item.url.path == "/api/v1/tasks/original-task" for item in attempts)


@pytest.mark.asyncio
async def test_download_refuses_private_destination():
    with pytest.raises(MediaUrlError):
        await media_ai_io.download_media("http://127.0.0.1/secrets")


@pytest.mark.asyncio
async def test_download_pins_public_ip_and_rejects_redirect_to_private(monkeypatch):
    async def resolve(*_):
        return ["8.8.8.8"]
    from app.services import media_url_source
    monkeypatch.setattr(media_url_source, "_resolve_host", resolve)
    def upstream(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "public.example"
        assert "authorization" not in request.headers
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
    mock_http(monkeypatch, upstream)
    with pytest.raises(MediaUrlError):
        await media_ai_io.download_media("https://public.example/image")
