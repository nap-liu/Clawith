"""Inference options reach shared Chat/Responses transports without losing media."""

import json

import httpx
import pytest

from app.services.media_ai_io import MediaInput
from app.services.media_ai_provider import understand_response
from test_media_ai_provider import PNG, mock_http
from test_media_ai_sessions import enable_read, run_task
from test_media_model_selection import context, make_model, request_for
from app.services.media_ai_tools import execute_media_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_compatible", "openai_responses"])
async def test_understanding_parameter_overrides_reach_provider(monkeypatch, protocol):
    bodies = []
    def upstream(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body["model"] == "test-model" and "data:image/png" in json.dumps(body)
        if protocol == "openai_compatible":
            event = {"choices": [{"delta": {"content": "observed"}, "finish_reason": "stop"}]}
        else:
            event = {"type": "response.completed", "response": {"id": "r", "status": "completed",
                     "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "observed"}]}]}}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
    mock_http(monkeypatch, upstream)
    config = {"model_id": "selected", "model": "test-model", "provider": "custom",
              "base_url": "https://example.org/v1", "api_key": "private", "api_protocol": protocol,
              "temperature": 0.7, "max_output_tokens": 1000}
    media = [MediaInput("sample.png", "image/png", PNG)]
    await understand_response(config, "Describe", media)
    await understand_response(config, "Describe", media, parameters={"temperature": 0.2,
                              "max_output_tokens": 400, "top_p": 0.8, "seed": 42})
    key = "max_tokens" if protocol == "openai_compatible" else "max_output_tokens"
    assert bodies[0]["temperature"] == 0.7 and bodies[0][key] == 1000
    assert bodies[1]["temperature"] == 0.2 and bodies[1][key] == 400
    assert bodies[1]["top_p"] == 0.8 and bodies[1]["seed"] == 42
    assert config["temperature"] == 0.7


@pytest.mark.asyncio
async def test_understanding_parameters_survive_durable_queue(context, monkeypatch):
    from app.services import media_ai_runtime
    from app.services.llm.client import LLMResponse
    await enable_read(context)
    model = await make_model(context)
    context.arguments = {"model_id": str(model.id), "prompt": "Read", "files": ["sample.png"],
                         "parameters": {"temperature": 0.1, "top_p": 0.9}}
    receipt = json.loads(await execute_media_tool(context))
    assert receipt["status"] == "queued"
    assert (await request_for(receipt))["arguments"]["parameters"] == context.arguments["parameters"]
    calls = []
    async def load(*args):
        return [MediaInput("sample.png", "image/png", PNG)]
    async def understand(config, prompt, media, **kwargs):
        calls.append(kwargs)
        return LLMResponse(content="Read", finish_reason="stop")
    monkeypatch.setattr(media_ai_runtime, "load_media", load)
    monkeypatch.setattr(media_ai_runtime, "understand_response", understand)
    rows = await run_task(receipt)
    assert rows[-1].message_meta["media_result"]["status"] == "completed"
    assert calls[0]["parameters"] == context.arguments["parameters"]
