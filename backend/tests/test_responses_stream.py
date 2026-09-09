"""Observable HTTP streaming and native Responses replay contracts."""

import json

import httpx
import pytest

from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.client_shared import LLMError, LLMMessage

OUTPUT = [
    {"id": "reason-1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
    {"id": "message-1", "type": "message", "role": "assistant", "phase": "commentary",
     "status": "completed", "content": [{"type": "output_text", "text": "Checking", "annotations": []}]},
    {"id": "function-1", "type": "function_call", "call_id": "call-1",
     "name": "read_file", "arguments": '{"path":"a"}', "status": "completed"},
]


def response_data(output=None):
    return {"id": "resp-1", "status": "completed", "model": "model-a",
            "output": OUTPUT if output is None else output,
            "usage": {"input_tokens": 20, "output_tokens": 5}}


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events, before_terminal=None):
        self.events = events
        self.before_terminal = before_terminal

    async def __aiter__(self):
        for event in self.events:
            if event.get("type") == "response.completed" and self.before_terminal:
                self.before_terminal()
            encoded = ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
            for offset in range(0, len(encoded), 7):
                yield encoded[offset:offset + 7]


@pytest.mark.asyncio
async def test_sse_delivers_text_and_tool_arguments_before_terminal():
    chunks, calls, payloads = [], [], []

    async def chunk(value):
        chunks.append(value)

    async def tool(value):
        calls.append(value)

    def before_terminal():
        assert chunks == ["Check", "ing"]
        assert calls[-1]["arguments"] == '{"path":"a"}'

    events = [
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": "Check"},
        {"type": "response.output_text.delta", "delta": "ing"},
        {"type": "response.output_item.added", "output_index": 2,
         "item": {**OUTPUT[2], "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"path":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"a"}'},
        {"type": "response.completed", "response": response_data()},
    ]

    async def handler(request):
        payloads.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, stream=EventStream(events, before_terminal))

    client = OpenAIResponsesClient("test", model="model-a")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.stream([LLMMessage("user", "read")], on_chunk=chunk, on_tool_delta=tool)
        assert result.content == "Checking"
        assert result.tool_calls[0]["id"] == "call-1"
        assert result.usage["input_tokens"] == 20
        assert result.responses_snapshot["output"] == OUTPUT
        assert payloads[0]["stream"] is True
        assert payloads[0]["store"] is False
        assert payloads[0]["include"] == ["reasoning.encrypted_content"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "failed", "incomplete", "cancelled"])
async def test_noncompleted_stream_never_returns_executable_tools(terminal):
    events = [{"type": "response.output_item.added", "output_index": 0, "item": OUTPUT[2]}]
    if terminal:
        events.append({"type": f"response.{terminal}", "response": {
            **response_data(), "status": terminal,
        }})
    client = OpenAIResponsesClient("test", model="model-a")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=EventStream(events)),
    ))
    try:
        with pytest.raises(LLMError):
            await client.stream([LLMMessage("user", "read")])
    finally:
        await client.close()


def test_native_replay_keeps_phase_reasoning_and_unique_tool_pair():
    client = OpenAIResponsesClient("test", model="model-a")
    result = client._parse_response_data(response_data())
    messages = [LLMMessage("assistant", result.content, tool_calls=result.tool_calls,
                           responses_snapshot=result.responses_snapshot),
                LLMMessage("tool", "file text", tool_call_id="call-1")]
    items = client._messages_to_input(messages)
    assert items[:3] == OUTPUT
    assert items[3] == {"type": "function_call_output", "call_id": "call-1", "output": "file text"}
    assert len(items) == 4
    items[0]["encrypted_content"] = "changed"
    assert result.responses_snapshot["output"][0]["encrypted_content"] == "opaque"
    for alternate in [OpenAIResponsesClient("test", model="model-b"),
                      OpenAIResponsesClient("test", model="model-a", base_url="https://other.example/v1")]:
        converted = alternate._messages_to_input(messages)
        assert not any(x.get("type") == "reasoning" for x in converted)
        assert len([x for x in converted if x.get("type") == "function_call"]) == 1


def test_optional_tool_parameters_remain_optional():
    client = OpenAIResponsesClient("test", model="model-a")
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}
    converted = client._convert_tools([{"type": "function", "function": {
        "name": "read_file", "parameters": schema,
    }}])
    assert converted[0]["strict"] is False
    assert converted[0]["parameters"] == schema


def test_native_replay_honors_shared_argument_repair_without_mutating_audit():
    client = OpenAIResponsesClient("test", model="model-a")
    output = [{**OUTPUT[2], "arguments": '{"path":"a",}'}]
    response = client._parse_response_data(response_data(output))
    response.tool_calls[0]["function"]["arguments"] = '{"path":"a"}'
    items = client._messages_to_input([
        LLMMessage("assistant", tool_calls=response.tool_calls, responses_snapshot=response.responses_snapshot),
        LLMMessage("tool", "text", tool_call_id="call-1"),
    ])
    assert items[0]["arguments"] == '{"path":"a"}'
    assert response.responses_snapshot["output"][0]["arguments"] == '{"path":"a",}'


@pytest.mark.asyncio
async def test_provider_refusal_is_returned_as_visible_text():
    client = OpenAIResponsesClient("test", model="model-a")
    output = [{**OUTPUT[1], "content": [{"type": "refusal", "refusal": "Cannot fulfill this request."}]}]
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response_data(output)),
    ))
    try:
        response = await client.complete([LLMMessage("user", "request")])
        assert response.content == "Cannot fulfill this request."
        assert response.tool_calls == []
    finally:
        await client.close()
