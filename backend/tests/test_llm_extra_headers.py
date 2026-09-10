"""Configured model headers reach every transport without a second policy."""

import httpx
import pytest

from app.services.llm.client_anthropic import AnthropicClient
from app.services.llm.client_gemini import GeminiClient
from app.services.llm.client_openai_compatible import OpenAICompatibleClient
from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.client_registry import create_llm_client
from app.services.llm.client_shared import LLMMessage


@pytest.mark.asyncio
@pytest.mark.parametrize(("client_type", "auth_header"), [
    (OpenAICompatibleClient, "authorization"),
    (OpenAIResponsesClient, "authorization"),
    (AnthropicClient, "x-api-key"),
    (GeminiClient, "x-goog-api-key"),
])
async def test_headers_override_case_insensitively_and_are_snapshotted(client_type, auth_header):
    configured = {auth_header.upper(): "configured", "X-Request-Route": "fast"}
    client = client_type("base", "https://gateway.example/v1", "test", extra_headers=configured)
    configured["X-Request-Route"] = "mutated"
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers[auth_header] == "configured"
        assert len(request.headers.get_list(auth_header)) == 1
        assert request.headers["X-Request-Route"] == "fast"
        assert request.headers["Content-Type"] == "application/json"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "content": [{"type": "text", "text": "OK"}],
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await client.complete([LLMMessage(role="user", content="OK")])
    assert result.content == "OK"
    assert len(requests) == 1
    with pytest.raises(TypeError):
        client.extra_headers["X-Request-Route"] = "changed"


@pytest.mark.asyncio
async def test_gemini_legacy_transport_keeps_configured_headers():
    client = GeminiClient(
        "base", "https://gateway.example/openai", "test",
        extra_headers={"X-Request-Route": "legacy"},
    )
    fallback = await client._get_openai_fallback_client()
    assert fallback._get_headers()["x-request-route"] == "legacy"
    assert fallback._get_headers()["authorization"] == "Bearer base"


@pytest.mark.parametrize("protocol", ["openai_compatible", "openai_responses", "anthropic", "gemini"])
def test_default_headers_can_be_overridden_or_cleared_without_changing_timeouts(protocol):
    def create(headers=None):
        return create_llm_client(
            "qwen", "base", "test", api_protocol=protocol,
            extra_headers=headers, timeout=47,
        )

    inherited = create()
    assert inherited._get_headers()["x-dashscope-wait-timeout"] == "120"
    changed = create({"X-DashScope-Wait-Timeout": "5"})
    assert changed._get_headers()["x-dashscope-wait-timeout"] == "5"
    cleared = create({})
    assert "x-dashscope-wait-timeout" not in cleared._get_headers()
    assert inherited.timeout == changed.timeout == cleared.timeout == 47
    other = create_llm_client("openai", "base", "test")
    assert "x-dashscope-wait-timeout" not in other._get_headers()


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", [
    OpenAICompatibleClient, OpenAIResponsesClient, AnthropicClient, GeminiClient,
])
async def test_redirects_keep_extra_credentials_only_on_the_configured_origin(client_type, monkeypatch):
    visited = []

    def handle(request):
        visited.append(request)
        if len(visited) == 1:
            assert request.headers["x-custom-token"] == "private"
            return httpx.Response(307, headers={"Location": "/second"})
        if len(visited) == 2:
            assert request.headers["x-custom-token"] == "private"
            return httpx.Response(307, headers={"Location": "https://other.example/target"})
        assert "x-custom-token" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, json={})

    original = httpx.AsyncClient

    def build_client(**kwargs):
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    client = client_type(
        "base", "https://origin.example/v1", "test",
        extra_headers={"X-Custom-Token": "private", "aUtHoRiZaTiOn": "Bearer configured"},
    )
    transport = await client._get_client()
    try:
        response = await transport.post("https://origin.example/first", headers=client._get_headers())
        assert response.status_code == 200
        assert len(visited) == 3
    finally:
        await client.close()
