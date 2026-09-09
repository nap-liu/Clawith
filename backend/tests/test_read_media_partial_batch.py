"""Batch admission, worker HTTP and follow-up keep original input identity."""

import base64
import json
import uuid

import pytest

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.services import media_ai_tools
from app.services.tool_seeder import seed_builtin_tools
from execution_provider_fixture import provider
from test_media_ai_sessions import run_task
from test_media_ai_provider import PNG
from test_read_image_handler import legacy
import test_media_ai_runtime as runtime_tests

context = runtime_tests.context
pytestmark = pytest.mark.asyncio


def image_blocks(request):
    return [part for message in request["messages"] if isinstance(message.get("content"), list)
            for part in message["content"] if part["type"] == "image_url"]


def labels(request):
    return [part["text"] for message in request["messages"] if isinstance(message.get("content"), list)
            for part in message["content"] if part["type"] == "text"]


async def test_partial_batch_is_admitted_and_original_indices_survive_followup(context):
    _, model_id = await legacy(context)
    await seed_builtin_tools()
    valid = "data:image/png;base64," + base64.b64encode(PNG).decode()
    broken = "data:image/png;base64,not-valid-base64"
    context.tool_name = "read_media"
    async with provider([{"content": "Input 1 and Input 3 are readable."}]) as (url, requests):
        async with async_session() as db:
            model = await db.get(LLMModel, model_id)
            model.base_url, model.api_protocol = url, "openai_compatible"
            await db.commit()
        context.arguments = {"model_id": str(model_id), "prompt": "Describe all three inputs by number",
                             "files": [valid, broken, valid]}
        receipt = json.loads(await media_ai_tools.execute_media_tool(context))
        assert receipt["status"] == "queued" and requests == []
        rows = await run_task(receipt)
        result = rows[-1].message_meta["media_result"]
        assert result["status"] == "completed"
        assert result["input_errors"] == [{"index": 2, "source": broken, "code": "invalidMedia"}]
        assert len(requests) == 1 and len(image_blocks(requests[0])) == 2
        texts = labels(requests[0])
        assert any(text.startswith("Input 1") for text in texts)
        assert any(text.startswith("Input 3") for text in texts)
        assert any("Input 2" in text and "unavailable" in text.lower() for text in texts)
        assert all(valid not in text and broken not in text for text in texts)
        async with async_session() as db:
            anchor = await db.get(ChatMessage, uuid.UUID(receipt["task_id"]))
        assert [item["source"] for item in anchor.message_meta["media_request"]["arguments"]["files"]] == [valid, broken, valid]

        context.tool_call_id = "followup-" + uuid.uuid4().hex
        context.arguments = {"session_id": receipt["session_id"], "prompt": "What is in Input 3?"}
        followup = json.loads(await media_ai_tools.execute_media_tool(context))
        rows = await run_task(followup)
        assert rows[-1].message_meta["media_result"]["status"] == "completed"
        assert len(requests) == 2 and len(image_blocks(requests[1])) == 2
        assert any(text.startswith("Input 3") for text in labels(requests[1]))
        assert any("Input 2" in text and "unavailable" in text.lower() for text in labels(requests[1]))

        context.tool_call_id = "all-bad-" + uuid.uuid4().hex
        context.arguments = {"model_id": str(model_id), "prompt": "Read the inputs", "files": [broken, broken]}
        failed_receipt = json.loads(await media_ai_tools.execute_media_tool(context))
        assert failed_receipt["status"] == "queued"
        failed = await run_task(failed_receipt)
        assert failed[-1].message_meta["media_result"]["status"] == "failed"
        assert len(requests) == 2
