"""Authenticated browser speech input using the enterprise model pool."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

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
    resolve_speech_credentials,
)

from app.services.speech_transports import stream_speech
from app.services.llm.failure_outcome import render_message

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
        raise ValueError(render_message("speech.invalidTicket")) from exc

    redis = await get_redis()
    claimed = await redis.set(f"speech:ticket:{payload['jti']}", "used", ex=120, nx=True)
    if not claimed:
        raise ValueError(render_message("speech.ticketUsed"))
    return user_id


@router.post("/api/speech/ticket")
async def create_speech_ticket(current_user: User = Depends(get_current_user)):
    try:
        async with async_session() as db:
            await resolve_speech_credentials(db, current_user.tenant_id)
            await db.commit()
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
    """Dispatch browser PCM16 input and return normalized transcript events."""
    await websocket.accept()
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        if not isinstance(auth_message, dict) or auth_message.get("type") != "authenticate" or not auth_message.get("ticket"):
            raise ValueError(render_message("speech.missingTicket"))
        ticket = str(auth_message["ticket"])
        user_id = await _consume_speech_ticket(ticket)
    except (ValueError, TypeError, json.JSONDecodeError, TimeoutError) as exc:
        await _send_json_safe(
            websocket,
            {"type": "error", "code": "AUTH_FAILED", "message": str(exc) or render_message("speech.authTimeout")},
        )
        await websocket.close(code=4001)
        return
    except WebSocketDisconnect:
        return

    try:
        async with async_session() as db:
            user = (await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))).scalar_one_or_none()
            if user is None:
                raise SpeechCredentialUnavailable(render_message("speech.userUnavailable"))
            credentials = await resolve_speech_credentials(db, user.tenant_id)
            await db.commit()
    except SpeechCredentialUnavailable as exc:
        await _send_json_safe(websocket, {"type": "error", "code": "CREDENTIAL_UNAVAILABLE", "message": str(exc)})
        await websocket.close(code=4003)
        return

    try:
        await stream_speech(websocket, credentials)
    except WebSocketDisconnect:
        return
    except TimeoutError:
        await _send_json_safe(websocket, {"type": "error", "code": "TIMEOUT", "message": render_message("speech.timeout")})
    except Exception as exc:
        logger.warning(f"[Speech] session failed for user {user_id}: {type(exc).__name__}")
        await _send_json_safe(
            websocket,
            {"type": "error", "code": "UPSTREAM_FAILED", "message": render_message("speech.upstreamFailed")},
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
