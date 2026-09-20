"""Standard tool content survives transport, durable replay and provider adaptation."""

import base64
import copy
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.services import tool_results
from app.services.chat_history import persist_tool_call_row, expand_tool_call_round
from app.services.llm.client_shared import LLMMessage
from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.caller_streaming_support import _assemble_api_messages_for_call_llm
from app.services.llm import tool_result_projection
from app.services.mcp_client import MCPClient
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient
from tests.test_tool_output_budget_persistence import durable_identity  # noqa: F401

pytestmark = pytest.mark.asyncio
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nimage fixture").decode()


@pytest.fixture(autouse=True)
async def fresh_pool():
    await engine.dispose()
    yield
    await engine.dispose()


def result():
    return {
        "content": [
            {"type": "text", "text": "capture result"},
            {"type": "image", "mimeType": "image/png", "data": PNG},
            {"type": "audio", "mimeType": "audio/wav", "data": base64.b64encode(b"audio fixture").decode()},
            {"type": "resource", "resource": {"uri": "memory:///note", "text": "resource text", "mimeType": "text/plain"}},
        ],
        "structuredContent": {"count": 2},
        "isError": True,
        "_meta": {"private": "not model content"},
    }


async def test_mcp_http_and_stdio_keep_standard_result(monkeypatch):
    payload = result()
    client = MCPClient("https://example.test/mcp")
    monkeypatch.setattr(client, "_detect_and_request", AsyncMock(return_value={"result": payload}))
    actual = await client.call_tool("capture", {})
    assert actual == payload

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"success": True, "data": payload}))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    assert await SandboxMcpHubClient("http://sandbox.test", None).call_tool("server", "capture", {}) == payload

    monkeypatch.setattr(client, "_detect_and_request", AsyncMock(return_value={"error": {"code": -32602, "message": "bad arguments"}}))
    failed = await client.call_tool("capture", {})
    assert failed["isError"] is True
    assert "bad arguments" in failed["content"][0]["text"]


async def test_persistence_round_order_replay_and_agent_isolation(durable_identity, tmp_path, monkeypatch):  # noqa: F811
    agent_id, user_id = durable_identity
    monkeypatch.setattr(tool_results.current_agent_runtime_workspace(agent_id).__class__, "local_path",
                        lambda self, path: tmp_path / path)
    files = {}
    storage = SimpleNamespace(
        exists=AsyncMock(side_effect=lambda key: key in files),
        write_bytes=AsyncMock(side_effect=lambda key, data, **kw: files.__setitem__(key, data)),
        read_bytes=AsyncMock(side_effect=lambda key: files[key] if key in files else (_ for _ in ()).throw(FileNotFoundError(key))),
    )
    monkeypatch.setattr(tool_results, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(tool_result_projection, "get_storage_backend", lambda: storage)
    session = str(uuid.uuid4())
    payload = result()
    payload["content"] = payload["content"][:2]
    durable = await tool_results.persist_tool_result(payload, agent_id=agent_id, session_id=session)
    assert "data" not in durable["content"][1]
    assert files and payload["content"][1]["data"] == PNG
    async with async_session() as db:
        for index in range(2):
            await persist_tool_call_row(db, agent_id=agent_id, user_id=user_id, conversation_id=session, evt={
                "name": "capture", "call_id": f"call_{index}", "args": {}, "status": "done",
                "round_id": "batch", "round_tool_index": index,
                "result": tool_results.tool_result_text(durable), "tool_result": durable,
            })
        await db.commit()
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == session).order_by(ChatMessage.created_at, ChatMessage.id))).all()
    canonical = await _assemble_api_messages_for_call_llm(
        expand_tool_call_round(rows), agent_id=agent_id, supports_vision=True,
        static_prompt="test", dynamic_prompt="runtime context",
    )
    untouched = copy.deepcopy(canonical)
    model = SimpleNamespace(provider="qwen", api_protocol="openai_responses", tool_result_multimodal_mode="user_message",
                            input_modalities=["text", "image"])
    projected = await tool_result_projection.project_tool_results(canonical, model=model, agent_id=agent_id)
    assert canonical == untouched
    assert [m.role for m in projected] == ["system", "assistant", "tool", "tool", "user"]
    assert sum(p["type"] == "image_url" for p in projected[-1].content) == 2
    assert "not model content" not in str([m.content for m in projected])
    wire = OpenAIResponsesClient("test", model="test")._messages_to_input(projected)
    assert [item.get("type", item.get("role")) for item in wire if item.get("role") != "assistant"] == ["system", "function_call", "function_call", "function_call_output", "function_call_output", "user"]
    assert len(rows) == 2 and all(row.role == "tool_call" for row in rows)
    assert await tool_result_projection.project_tool_results(canonical, model=model, agent_id=agent_id) == projected
    foreign = await tool_result_projection.project_tool_results(canonical, model=model, agent_id=uuid.uuid4())
    assert not any(p["type"] == "image_url" for p in foreign[-1].content)
    assert "unavailable" in str(foreign[-1].content)


