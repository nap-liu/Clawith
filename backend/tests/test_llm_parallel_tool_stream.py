"""Provider-boundary checks for atomic parallel tool-call rounds."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app.services.llm.caller import (
    ProviderThrottleExhausted,
    _sanitize_tool_calls_for_context,
    _stream_with_throttle_retry,
)
from app.services.llm.client import LLMError, LLMMessage
from app.services.llm.client_gemini import GeminiClient
from app.services.llm.client_openai_compatible import OpenAICompatibleClient


def _sse(*payloads: dict) -> str:
    return "".join(f"data: {json.dumps(payload)}\n\n" for payload in payloads) + "data: [DONE]\n\n"


def _client_for_sse(body: str) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient(
        api_key="test",
        base_url="https://provider.invalid/v1",
        model="test-model",
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=body))
    )
    return client


@pytest.mark.asyncio
async def test_streamed_function_round_replays_required_type_to_strict_chat_provider():
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, text=_sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "read-1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"report.txt"}'},
            }]}, "finish_reason": "tool_calls"}]}))
        call = next(message for message in payload["messages"] if message.get("tool_calls"))["tool_calls"][0]
        assert call["type"] == "function"
        assert call["id"] == "read-1"
        assert payload["messages"][-1]["tool_call_id"] == "read-1"
        return httpx.Response(200, text=_sse({"choices": [{
            "delta": {"content": "Read successfully."}, "finish_reason": "stop",
        }]}))

    client = OpenAICompatibleClient("test", base_url="https://provider.invalid/v1", model="test-model")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await client.stream([LLMMessage("user", "Read the report")])
        final = await client.stream([
            LLMMessage("user", "Read the report"),
            LLMMessage("assistant", tool_calls=response.tool_calls),
            LLMMessage("tool", "Report text", tool_call_id="read-1"),
        ])
        assert final.content == "Read successfully."
        assert len(requests) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_one_sse_event_preserves_every_parallel_tool_delta():
    body = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-a",
                                "function": {"name": "first", "arguments": '{"value":1}'},
                            },
                            {
                                "index": 1,
                                "id": "call-b",
                                "function": {"name": "second", "arguments": '{"value":2}'},
                            },
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    client = _client_for_sse(body)
    try:
        response = await client.stream(messages=[])
    finally:
        await client.close()

    assert [call["id"] for call in response.tool_calls] == ["call-a", "call-b"]
    assert [call["function"]["name"] for call in response.tool_calls] == ["first", "second"]
    assert [json.loads(call["function"]["arguments"]) for call in response.tool_calls] == [
        {"value": 1},
        {"value": 2},
    ]


@pytest.mark.asyncio
async def test_parallel_tool_fragments_merge_by_index_across_events():
    body = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 1, "id": "call-b", "function": {"name": "second"}},
                            {"index": 0, "id": "call-a", "function": {"name": "first"}},
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"value":1}'}},
                            {"index": 1, "function": {"arguments": '{"value":2}'}},
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    client = _client_for_sse(body)
    try:
        response = await client.stream(messages=[])
    finally:
        await client.close()

    assert [(call["id"], call["function"]["name"]) for call in response.tool_calls] == [
        ("call-a", "first"),
        ("call-b", "second"),
    ]


@pytest.mark.asyncio
async def test_gemini_preserves_identical_parallel_calls_but_ignores_repeated_snapshot():
    payload = {
        "candidates": [{
            "content": {"parts": [
                {"functionCall": {"name": "lookup", "args": {"id": 1}}},
                {"functionCall": {"name": "lookup", "args": {"id": 1}}},
            ]},
            "finishReason": "STOP",
        }]
    }
    body = _sse(payload, payload)
    client = GeminiClient(
        api_key="test",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-test",
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=body))
    )
    try:
        response = await client.stream(messages=[])
    finally:
        await client.close()

    assert len(response.tool_calls) == 2
    assert [call["function"]["name"] for call in response.tool_calls] == ["lookup", "lookup"]


@pytest.mark.asyncio
async def test_gemini_sse_resource_exhausted_uses_five_retry_lane(monkeypatch):
    request_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(request.content)
        return httpx.Response(
            200,
            text=_sse({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}),
        )

    client = GeminiClient(
        api_key="test",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-test",
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.services.llm.provider_retry._sleep_before_throttle_retry",
        AsyncMock(return_value=None),
    )
    model = SimpleNamespace(
        provider="gemini",
        model="gemini-test",
        base_url=client.base_url,
        api_key_encrypted="",
    )
    try:
        with pytest.raises(ProviderThrottleExhausted):
            await _stream_with_throttle_retry(client, model=model, round_i=1, messages=[])
    finally:
        await client.close()

    assert len(request_bodies) == 6
    assert request_bodies == [request_bodies[0]] * 6


def test_mixed_valid_and_invalid_tool_round_is_rejected_atomically():
    valid = {
        "id": "call-a",
        "type": "function",
        "function": {"name": "first", "arguments": "{}"},
    }
    invalid = {
        "id": "call-b",
        "type": "function",
        "function": {"name": "", "arguments": "{}"},
    }

    sanitized, retry_instruction = _sanitize_tool_calls_for_context([valid, invalid])

    assert sanitized is None
    assert "Retry once" in retry_instruction


@pytest.mark.parametrize(
    "tool_calls",
    [
        [
            {"id": "same", "function": {"name": "first", "arguments": "{}"}},
            {"id": "same", "function": {"name": "second", "arguments": "{}"}},
        ],
        [{"id": "call-a", "function": {"name": "first", "arguments": "[]"}}],
        [{"id": "call-a", "function": "not-an-object"}],
        [{"id": "call-a", "type": "custom", "function": {"name": "first"}}],
    ],
)
def test_malformed_tool_rounds_are_rejected(tool_calls):
    sanitized, retry_instruction = _sanitize_tool_calls_for_context(tool_calls)

    assert sanitized is None
    assert retry_instruction


@pytest.mark.asyncio
async def test_conflicting_tool_id_fragments_are_a_protocol_error():
    body = _sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-a"}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-b"}]}}]},
    )
    client = _client_for_sse(body)
    try:
        with pytest.raises(LLMError) as exc_info:
            await client.stream(messages=[])
    finally:
        await client.close()

    assert exc_info.value.error_code == "invalid_tool_call_stream"


def test_duplicate_indices_in_one_event_are_a_protocol_error():
    client = _client_for_sse("")
    line = "data: " + json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "first"}},
                            {"index": 0, "function": {"name": "second"}},
                        ]
                    }
                }
            ]
        }
    )

    with pytest.raises(LLMError) as exc_info:
        client._parse_stream_line(line, False, "")

    assert exc_info.value.error_code == "invalid_tool_call_stream"
