"""Alibaba Cloud Fun-ASR protocol helpers and tenant credential resolution."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import websockets

from app.config import get_settings
from app.core.security import decrypt_data
from app.models.speech_recognition_config import SpeechRecognitionConfig

settings = get_settings()


class SpeechCredentialUnavailable(RuntimeError):
    """Raised when a tenant has no DashScope-compatible credential."""


@dataclass(frozen=True)
class SpeechCredentials:
    api_key: str
    provider: str
    model: str


@dataclass(frozen=True)
class SpeechResult:
    text: str
    sentence_id: int
    is_final: bool
    duration_seconds: int | None = None


async def resolve_speech_credentials(db: AsyncSession, tenant_id: uuid.UUID | None) -> SpeechCredentials:
    """Resolve the tenant's independent speech-service config; never inspect llm_models."""
    if tenant_id is None:
        raise SpeechCredentialUnavailable("当前用户不属于任何租户")

    config = (
        await db.execute(
            select(SpeechRecognitionConfig).where(SpeechRecognitionConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if config is None:
        raise SpeechCredentialUnavailable("当前租户尚未配置语音识别凭证，请联系管理员")
    if not config.enabled or config.provider != "aliyun_dashscope" or config.model != "fun-asr-realtime":
        raise SpeechCredentialUnavailable("当前租户配置的语音识别凭证不可用，请联系管理员")
    try:
        api_key = decrypt_data(config.api_key_encrypted, settings.SECRET_KEY).strip()
    except ValueError:
        api_key = config.api_key_encrypted.strip()
    if not api_key:
        raise SpeechCredentialUnavailable("当前租户的语音识别 API Key 为空")
    return SpeechCredentials(api_key=api_key, provider=config.provider, model=config.model)


async def verify_speech_credentials(credentials: SpeechCredentials) -> None:
    """Perform a short authenticated Fun-ASR task without exposing the key."""
    task_id = str(uuid.uuid4())
    async with websockets.connect(
        settings.ASR_WEBSOCKET_URL,
        additional_headers={"Authorization": f"Bearer {credentials.api_key}"},
        open_timeout=10,
        close_timeout=5,
    ) as upstream:
        await upstream.send(json.dumps(build_run_task(task_id, model=credentials.model), ensure_ascii=False))
        started = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
        if started.get("header", {}).get("event") != "task-started":
            header = started.get("header", {})
            raise RuntimeError(header.get("error_message") or "阿里云语音识别任务启动失败")


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
