"""Thin speech transports sharing the browser PCM16/control/event contract."""

import asyncio
import io
import json
import time
import uuid
import wave

import httpx
import websockets

from app.config import get_settings
from app.services.llm.failure_outcome import render_message
from app.services.speech_recognition import build_run_task, build_finish_task, parse_result_event

settings = get_settings()


async def stream_dashscope(websocket, credentials):
    task_id = str(uuid.uuid4())
    started_at = time.monotonic()
    final_sentences: dict[int, str] = {}
    stopping = False

    async with websockets.connect(
        credentials.base_url or settings.ASR_WEBSOCKET_URL,
        additional_headers={"Authorization": f"Bearer {credentials.api_key}"},
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=2 * 1024 * 1024,
    ) as upstream:
        await upstream.send(json.dumps(build_run_task(task_id, model=credentials.model), ensure_ascii=False))
        initial = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
        initial_event = initial.get("header", {}).get("event")
        if initial_event != "task-started":
            header = initial.get("header", {})
            raise RuntimeError(header.get("error_message") or render_message("speech.startFailed"))

        await websocket.send_json({"type": "ready", "sample_rate": 16000, "max_duration": settings.ASR_MAX_DURATION_SECONDS})

        client_receive = asyncio.create_task(websocket.receive())
        upstream_receive = asyncio.create_task(upstream.recv())
        try:
            while True:
                remaining = settings.ASR_MAX_DURATION_SECONDS + 10 - (time.monotonic() - started_at)
                if remaining <= 0:
                    raise TimeoutError(render_message("speech.timeout"))
                done, _ = await asyncio.wait(
                    {client_receive, upstream_receive},
                    timeout=min(remaining, 5),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue

                if client_receive in done:
                    message = client_receive.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None and not stopping:
                        audio = message["bytes"]
                        if len(audio) > 64 * 1024:
                            raise ValueError(render_message("speech.chunkTooLarge"))
                        await upstream.send(audio)
                    elif message.get("text"):
                        control = json.loads(message["text"])
                        if control.get("type") == "cancel":
                            break
                        if control.get("type") == "stop" and not stopping:
                            stopping = True
                            await upstream.send(json.dumps(build_finish_task(task_id)))
                    client_receive = asyncio.create_task(websocket.receive())

                if upstream_receive in done:
                    event = json.loads(upstream_receive.result())
                    event_name = event.get("header", {}).get("event")
                    result = parse_result_event(event)
                    if result is not None:
                        if result.is_final:
                            final_sentences[result.sentence_id] = result.text
                        full_text = "".join(final_sentences[index] for index in sorted(final_sentences))
                        visible_text = full_text if result.is_final else f"{full_text}{result.text}"
                        await websocket.send_json(
                            {
                                "type": "final" if result.is_final else "partial",
                                "text": visible_text,
                                "duration": result.duration_seconds,
                            }
                        )
                    if event_name == "task-failed":
                        header = event.get("header", {})
                        raise RuntimeError(header.get("error_message") or render_message("speech.upstreamFailed"))
                    if event_name == "task-finished":
                        final_text = "".join(final_sentences[index] for index in sorted(final_sentences))
                        await websocket.send_json({"type": "completed", "text": final_text})
                        break
                    upstream_receive = asyncio.create_task(upstream.recv())
        finally:
            for task in (client_receive, upstream_receive):
                if not task.done():
                    task.cancel()
            await asyncio.gather(client_receive, upstream_receive, return_exceptions=True)


async def transcribe_pcm(credentials, pcm: bytes) -> str:
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    endpoint = credentials.base_url.rstrip("/")
    if not endpoint.endswith("/audio/transcriptions"):
        endpoint += "/audio/transcriptions"
    async with httpx.AsyncClient(timeout=credentials.timeout) as client:
        response = await client.post(
            endpoint, headers={"Authorization": f"Bearer {credentials.api_key}"},
            data={"model": credentials.model},
            files={"file": ("recording.wav", audio.getvalue(), "audio/wav")},
        )
        response.raise_for_status()
        return str(response.json().get("text") or "")


async def stream_openai(websocket, credentials):
    audio = bytearray()
    await websocket.send_json({"type": "ready", "sample_rate": 16000,
                               "max_duration": settings.ASR_MAX_DURATION_SECONDS})
    async with asyncio.timeout(settings.ASR_MAX_DURATION_SECONDS + 10):
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                chunk = message["bytes"]
                if len(chunk) > 64 * 1024:
                    raise ValueError(render_message("speech.chunkTooLarge"))
                audio.extend(chunk)
            elif message.get("text"):
                control = json.loads(message["text"])
                if control.get("type") == "cancel":
                    return
                if control.get("type") == "stop":
                    break
    text = await transcribe_pcm(credentials, bytes(audio)) if audio else ""
    await websocket.send_json({"type": "final", "text": text, "duration": len(audio) / 32000})
    await websocket.send_json({"type": "completed", "text": text})


async def stream_speech(websocket, credentials):
    adapter = stream_dashscope if credentials.transport == "dashscope" else stream_openai
    await adapter(websocket, credentials)
