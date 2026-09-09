"""Provider-independent media requests and normalized generation results."""

import base64
import json

import httpx
import pytest

from app.services import media_ai_provider as provider
from app.services.media_ai_io import MediaInput

PNG = b"\x89PNG\r\n\x1a\nimage"


def config(**overrides):
    return provider.connection({"model_id": "configured-model", "provider": "custom", "model": "image-model",
                                "base_url": "http://gateway.example/prefix/v1", "api_key": "test-key", **overrides})


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_standard_images_preserve_prefix_and_native_url_references(monkeypatch, edit):
    def upstream(request):
        assert request.url.path == "/prefix/v1/images/" + ("edits" if edit else "generations")
        body = json.loads(request.content)
        assert body["model"] == "image-model"
        if edit:
            assert body["images"] == [{"image_url": "https://storage.example/image?signature=exact"}]
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"total_tokens": 9}})
    mock_http(monkeypatch, upstream)
    media = [MediaInput("image", "image/png", url="https://storage.example/image?signature=exact")] if edit else []
    path, body = provider.generation_payload(config(), {"prompt": "draw", "output_type": "image"}, media)
    result = await provider.request(config(), path, body)
    assert result["_bytes"] == PNG
    assert result["usage"]["total_tokens"] == 9


@pytest.mark.asyncio
async def test_standard_speech_returns_bytes_without_bailian_fields(monkeypatch):
    def upstream(request):
        body = json.loads(request.content)
        assert request.url.path == "/prefix/v1/audio/speech"
        assert body == {"model": "image-model", "input": "hello", "voice": "alloy", "response_format": "mp3"}
        assert "X-DashScope-Async" not in request.headers
        return httpx.Response(200, content=b"ID3audio")
    mock_http(monkeypatch, upstream)
    path, body = provider.generation_payload(config(), {"prompt": "hello", "output_type": "audio"}, [])
    assert (await provider.request(config(), path, body))["_bytes"] == b"ID3audio"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_reference", [False, True])
