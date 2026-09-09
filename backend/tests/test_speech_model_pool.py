"""Speech selection/migration and the real browser PCM WebSocket contract."""

import asyncio
import io
import json
import socket
import uuid
import wave
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select

from app.api import speech, speech_config
from app.api.enterprise_routes_model_defaults import MediaModelDefaults, update_media_model_defaults
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.speech_recognition_config import SpeechRecognitionConfig
from app.models.user import User
from app.services.speech_model_selection import SpeechCredentialUnavailable, migrate_speech_config
from app.services.speech_recognition import resolve_speech_credentials
from app.services.tool_config import get_tenant_tool_config
from tests.test_turn_recovery import _make_agent_with_model

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolated_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def legacy_config(*, enabled=True):
    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        tenant_id = agent.tenant_id
        db.add(SpeechRecognitionConfig(tenant_id=tenant_id, api_key_encrypted="legacy-key", enabled=enabled))
        await db.commit()
    return tenant_id, user_id


async def test_legacy_migration_is_idempotent_and_disable_delete_do_not_resurrect():
    tenant, _ = await legacy_config(enabled=False)
    async with async_session() as db:
        assert await migrate_speech_config(db, tenant)
        await db.commit()
        config = await get_tenant_tool_config(db, tenant, "media_ai")
        model_id = uuid.UUID(config["speech_model_id"])
        model = await db.get(LLMModel, model_id)
        assert model.enabled is False and model.purposes == ["speech_recognition"]
        with pytest.raises(SpeechCredentialUnavailable):
            await resolve_speech_credentials(db, tenant)
        model.enabled = True
        await db.commit()
        credentials = await resolve_speech_credentials(db, tenant)
        assert credentials.api_key == "legacy-key" and credentials.transport == "dashscope"
        assert await migrate_speech_config(db, tenant) is False
        await db.delete(model)
        await db.commit()
        with pytest.raises(SpeechCredentialUnavailable):
            await resolve_speech_credentials(db, tenant)
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(
            LLMModel.id == model_id)) == 0


async def test_first_explicit_clear_prevents_legacy_default_import_and_old_put_is_gone():
    tenant, user_id = await legacy_config()
    async with async_session() as db:
        user = await db.get(User, user_id)
        response = await update_media_model_defaults(
            MediaModelDefaults(speech_model_id=None), current_user=user, tenant_id=tenant, db=db,
        )
        assert response["speech_model_id"] is None
        assert await migrate_speech_config(db, tenant) is False
        with pytest.raises(SpeechCredentialUnavailable):
            await resolve_speech_credentials(db, tenant)
        with pytest.raises(HTTPException) as error:
            await speech_config.update_speech_config(user)
        assert error.value.status_code == 410


async def test_other_tenant_model_cannot_be_selected_or_connection_tested():
    tenant, user_id = await legacy_config()
    other_tenant, _ = await legacy_config()
    async with async_session() as db:
        await migrate_speech_config(db, other_tenant)
        other = await get_tenant_tool_config(db, other_tenant, "media_ai")
        model_id = uuid.UUID(other["speech_model_id"])
        await db.commit()
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as error:
            await update_media_model_defaults(
                MediaModelDefaults(speech_model_id=model_id), current_user=user, tenant_id=tenant, db=db,
            )
        assert error.value.status_code == 422
        with pytest.raises(HTTPException) as error:
            await speech_config.test_speech_config(tenant_id=tenant, model_id=model_id, current_user=user, db=db)
        assert error.value.status_code == 422


@asynccontextmanager
async def serve_app(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        yield port
    finally:
        server.should_exit = True
        await task
        listener.close()


@pytest.mark.parametrize("transport", ["dashscope", "openai"])
async def test_browser_ws_uses_enterprise_model_and_normalized_results(transport, monkeypatch):
    tenant, user_id = await legacy_config()
    received = []
    provider_app = FastAPI()

    @provider_app.post("/v1/audio/transcriptions")
    async def transcribe(request: Request):
        form = await request.form()
        assert request.headers["authorization"] == "Bearer pool-key"
        assert form["model"] == "custom-speech-model"
        data = await form["file"].read()
        with wave.open(io.BytesIO(data)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            received.append(wav.readframes(wav.getnframes()))
        return {"text": "企业语音识别"}

    async def native(upstream):
        request = json.loads(await upstream.recv())
        assert request["payload"]["model"] == "fun-asr-realtime"
        assert upstream.request.headers["authorization"] == "Bearer pool-key"
        await upstream.send(json.dumps({"header": {"event": "task-started"}}))
        received.append(await upstream.recv())
        assert json.loads(await upstream.recv())["header"]["action"] == "finish-task"
        await upstream.send(json.dumps({"header": {"event": "result-generated"}, "payload": {
            "output": {"sentence": {"text": "企业语音识别", "sentence_id": 0, "sentence_end": True}},
        }}))
        await upstream.send(json.dumps({"header": {"event": "task-finished"}}))

    async with serve_app(provider_app) as http_port, websockets.serve(native, "127.0.0.1", 0) as ws_server:
        endpoint = (f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}" if transport == "dashscope"
                    else f"http://127.0.0.1:{http_port}/v1")
        async with async_session() as db:
            model = LLMModel(tenant_id=tenant, provider="qwen" if transport == "dashscope" else "custom",
                             model="fun-asr-realtime" if transport == "dashscope" else "custom-speech-model",
                             label="Speech", api_key_encrypted="pool-key", base_url=endpoint,
                             purposes=["speech_recognition"], input_modalities=["audio"], enabled=True)
            db.add(model)
            await db.flush()
            user = await db.get(User, user_id)
            await update_media_model_defaults(MediaModelDefaults(speech_model_id=model.id),
                                              current_user=user, tenant_id=tenant, db=db)
        monkeypatch.setattr(speech, "_consume_speech_ticket", AsyncMock(return_value=user_id))
        browser_app = FastAPI()
        browser_app.include_router(speech.router)
        async with serve_app(browser_app) as browser_port:
            async with websockets.connect(f"ws://127.0.0.1:{browser_port}/ws/speech") as browser:
                await browser.send(json.dumps({"type": "authenticate", "ticket": "test-ticket"}))
                assert json.loads(await browser.recv())["type"] == "ready"
                pcm = b"\x01\x00" * 1600
                await browser.send(pcm)
                await browser.send(json.dumps({"type": "stop"}))
                final = json.loads(await browser.recv())
                completed = json.loads(await browser.recv())
                assert final["type"] == "final" and final["text"] == "企业语音识别"
                assert completed == {"type": "completed", "text": "企业语音识别"}
                assert received == [pcm]