async def test_audio_video_native_images_and_unsupported_resource():
    payload = result()
    payload["content"].append({"type": "resource", "resource": {
        "uri": "memory:///clip", "mimeType": "video/mp4", "blob": base64.b64encode(b"video fixture").decode(),
    }})
    messages = [
        LLMMessage(role="assistant", tool_calls=[{"id": "c", "type": "function", "function": {"name": "capture", "arguments": "{}"}}]),
        LLMMessage(role="tool", tool_call_id="c", content=tool_results.tool_result_text(payload), tool_result=payload),
    ]
    model = SimpleNamespace(provider="openai", api_protocol="openai_compatible", tool_result_multimodal_mode="user_message",
                            input_modalities=["text", "image", "audio", "video"])
    projected = await tool_result_projection.project_tool_results(messages, model=model, agent_id=None)
    assert [p["type"] for p in projected[-1].content if p["type"] != "text"] == ["image_url", "input_audio", "video_url"]
    assert projected[-1].content[4]["input_audio"]["format"] == "wav"
    model.api_protocol = "anthropic"
    model.tool_result_multimodal_mode = "native"
    projected = await tool_result_projection.project_tool_results(messages, model=model, agent_id=None)
    wire = projected[-1].to_anthropic_format()["content"][0]
    assert wire["type"] == "tool_result" and wire["is_error"] is True
    assert any(p["type"] == "image" for p in wire["content"])
    assert "audio/wav" in str(wire) and "unavailable" in str(wire)


async def test_invalid_path_is_not_resolved():
    for uri in ["agent-file:///../secret", "agent-file://other/.tool_results/x", "agent-file:///soul.md"]:
        with pytest.raises(ValueError):
            tool_results.agent_resource_path(uri)


async def test_media_persistence_rejects_foreign_agent_symlink(tmp_path, monkeypatch):
    from app.services.agent_runtime_workspace import AgentRuntimeWorkspace
    from app.services.storage_runtime.local import LocalStorageBackend

    own, foreign = tmp_path / "own", tmp_path / "foreign"
    own.mkdir()
    foreign.mkdir()
    (own / ".tool_results").symlink_to(foreign, target_is_directory=True)
    workspace = AgentRuntimeWorkspace("own", own, "own")
    monkeypatch.setattr(tool_results, "current_agent_runtime_workspace", lambda _: workspace)
    monkeypatch.setattr(tool_results, "get_storage_backend", lambda: LocalStorageBackend(str(tmp_path)))
    with pytest.raises(ValueError, match="escaped"):
        await tool_results.persist_tool_result(result(), agent_id="own", session_id="session")
    assert list(foreign.rglob("*")) == []