async def test_standard_video_resumes_original_id_and_downloads_with_auth(monkeypatch, with_reference):
    seen = []
    reference = "https://storage.example/reference.png?signature=exact"
    def upstream(request):
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["seconds"] == "8"
            if with_reference:
                assert body["input_reference"] == {"image_url": reference}
            else:
                assert "input_reference" not in body
            return httpx.Response(200, json={"id": "original-video", "status": "queued"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video bytes")
        return httpx.Response(200, json={"id": "original-video", "status": "completed"})
    mock_http(monkeypatch, upstream)
    media = [MediaInput("workspace/reference.png", "image/png", url=reference)] if with_reference else []
    path, body = provider.generation_payload(config(), {"prompt": "move", "output_type": "video", "duration": 8}, media)
    task = await provider.request(config(), path, body, asynchronous=True)
    result = await provider.poll_generation(config(), task["output"]["task_id"])
    assert result["output"]["task_status"] == "SUCCEEDED"
    assert await provider.download_result(config(), provider.result_url(result, "video")) == b"video bytes"
    assert seen == [("POST", "/prefix/v1/videos"), ("GET", "/prefix/v1/videos/original-video"),
                    ("GET", "/prefix/v1/videos/original-video/content")]


@pytest.mark.asyncio
async def test_standard_terminal_failure_remains_failed_task(monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={"id": "video", "status": "failed", "error": {"code": "rejected"}}))
    result = await provider.poll_generation(config(), "video")
    assert result["output"] == {"task_id": "video", "task_status": "FAILED", "code": "rejected"}


@pytest.mark.asyncio
async def test_bailian_enterprise_model_keeps_native_generation(monkeypatch):
    cfg = config(provider="qwen", base_url="https://dashscope.example/compatible-mode/v1", image_model="configured-image")
    def upstream(request):
        assert request.url.path == "/api/v1/services/aigc/multimodal-generation/generation"
        assert json.loads(request.content)["model"] == "configured-image"
        return httpx.Response(200, json={"output": {"choices": [{"message": {"content": [{"image": "https://result.example/image"}]}}]}})
    mock_http(monkeypatch, upstream)
    path, body = provider.generation_payload(cfg, {"prompt": "draw", "output_type": "image"}, [])
    assert provider.result_url(await provider.request(cfg, path, body), "image") == "https://result.example/image"


@pytest.mark.asyncio
async def test_understanding_uses_shared_client_protocol_and_returns_native_snapshot(monkeypatch):
    from app.services.llm.client import LLMResponse
    calls = []
    snapshot = {"protocol": "openai_responses", "output": [{"type": "message"}]}
    class Client:
        async def stream(self, **kwargs):
            assert kwargs["messages"][0].responses_snapshot == snapshot
            assert kwargs["messages"][-1].content[1]["image_url"]["url"] == "https://media.example/a.png"
            return LLMResponse(content="observed", finish_reason="stop", responses_snapshot=snapshot)
        async def close(self):
            calls.append("closed")
    def factory(**kwargs):
        assert kwargs["api_protocol"] == "openai_responses"
        assert kwargs["base_url"] == "http://gateway.example/prefix/v1"
        return Client()
    monkeypatch.setattr(provider, "create_llm_client", factory)
    response = await provider.understand_response(
        config(api_protocol="openai_responses"), "summarize",
        [MediaInput("image", "image/png", url="https://media.example/a.png")],
        history=[{"role": "assistant", "content": "prior", "responses_snapshot": snapshot}],
    )
    assert response.content == "observed"
    assert response.responses_snapshot == snapshot
    assert calls == ["closed"]


@pytest.mark.asyncio
async def test_binary_result_is_saved_to_workspace_before_checkpoint(monkeypatch):
    import uuid
    from types import SimpleNamespace
    from app.services import media_ai_jobs
    writes = []
    job = {"execution_agent_id": str(uuid.uuid4()), "output_type": "image", "path_base": "workspace/media/job"}
    async def save(agent, path, data, **kwargs):
        writes.append((path, data))
    async def checkpoint(*args):
        assert writes
        assert "_bytes" not in args[2]
        assert args[2]["file"]["path"] == "workspace/media/job.png"
    monkeypatch.setattr(media_ai_jobs, "store_agent_bytes", save)
    monkeypatch.setattr(media_ai_jobs, "checkpoint", checkpoint)
    await media_ai_jobs.accept_result(SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4()), job,
                                     {"_bytes": PNG, "usage": {"total_tokens": 1}})
    assert writes == [("workspace/media/job.png", PNG)]
    assert job["status"] == "delivering"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,endpoint,history_only", [
    ("video", "https://dashscope.aliyuncs.com/compatible-mode/v1/responses", False),
    ("audio", "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", False),
    ("video", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True),
])
async def test_bailian_audio_video_selects_chat_before_submission(monkeypatch, kind, endpoint, history_only):
    requests = []
    url = "https://storage.example/movie.mp4?signature=exact" if kind == "video" else "https://storage.example/audio.wav"
    part = {"type": "video_url", "video_url": {"url": url}} if kind == "video" else {
        "type": "input_audio", "input_audio": {"data": url},
    }

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert body["model"] == "image-model"
        assert "opaque-state" not in request.content.decode()
        assert any(part in message["content"] for message in body["messages"]
                   if isinstance(message.get("content"), list))
        chunk = {"choices": [{"delta": {"content": "Understood"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    mock_http(monkeypatch, upstream)
    cfg = config(provider="qwen", api_protocol="openai_responses", base_url=endpoint)
    history = [{"role": "assistant", "content": "Previous answer", "responses_snapshot": {
        "protocol": "openai_responses", "output": [{"type": "reasoning", "encrypted_content": "opaque-state"}],
    }}]
    if history_only:
        history.insert(0, {"role": "user", "content": [part]})
    media = [] if history_only else [MediaInput(kind, "video/mp4" if kind == "video" else "audio/wav", url=url)]
    response = await provider.understand_response(cfg, "Follow up", media, history=history)
    assert response.content == "Understood" and response.responses_snapshot is None
    assert len(requests) == 1
    assert cfg["api_protocol"] == "openai_responses"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,kind", [
    ("https://gateway.example/compatible-mode/v1", "video"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "image"),
])
async def test_provider_label_does_not_override_third_party_or_image_responses(monkeypatch, endpoint, kind):
    def upstream(request):
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        assert "input" in body and "messages" not in body
        event = {"type": "response.completed", "response": {
            "id": "resp-media", "status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Observed"}]}],
        }}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")
    mock_http(monkeypatch, upstream)
    response = await provider.understand_response(
        config(provider="qwen", api_protocol="openai_responses", base_url=endpoint), "Describe",
        [MediaInput(kind, "video/mp4" if kind == "video" else "image/png", url="https://media.example/input")],
    )
    assert response.content == "Observed" and response.responses_snapshot["protocol"] == "openai_responses"
