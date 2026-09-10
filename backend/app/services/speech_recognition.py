"""Enterprise speech model resolution and native Fun-ASR protocol helpers."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession
import websockets

from app.config import get_settings
from app.services.llm.client_registry import get_provider_spec
from app.services.llm.utils import get_model_api_key
from app.services.llm.failure_outcome import render_message
from app.services.speech_model_selection import SpeechCredentialUnavailable, resolve_speech_model
from app.services.model_headers import resolve_model_headers
from app.services.llm.provider_parameters import merge_request_headers

settings = get_settings()


@dataclass(frozen=True)
class SpeechCredentials:
    api_key: str
    provider: str
    model: str
    base_url: str = ""
    transport: str = "dashscope"
    timeout: float = 120
    extra_headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SpeechResult:
    text: str
    sentence_id: int
    is_final: bool
    duration_seconds: int | None = None


async def resolve_speech_credentials(db: AsyncSession, tenant_id: uuid.UUID | None, model_id=None) -> SpeechCredentials:
    """Snapshot an enabled, tenant-owned enterprise model before provider I/O."""
    model = await resolve_speech_model(db, tenant_id, model_id)
    api_key = get_model_api_key(model).strip()
    if not api_key:
        raise SpeechCredentialUnavailable(render_message("speech.emptyKey"))
    spec = get_provider_spec(model.provider)
    endpoint = model.base_url or (spec.default_base_url if spec else "") or ""
    native = model.provider in {"qwen", "aliyun_dashscope"} and model.model.startswith("fun-asr")
    if native and not endpoint.startswith(("ws://", "wss://")):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise SpeechCredentialUnavailable(render_message("speech.invalidEndpoint"))
        path = "/api-ws/v1/inference" if parsed.path.rstrip("/") in {"", "/compatible-mode/v1"} else parsed.path
        endpoint = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, parsed.query, ""))
    if not endpoint or (not native and not endpoint.startswith(("https://", "http://"))):
        raise SpeechCredentialUnavailable(render_message("speech.invalidEndpoint"))
    return SpeechCredentials(api_key=api_key, provider=model.provider, model=model.model,
                             base_url=endpoint, transport="dashscope" if native else "openai",
                             extra_headers=tuple(resolve_model_headers(model).items()),
                             timeout=float(model.request_timeout or 120))


async def verify_speech_credentials(credentials: SpeechCredentials) -> None:
    """Probe the selected speech transport without exposing its key."""
    if credentials.transport == "openai":
        from app.services.speech_transports import transcribe_pcm

        await transcribe_pcm(credentials, b"\0" * 3200)
        return
    task_id = str(uuid.uuid4())
    async with websockets.connect(
        credentials.base_url or settings.ASR_WEBSOCKET_URL,
        additional_headers=merge_request_headers(
            {"Authorization": f"Bearer {credentials.api_key}"}, dict(credentials.extra_headers),
        ),
        open_timeout=10,
        close_timeout=5,
    ) as upstream:
        await upstream.send(json.dumps(build_run_task(task_id, model=credentials.model), ensure_ascii=False))
        started = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
        if started.get("header", {}).get("event") != "task-started":
            header = started.get("header", {})
            raise RuntimeError(header.get("error_message") or render_message("speech.startFailed"))
        await upstream.send(json.dumps(build_finish_task(task_id)))


def build_run_task(task_id: str, *, model: str = "fun-asr-realtime") -> dict[str, Any]:
    """Build the documented Fun-ASR duplex task request for Chinese chat input."""
    return {
        "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": {
                "format": "pcm",
                "sample_rate": 16000,
                "language_hints": ["zh"],
                "semantic_punctuation_enabled": False,
                "max_sentence_silence": 700,
                "heartbeat": True,
            },
            "input": {},
        },
    }


def build_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    }


def parse_result_event(event: dict[str, Any]) -> SpeechResult | None:
    """Normalize a Fun-ASR result-generated event and ignore heartbeat packets."""
    if event.get("header", {}).get("event") != "result-generated":
        return None
    sentence = event.get("payload", {}).get("output", {}).get("sentence", {})
    if sentence.get("heartbeat"):
        return None
    text = str(sentence.get("text") or "").strip()
    if not text:
        return None
    usage = event.get("payload", {}).get("usage") or {}
    duration = usage.get("duration")
    return SpeechResult(
        text=text,
        sentence_id=int(sentence.get("sentence_id") or 0),
        is_final=bool(sentence.get("sentence_end")),
        duration_seconds=int(duration) if duration is not None else None,
    )