async def test_add_media_to_ctx_accepts_any_agentdir_path_and_projects_every_image(
    tmp_path,
    monkeypatch,
):
    from app.services.agent_runtime_workspace import AgentRuntimeWorkspace
    from app.services.image_context_tool import add_media_to_ctx
    from app.services.storage_runtime.local import LocalStorageBackend
    from app.services import image_context_tool

    own = tmp_path / "own"
    paths = ["memory/archive/first.png", "custom/nested/second.data"]
    for path in paths:
        target = own / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\nimage-" + path.encode())
    workspace = AgentRuntimeWorkspace("own", own, "own")
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(image_context_tool, "current_agent_runtime_workspace", lambda _: workspace)
    monkeypatch.setattr(image_context_tool, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(tool_results, "current_agent_runtime_workspace", lambda _: workspace)
    monkeypatch.setattr(tool_results, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(tool_result_projection, "current_agent_runtime_workspace", lambda _: workspace)
    monkeypatch.setattr(tool_result_projection, "get_storage_backend", lambda: storage)

    result_value = await add_media_to_ctx("own", paths)
    persisted = await tool_results.persist_tool_result(
        result_value,
        agent_id="own",
        session_id="session",
    )
    projected = await tool_result_projection.project_tool_results(
        [
            LLMMessage(role="assistant", tool_calls=[{
                "id": "recall",
                "type": "function",
                "function": {"name": "add_media_to_ctx", "arguments": "{}"},
            }]),
            LLMMessage(
                role="tool",
                tool_call_id="recall",
                content=tool_results.tool_result_text(persisted),
                tool_result=persisted,
            ),
        ],
        model=SimpleNamespace(
            provider="openai",
            api_protocol="openai_compatible",
            tool_result_multimodal_mode="user_message",
            supports_vision=True,
            input_modalities=["text", "image"],
        ),
        agent_id="own",
    )

    image_parts = [
        part
        for message in projected
        if isinstance(message.content, list)
        for part in message.content
        if part.get("type") == "image_url"
    ]
    assert len(image_parts) == 2
    assert persisted["structuredContent"] == {"count": 2, "files": paths}


async def test_summary_and_replay_keep_private_content_out_of_model():
    from app.services.chat_history_loading import expand_tool_call_row
    from app.services.llm.compactor_serialization import serialize_span_for_summary

    payload = result()
    payload["content"].append({"type": "text", "text": "USER_ONLY", "annotations": {"audience": ["user"]}})
    bounded = "capture result (bounded, safe text)"
    stored = json.dumps({"name": "capture", "call_id": "c", "status": "done",
                         "result": bounded, "tool_result": payload})
    row = SimpleNamespace(id=uuid.uuid4(), role="tool_call", content=stored,
                          message_meta={}, created_at=datetime.now(timezone.utc))
    for prefilter in (True, False):
        summary = serialize_span_for_summary([row], prefilter=prefilter)
        assert "not model content" not in summary and "USER_ONLY" not in summary
        assert "capture result" in summary
        assert '"count": 2' not in summary.replace('\\"', '"')
    replay = expand_tool_call_row(row)[-1]
    assert replay["content"] == bounded
    assert "USER_ONLY" not in replay["content"]
    assert "USER_ONLY" in tool_results.tool_result_text(payload, audience="user")
    assert row.content == stored and replay["tool_result"] == payload


@pytest.mark.parametrize("protocol", ["openai_compatible", "openai_responses"])
async def test_projection_resource_failures_capabilities_and_cache_prefix(protocol, monkeypatch):
    model = SimpleNamespace(provider="openai", api_protocol=protocol, tool_result_multimodal_mode="user_message",
                            supports_vision=True, input_modalities=["text", "image"])
    def round_messages(call_id, blocks):
        return [
            LLMMessage(role="assistant", tool_calls=[{"id": call_id, "type": "function",
                "function": {"name": "capture", "arguments": "{}"}}]),
            LLMMessage(role="tool", tool_call_id=call_id, content="stable result", tool_result={"content": blocks}),
        ]
    blocks = [result()["content"][1], {"type": "resource_link", "name": "blocked",
              "mimeType": "image/png", "uri": "http://127.0.0.1/private.png"}]
    messages = [LLMMessage(role="system", content="stable system"), *round_messages("first", blocks)]
    if protocol == "openai_responses":
        messages[1].responses_snapshot = {"protocol": protocol, "endpoint": "https://api.openai.com/v1",
            "model": "test", "output": [
                {"type": "reasoning", "id": "rs_first", "encrypted_content": "stable-reasoning", "summary": []},
                {"type": "function_call", "call_id": "first", "name": "capture", "arguments": "{}"},
            ]}
    original = copy.deepcopy(messages)
    first = await tool_result_projection.project_tool_results(messages, model=model, agent_id=None)
    assert any(p["type"] == "image_url" for p in first[-1].content)
    assert "MEDIA_URL_FORBIDDEN_TARGET" in str(first[-1].content)
    later = await tool_result_projection.project_tool_results(
        [*messages, *round_messages("second", blocks[:1])], model=model, agent_id=None,
    )
    client = OpenAIResponsesClient("test", model="test")
    wire = client._messages_to_input if protocol == "openai_responses" else lambda ms: [m.to_openai_format() for m in ms]
    first_wire, later_wire = wire(first), wire(later)
    assert json.dumps(first_wire, ensure_ascii=False) == json.dumps(later_wire[:len(first_wire)], ensure_ascii=False)
    assert messages == original
    from app.services.chat_history_loading import expand_tool_call_row

    stored_row = SimpleNamespace(id=uuid.uuid4(), role="tool_call", created_at=datetime.now(timezone.utc),
        content=json.dumps({"name": "capture", "call_id": "first", "args": {}, "status": "done",
            "result": messages[2].content, "tool_result": messages[2].tool_result}),
        message_meta={"responses_snapshot": messages[1].responses_snapshot} if messages[1].responses_snapshot else {})
    reloaded = await _assemble_api_messages_for_call_llm(
        expand_tool_call_row(stored_row), agent_id=None, supports_vision=True,
        static_prompt="stable system", dynamic_prompt=None,
    )
    replayed = await tool_result_projection.project_tool_results(reloaded, model=model, agent_id=None)
    assert json.dumps(first_wire, ensure_ascii=False) == json.dumps(wire(replayed), ensure_ascii=False)
    if protocol == "openai_responses":
        assert any(item.get("encrypted_content") == "stable-reasoning" for item in first_wire)
    model.supports_vision = False
    model.input_modalities = ["text"]
    reader = AsyncMock(side_effect=AssertionError("unsupported media must not be read"))
    monkeypatch.setattr(tool_result_projection, "_resource_bytes", reader)
    filtered = await tool_result_projection.project_tool_results(messages, model=model, agent_id=None)
    assert not any(p["type"] == "image_url" for p in filtered[-1].content)
    assert "does not support image" in str(filtered[-1].content)
    reader.assert_not_called()


async def test_backend_normalizes_live_and_history_display_identically():
    from app.services.chat_history_context import parse_tool_call_for_display
    from app.services.tool_result_display import tool_event_for_display

    payload = result()
    payload["content"].extend([
        {"type": "resource_link", "uri": "agent-file:///.tool_results/s/clip.webm", "name": "clip.webm", "mimeType": "video/webm"},
        {"type": "resource_link", "uri": "agent-file:///../secret", "name": "secret"},
        {"type": "text", "text": "ASSISTANT_ONLY", "annotations": {"audience": ["assistant"]}},
    ])
    evt = {"name": "capture", "status": "done", "result": "model view", "tool_result": payload,
           "_durable_message_id": str(uuid.uuid4())}
    original = copy.deepcopy(evt)
    live = tool_event_for_display(evt)
    history = parse_tool_call_for_display(json.dumps(evt))
    assert live["toolResultContent"] == history["toolResultContent"]
    assert "tool_result" not in live and "not model content" not in str(live)
    assert "ASSISTANT_ONLY" not in str(live)
    content = live["toolResultContent"]["content"]
    attachments = [block["attachment"] for block in content if block["type"] == "attachment"]
    assert [item["kind"] for item in attachments] == ["image", "audio", "video"]
    assert attachments[-1]["path"] == ".tool_results/s/clip.webm"
    assert {"type": "unavailable"} in content
    assert live["persistedMessageId"] == evt["_durable_message_id"] and evt == original
