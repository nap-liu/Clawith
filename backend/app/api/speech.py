"""Authenticated browser speech-input proxy for Alibaba Cloud Fun-ASR."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.core.events import get_redis
from app.core.security import get_current_user
from app.database import async_session
from app.models.user import User
from app.services.speech_recognition import (
    SpeechCredentialUnavailable,
    build_finish_task,
    build_run_task,
    parse_result_event,
    resolve_speech_credentials,
)

router = APIRouter(tags=["speech"])
settings = get_settings()


def _make_speech_ticket(user_id: uuid.UUID) -> tuple[str, int]:
    expires_in = 90
    payload = {
        "sub": str(user_id),
        "scope": "speech:stream",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, expires_in


async def _consume_speech_ticket(ticket: str) -> uuid.UUID:
    try:
        payload = jwt.decode(ticket, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("scope") != "speech:stream" or not payload.get("jti") or not payload.get("sub"):
            raise ValueError("invalid ticket scope")
        user_id = uuid.UUID(str(payload["sub"]))
    except (JWTError, ValueError, TypeError) as exc:
        raise ValueError("语音凭证无效或已过期") from exc

    redis = await get_redis()
    claimed = await redis.set(f"speech:ticket:{payload['jti']}", "used", ex=120, nx=True)
    if not claimed:
        raise ValueError("语音凭证已使用")
    return user_id


@router.post("/api/speech/ticket")
async def create_speech_ticket(current_user: User = Depends(get_current_user)):
    try:
        async with async_session() as db:
            await resolve_speech_credentials(db, current_user.tenant_id)
    except SpeechCredentialUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ticket, expires_in = _make_speech_ticket(current_user.id)
    return {"ticket": ticket, "expires_in": expires_in}


async def _send_json_safe(websocket: WebSocket, payload: dict) -> None:
    try:
        await websocket.send_json(payload)
    except Exception:
        pass


@router.websocket("/ws/speech")
async def websocket_speech(websocket: WebSocket):
    """Proxy browser PCM16 frames to Fun-ASR and return normalized transcript events."""
    await websocket.accept()
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        if not isinstance(auth_message, dict) or auth_message.get("type") != "authenticate" or not auth_message.get("ticket"):
            raise ValueError("缺少语音凭证")
        ticket = str(auth_message["ticket"])
        user_id = await _consume_speech_ticket(ticket)
    except (ValueError, TypeError, json.JSONDecodeError, TimeoutError) as exc:
        await _send_json_safe(
            websocket,
            {"type": "error", "code": "AUTH_FAILED", "message": str(exc) or "语音认证超时"},
        )
        await websocket.close(code=4001)
        return
    except WebSocketDisconnect:
        return

    try:
        async with async_session() as db:
            user = (await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))).scalar_one_or_none()
            if user is None:
                raise SpeechCredentialUnavailable("用户不存在或已停用")
            credentials = await resolve_speech_credentials(db, user.tenant_id)
    except SpeechCredentialUnavailable as exc:
        await _send_json_safe(websocket, {"type": "error", "code": "CREDENTIAL_UNAVAILABLE", "message": str(exc)})
        await websocket.close(code=4003)
        return

    task_id = str(uuid.uuid4())
    started_at = time.monotonic()
    final_sentences: dict[int, str] = {}
    stopping = False

    try:
        async with websockets.connect(
            settings.ASR_WEBSOCKET_URL,
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
                raise RuntimeError(header.get("error_message") or f"阿里云启动失败：{initial_event or 'unknown'}")

            await websocket.send_json({"type": "ready", "sample_rate": 16000, "max_duration": settings.ASR_MAX_DURATION_SECONDS})

            client_receive = asyncio.create_task(websocket.receive())
            upstream_receive = asyncio.create_task(upstream.recv())
            try:
                while True:
                    remaining = settings.ASR_MAX_DURATION_SECONDS + 10 - (time.monotonic() - started_at)
                    if remaining <= 0:
                        raise TimeoutError("语音输入超时")
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
                                raise ValueError("音频分片过大")
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
                            raise RuntimeError(header.get("error_message") or "阿里云语音识别失败")
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
    except WebSocketDisconnect:
        return
    except TimeoutError as exc:
        await _send_json_safe(websocket, {"type": "error", "code": "TIMEOUT", "message": str(exc)})
    except Exception as exc:
        logger.warning(f"[Speech] session failed for user {user_id}: {type(exc).__name__}")
        await _send_json_safe(
            websocket,
            {"type": "error", "code": "UPSTREAM_FAILED", "message": "语音识别服务暂时不可用，请稍后重试"},
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
