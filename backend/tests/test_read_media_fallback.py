"""Legacy configured backup uses one safe provider attempt without a second loop."""

import json

import httpx
import pytest

from app.services.media_ai_io import MediaAIError, MediaInput
from app.services.media_ai_provider import understand_response

pytestmark = pytest.mark.asyncio


def config():
    backup = {"model_id": "backup", "provider": "openai", "api_protocol": "openai_compatible",
              "base_url": "https://backup.example/v1", "model": "backup-model", "api_key": "fake"}
    return {**backup, "model_id": "primary", "model": "primary-model",
            "base_url": "https://primary.example/v1", "fallback_connection": backup}


@pytest.mark.parametrize("failure", ["refused", "partial", "network"])
async def test_backup_only_after_explicit_refusal_before_any_output(monkeypatch, failure):
    seen = []
    original = httpx.AsyncClient

    async def provider(request):
        seen.append((request.url.host, json.loads(request.content)))
        if request.url.host == "backup.example":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"choices":[{"delta":{"content":"Backup completed"},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'))
        if failure == "refused":
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "Temporarily unavailable"}})
        if failure == "network":
            raise httpx.ConnectError("Fixture connection lost", request=request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            'data: {"choices":[{"delta":{"content":"Partial answer"},"finish_reason":null}]}\n\n'
            'data: {"error":{"code":"unavailable","message":"Unavailable","status":503}}\n\n'))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(provider)}))
    media = [MediaInput("image", "image/png", url="https://files.example/image.png")]
    if failure == "refused":
        response = await understand_response(config(), "Describe", media)
        assert response.content == "Backup completed" and response.model == "backup-model"
        assert response.usage["provider_attempts"] == [
            {"model_id": "primary", "status": "failed", "http_status": 503, "provider_code": "unavailable"},
            {"model_id": "backup", "status": "completed"},
        ]
        assert [host for host, _ in seen] == ["primary.example", "backup.example"]
        assert seen[0][1]["messages"] == seen[1][1]["messages"]
    else:
        with pytest.raises((MediaAIError, httpx.HTTPError)):
            await understand_response(config(), "Describe", media)
        assert [host for host, _ in seen] == ["primary.example"]
