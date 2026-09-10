"""Configured model headers reach providers and never third-party artifacts."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import media_ai_io, media_ai_provider as provider
from app.services.media_ai_io import MediaAIError
from test_media_ai_provider import PNG, mock_http


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,expected", [(None, "120"), ({"x-dashscope-wait-timeout": "60"}, "60"), ({}, None)])
async def test_native_bailian_generation_default_override_and_explicit_removal(monkeypatch, extra, expected):
    def upstream(request):
        assert request.headers.get("X-DashScope-Wait-Timeout") == expected
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["X-DashScope-Async"] == "enable"
        assert len(request.headers.get_list("X-DashScope-Wait-Timeout")) == (1 if expected else 0)
        return httpx.Response(200, json={"output": {"task_id": "task"}})

    mock_http(monkeypatch, upstream)
    cfg = provider.connection({"api_key": "test-key", "extra_headers": extra})
    result = await provider.request(cfg, "/api/v1/video", {}, asynchronous=True)
    assert result["output"]["task_id"] == "task"


@pytest.mark.asyncio
async def test_legacy_bailian_understanding_uses_configured_headers(monkeypatch):
    def upstream(request):
        assert request.headers["X-DashScope-Wait-Timeout"] == "60"
        assert request.headers["X-Tenant-Routing"] == "example"
        chunk = {"choices": [{"delta": {"content": "Observed"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    mock_http(monkeypatch, upstream)
    cfg = provider.connection({"api_key": "test-key", "extra_headers": {
        "X-DashScope-Wait-Timeout": "60", "X-Tenant-Routing": "example",
    }})
    result, _ = await provider.understand(cfg, "Describe", [media_ai_io.MediaInput("image", "image/png", PNG)])
    assert result == "Observed"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,model", [("custom", "video-model"), ("tokenhub", "kling-video-v3")])
async def test_custom_headers_survive_submission_and_polling_for_other_platforms(monkeypatch, platform, model):
    calls = []

    def upstream(request):
        calls.append(request.method)
        assert request.headers["X-Provider-Key"] == "private-extra"
        assert "X-DashScope-Wait-Timeout" not in request.headers
        if platform == "custom":
            return httpx.Response(200, json={"id": "task", "status": "in_progress"})
        data = {"id": "task", "status": "processing"}
        return httpx.Response(200, json={"code": 0, "data": data if request.method == "POST" else [data]})

    mock_http(monkeypatch, upstream)
    cfg = provider.connection({"model_id": "configured", "provider": platform, "model": model,
                               "api_key": "test-key", "base_url": "https://gateway.example/v1",
                               "extra_headers": {"X-Provider-Key": "private-extra"}})
    path, body = provider.generation_payload(cfg, {"prompt": "move", "output_type": "video"}, [])
    task = await provider.request(cfg, path, body, asynchronous=True)
    result = await provider.poll_generation(cfg, task["output"]["task_id"])
    assert result["output"]["task_status"] == "RUNNING"
    assert calls == ["POST", "GET"]


@pytest.mark.asyncio
async def test_provider_content_headers_never_follow_generated_url_download(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request.url.host)
        if request.url.host == "provider.example":
            assert request.headers["Authorization"] == "Bearer test-key"
            assert request.headers["X-Provider-Key"] == "private-extra"
        else:
            assert request.url.host == "result.example"
            assert "Authorization" not in request.headers
            assert "X-Provider-Key" not in request.headers
        return httpx.Response(200, content=b"video bytes")

    async def target(url):
        assert url == "https://result.example/generated.mp4?signature=exact"
        return SimpleNamespace(connect_urls=[url], canonical_url=url,
                               host_header="result.example", sni_hostname="result.example")

    mock_http(monkeypatch, upstream)
    monkeypatch.setattr(media_ai_io, "_managed_request_target", target)
    cfg = provider.connection({"model_id": "configured", "provider": "custom", "model": "video-model",
                               "api_key": "test-key", "base_url": "https://provider.example/v1",
                               "extra_headers": {"X-Provider-Key": "private-extra"}})
    assert await provider.download_result(cfg, "provider:/videos/task/content") == b"video bytes"
    assert await provider.download_result(cfg, "https://result.example/generated.mp4?signature=exact") == b"video bytes"
    assert seen == ["provider.example", "result.example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["qwen", "custom", "tokenhub"])
async def test_media_provider_redirect_does_not_forward_custom_credentials(monkeypatch, platform):
    seen = []

    def upstream(request):
        seen.append(request.url.host)
        assert request.url.host == "provider.example"
        return httpx.Response(307, headers={"Location": "https://elsewhere.example/receive"}, json={})

    mock_http(monkeypatch, upstream)
    cfg = provider.connection({"model_id": "configured", "provider": platform, "model": "hy-image-v3",
                               "image_model": "qwen-image-2.0", "api_key": "test-key",
                               "base_url": "https://provider.example/v1",
                               "extra_headers": {"X-Provider-Key": "private-extra"}})
    path, body = provider.generation_payload(cfg, {"prompt": "draw", "output_type": "image"}, [])
    with pytest.raises(MediaAIError, match="providerFailed"):
        await provider.request(cfg, path, body)
    assert seen == ["provider.example"]
