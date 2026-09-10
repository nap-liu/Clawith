"""Exercise provider requests and reasoning-history continuity through HTTP."""

import json

import httpx
import pytest

from app.services.llm.client_openai_compatible import OpenAICompatibleClient
from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.client_shared import LLMMessage
from app.services.llm.reasoning import ReasoningConfigurationError


ENDPOINTS = [
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
]
TOOLS = [{"type": "function", "function": {
    "name": "lookup", "parameters": {"type": "object", "properties": {}},
}}]
MODELS = ["ZHIPU/GLM-5.3-Flash", "MiniMax/MiniMax-M3", "stepfun/step-3.7-flash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("model", MODELS)
async def test_provider_defaults_and_reasoning_history_survive_tool_turn(endpoint, model):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path.endswith("/chat/completions")
        if len(requests) == 1:
            message = {"content": "", "reasoning_content": "Need lookup.", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }]}
        else:
            assert payload["messages"][1]["reasoning_content"] == "Need lookup."
            message = {"content": "Found it."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    client = OpenAICompatibleClient("unused", endpoint, model, provider="qwen")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        history = [LLMMessage(role="user", content="Look it up.")]
        first = await client.complete(history, TOOLS, reasoning_effort=None)
        history.extend([
            LLMMessage(role="assistant", content=first.content, tool_calls=first.tool_calls,
                       reasoning_content=first.reasoning_content),
            LLMMessage(role="tool", tool_call_id="call_1", content="Found it."),
        ])
        result = await client.complete(history, TOOLS, reasoning_effort=None)
    assert result.content == "Found it."
    for payload in requests:
        assert not {"temperature", "thinking", "enable_thinking", "reasoning_effort"} & payload.keys()
        if model.startswith("stepfun/"):
            assert "tool_choice" not in payload
            assert "parallel_tool_calls" not in payload
        else:
            assert payload["tool_choice"] == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "effort", "options"), [
    (MODELS[0], "medium", {"reasoning_effort": "high"}),
    (MODELS[1], "none", {"thinking": {"type": "disabled"}}),
    (MODELS[1], "high", {"thinking": {"type": "adaptive"}}),
    (MODELS[2], "none", {"enable_thinking": False}),
    (MODELS[2], "max", {"enable_thinking": True, "reasoning_effort": "high"}),
])
async def test_explicit_effort_uses_the_documented_provider_fields(model, effort, options):
    def handle(request):
        payload = json.loads(request.content)
        actual = {key: payload[key] for key in ("thinking", "enable_thinking", "reasoning_effort")
                  if key in payload}
        assert actual == options
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    client = OpenAICompatibleClient("unused", ENDPOINTS[1], model, provider="qwen")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await client.complete([LLMMessage(role="user", content="OK")], reasoning_effort=effort)
    assert result.content == "OK"


def test_bailian_differences_do_not_apply_to_other_endpoints():
    other = "https://tokenhub.tencentmaas.com/v1"
    client = OpenAICompatibleClient("unused", other, MODELS[2], provider="tokenhub")
    payload = client._build_payload([LLMMessage(role="user", content="OK")], TOOLS, None, None)
    assert payload["tool_choice"] == "auto"
    client = OpenAICompatibleClient("unused", other, MODELS[1], provider="tokenhub")
    with pytest.raises(ReasoningConfigurationError):
        client._build_payload([], None, None, None, reasoning_effort="none")


def test_fixed_sampling_does_not_silently_ignore_an_override():
    client = OpenAICompatibleClient("unused", ENDPOINTS[0], MODELS[1], provider="qwen")
    assert client._build_payload([], None, 1.0, None)["temperature"] == 1.0
    with pytest.raises(ValueError, match="temperature"):
        client._build_payload([], None, 0.2, None)
    with pytest.raises(ReasoningConfigurationError):
        OpenAICompatibleClient("unused", ENDPOINTS[0], MODELS[0])._build_payload(
            [], None, None, None, reasoning_effort="none",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "effort", "expected"), [
    ("hy3", "none", "none"),
    ("hy3", "max", "high"),
    ("deepseek-v4-flash", "minimal", "low"),
    ("deepseek-v4-flash", "none", "none"),
    ("glm-5.3", "minimal", "low"),
    ("kimi-k3", "minimal", "max"),
])
async def test_tokenhub_responses_uses_its_model_specific_efforts(model, effort, expected):
    def handle(request):
        payload = json.loads(request.content)
        assert payload["reasoning"] == {"effort": expected}
        assert "X-DashScope-Wait-Timeout" not in request.headers
        assert not {"thinking", "enable_thinking", "reasoning_effort"} & payload.keys()
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "OK"},
        ]}]})

    client = OpenAIResponsesClient("unused", "https://tokenhub.tencentmaas.com/v1", model)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await client.complete([LLMMessage(role="user", content="OK")], reasoning_effort=effort)
    assert result.content == "OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_responses_forwards_explicit_provider_header(endpoint):
    def handle(request):
        assert request.headers["X-DashScope-Wait-Timeout"] == "30"
        assert request.headers["Authorization"] == "Bearer unused"
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "OK"},
        ]}]})

    client = OpenAIResponsesClient(
        "unused", endpoint, "qwen3.8-flash",
        extra_headers={"X-DashScope-Wait-Timeout": "30"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = transport
        result = await client.complete([LLMMessage(role="user", content="OK")])
    assert result.content == "OK"
