"""Observable attachment projection, playback references and model discovery."""

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.files import _message_references_media_path
from app.database import async_session
from app.models.tool import AgentTool, Tool
from app.models.tenant import Tenant
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.chat_message_serializer import serialize_chat_message_for_client
from app.services.model_catalog_tool import execute_list_models
from app.services.media_ai_io import normalize_sources
from test_media_model_selection import context, make_model


def test_historical_media_inputs_use_shared_attachments_and_playback_authority():
    files = ["workspace/a.png", {"source": "workspace/b.mp4", "kind": "video"},
             {"source": "https://example.org/audio?id=1&signature=exact", "kind": "audio"},
             "workspace/a.png"]
    message = SimpleNamespace(role="user", content="Read these", id="input", created_at=None,
                              message_meta={"media_request": {"arguments": {"files": files},
                                                               "connection_ref": "private"}})
    result = serialize_chat_message_for_client(message)
    assert result["display_content"] == "Read these"
    assert [item["kind"] for item in result["attachments"]] == ["image", "video", "audio", "image"]
    assert result["attachments"][2]["path"] == files[2]["source"]
    assert "connection_ref" not in json.dumps(result)
    assert _message_references_media_path(message, "workspace/b.mp4")
    assert not _message_references_media_path(message, "workspace/unrelated.mp4")
    message.message_meta = {"media_request": {"arguments": {"files": ["../private.mp4", "javascript:alert(1)"]}}}
    assert serialize_chat_message_for_client(message)["attachments"] == []
    message.message_meta = {"media_request": {"arguments": {
        "files": normalize_sources(["data://[invalid", "workspace/a.png"]),
    }}}
    result = serialize_chat_message_for_client(message)
    assert result["display_content"] == "Read these"
    assert [item["path"] for item in result["attachments"]] == ["workspace/a.png"]
    assert _message_references_media_path(message, "workspace/a.png")


@pytest.mark.asyncio
async def test_catalog_filters_enabled_tenant_models_and_exposes_seeded_contract(context):
    first = await make_model(context, model="video-a", label="First", provider="qwen",
                             base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    second = await make_model(context, model="video-b", label="Second", provider="qwen",
                              base_url=first.base_url)
    await make_model(context, model="video-disabled", enabled=False)
    await make_model(context, model="image-only", input_modalities=["text", "image"])
    async with async_session() as db:
        other = Tenant(name="Other company", slug=uuid.uuid4().hex)
        db.add(other)
        await db.commit()
    await make_model(context, tenant_id=other.id, model="video-other", provider="qwen", base_url=first.base_url)
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "list_models"))
        db.add(AgentTool(agent_id=context.agent_id, tool_id=tool.id, enabled=True))
        await db.commit()
    context.tool_name = "list_models"
    context.arguments = {"provider": "bailian", "purpose": "media_understanding",
                         "input_modalities": ["audio", "video"], "query": "VIDEO", "limit": 1}
    result = json.loads(await execute_list_models(context))
    assert result["total"] == 2 and result["next_offset"] == 1
    assert result["models"][0]["id"] == str(first.id)
    assert "model-secret" not in json.dumps(result) and "base_url" not in json.dumps(result)
    context.arguments["offset"] = 1
    assert json.loads(await execute_list_models(context))["models"][0]["id"] == str(second.id)
    context.arguments["provider"] = "missing"
    assert json.loads(await execute_list_models(context))["models"] == []
    tools = await get_agent_tools_for_llm(context.agent_id)
    catalog = next(item["function"] for item in tools if item["function"]["name"] == "list_models")
    assert "input_modalities" in catalog["parameters"]["properties"]
    async with async_session() as db:
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == context.agent_id, AgentTool.tool_id == tool.id))
        assignment.enabled = False
        await db.commit()
    assert json.loads(await execute_list_models(context))["code"] == "toolDisabled"
