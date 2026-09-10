"""Media selection matches actual inputs without hidden explicit-model substitution."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.security import decrypt_data
from app.database import async_session
from app.models.subagent_run import SubagentRun
from app.services import media_ai_context, media_ai_runtime
from app.services.llm.client import LLMResponse
from app.services.media_ai_io import MediaInput
from app.services.media_ai_tools import execute_media_tool
import test_media_ai_runtime as runtime_tests
from test_media_ai_sessions import enable_read, run_task
from test_media_model_selection import make_model, request_for

context = runtime_tests.context
pytestmark = pytest.mark.asyncio


async def submit(state, **arguments):
    state.tool_call_id = uuid.uuid4().hex
    state.arguments = {"prompt": "Describe this media", **arguments}
    return json.loads(await execute_media_tool(state))


async def test_default_matches_entire_batch_but_explicit_model_is_preserved(context):
    combined = await make_model(context, model="video-and-audio", input_modalities=["text", "video", "audio"])
    await make_model(context, model="a-video-only", input_modalities=["text", "video"])
    images = await make_model(context, model="image-default", input_modalities=["text", "image"])
    await enable_read(context)
    files = ["https://media.example/video.mp4", {"source": "https://media.example/opaque?signature=original", "kind": "audio"}]
    receipt = await submit(context, files=files)
    assert receipt["status"] == "queued"
    frozen = await request_for(receipt)
    assert frozen["config"]["model_id"] == str(combined.id)
    assert frozen["arguments"]["files"][1]["source"] == files[1]["source"]
    explicit = await submit(context, files=files, model_id=str(images.id))
    assert explicit["status"] == "queued"
    assert (await request_for(explicit))["config"]["model_id"] == str(images.id)
    assert (await submit(context))["code"] == "inputCombination"
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(SubagentRun).where(
            SubagentRun.parent_session_id == uuid.UUID(context.session_id),
        )) == 2


async def test_opaque_url_routes_on_loaded_mime_before_compaction_and_freezes_followup(context, monkeypatch):
    video = await make_model(context, model="video-model", input_modalities=["text", "video"])
    image = await make_model(context, model="image-default", input_modalities=["text", "image"])
    await enable_read(context)
    compact_models, provider_models = [], []

    async def load(agent_id, files):
        return [MediaInput(item["source"] if isinstance(item, dict) else item, "video/mp4",
                           url=item["source"] if isinstance(item, dict) else item) for item in files]

    async def compact(**kwargs):
        compact_models.append(kwargs["model"].id)

    async def understand(config, prompt, media, *, history):
        provider_models.append(config["model_id"])
        assert config["model_id"] == str(video.id)
        assert media or history
        return LLMResponse(content="A turtle swimming", model=config["model"])

    monkeypatch.setattr(media_ai_runtime, "load_media", load)
    monkeypatch.setattr(media_ai_context, "load_media", load)
    monkeypatch.setattr(media_ai_context, "maybe_compact", compact)
    monkeypatch.setattr(media_ai_runtime, "understand_response", understand)
    receipt = await submit(context, files=["https://media.example/opaque?signature=original"])
    assert (await request_for(receipt))["config"]["model_id"] == str(image.id)
    results = await run_task(receipt)
    assert results[-1].message_meta["media_result"]["status"] == "completed"
    assert compact_models == [video.id] and provider_models == [str(video.id)]
    request = await request_for(receipt)
    assert request["config"]["model_id"] == str(video.id)
    assert "model-secret" not in json.dumps(request)
    frozen = json.loads(decrypt_data(request["connection_ref"], get_settings().SECRET_KEY))
    assert frozen["runtime_model"]["id"] == str(video.id)
    followup = await submit(context, session_id=receipt["session_id"])
    assert (await request_for(followup))["config"]["model_id"] == str(video.id)
    assert (await run_task(followup))[-1].message_meta["media_result"]["status"] == "completed"
    assert compact_models == [video.id, video.id] and provider_models == [str(video.id), str(video.id)]


@pytest.mark.parametrize("explicit,provider_fails", [(True, False), (False, False), (True, True)])
async def test_stale_metadata_reaches_provider_and_reports_actual_outcome(context, monkeypatch, explicit, provider_fails):
    from app.services import media_ai_provider
    from app.services.llm.client import LLMError

    if explicit:
        await make_model(context, model="video-model", input_modalities=["text", "video"])
    image = await make_model(context, model="image-default", input_modalities=["text", "image"])
    await enable_read(context)
    calls = []

    async def load(agent_id, files):
        return [MediaInput("https://media.example/opaque", "video/mp4", url="https://media.example/opaque")]

    async def compact(**kwargs):
        assert kwargs["model"].id == image.id

    class Client:
        async def stream(self, **kwargs):
            content = kwargs["messages"][-1].content
            assert any(part.get("type") == "video_url" for part in content)
            calls.append(content)
            if provider_fails:
                raise LLMError("Provider rejects video", status_code=400, error_code="unsupported_video")
            return LLMResponse(content="Provider accepted the video")

        async def close(self):
            pass

    monkeypatch.setattr(media_ai_runtime, "load_media", load)
    monkeypatch.setattr(media_ai_context, "maybe_compact", compact)
    monkeypatch.setattr(media_ai_provider, "create_llm_client", lambda **kwargs: Client())
    arguments = {"model_id": str(image.id)} if explicit else {}
    receipt = await submit(context, files=["https://media.example/opaque"], **arguments)
    result = (await run_task(receipt))[-1].message_meta["media_result"]
    assert len(calls) == 1
    assert result["status"] == ("failed" if provider_fails else "completed")
    if provider_fails:
        assert result["code"] == "providerFailed" and result["provider_code"] == "unsupported_video"
    assert (await request_for(receipt))["config"]["model_id"] == str(image.id)


async def test_fallback_metadata_does_not_block_configured_provider(context):
    from app.services.media_model_inputs import history_modalities, resolve_loaded_inputs
    from app.services.media_model_selection import model_connection

    video = await make_model(context, model="video-model", input_modalities=["text", "video"])
    image = await make_model(context, model="image-model", input_modalities=["text", "image"])
    config = {**model_connection(video), "fallback_connection": model_connection(image)}
    history = [{"role": "user", "content": [{"type": "video_url", "video_url": {"url": "https://media.example/opaque"}}]}]
    resolved = await resolve_loaded_inputs(config, context.tenant_id, history_modalities(history))
    assert resolved["model_id"] == str(video.id)
    assert resolved is config
    assert resolved["fallback_connection"]["model_id"] == str(image.id)
    stale = {**model_connection(image), "fallback_connection": model_connection(image)}
    automatic = await resolve_loaded_inputs(stale, context.tenant_id, history_modalities(history))
    assert automatic["model_id"] == str(video.id)
    assert automatic["fallback_connection"]["model_id"] == str(image.id)
