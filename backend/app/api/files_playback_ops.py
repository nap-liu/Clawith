"""Playback helper and route implementations for agent workspace media."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.user import User
from app.services.chat_attachments import (
    MEDIA_PROBE_CHUNK_BYTES,
    canonical_media_mime,
    parse_legacy_chat_attachments,
    sniff_media_mime_bytes,
)
from app.services.media_playback import (
    MAX_RANGE_BYTES,
    PLAYBACK_COOKIE,
    create_ticket,
    decode_cookie_user,
    error_detail as playback_error_detail,
    load_ticket,
    verify_signature,
)
from app.services.storage_runtime.base import StorageEntry
from app.services.media_request_attachments import media_request_attachments


def playback_headers_impl() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }


def storage_entry_version_token_impl(entry: StorageEntry) -> str:
    return entry.version_id or entry.etag or entry.content_hash or f"{entry.modified_at}:{entry.size}"


def message_references_media_path_impl(message: ChatMessage, path: str) -> bool:
    meta = message.message_meta if isinstance(message.message_meta, dict) else {}
    if getattr(message, "role", None) == "user" and any(
        item["path"] == path for item in media_request_attachments(meta)
    ):
        return True
    if "attachments" in meta:
        attachments = meta.get("attachments")
        return isinstance(attachments, list) and any(
            isinstance(item, dict) and str(item.get("path") or "") == path
            for item in attachments
        )
    if getattr(message, "role", None) == "user":
        source_channel = str(meta.get("source_channel") or "")
        _, legacy_attachments = parse_legacy_chat_attachments(
            message.content or "", source_channel
        )
        if any(str(item.get("path") or "") == path for item in legacy_attachments):
            return True
    if getattr(message, "role", None) != "tool_call":
        return False
    try:
        payload = json.loads(message.content or "")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    standard_result = payload.get("tool_result")
    if payload.get("status") == "done" and isinstance(standard_result, dict):
        from app.services.tool_results import agent_resource_path

        for block in standard_result.get("content", []):
            resource = block.get("resource", block)
            try:
                if agent_resource_path(resource.get("uri", "")) == path:
                    return True
            except ValueError:
                continue
    tool_name = str(payload.get("name") or payload.get("tool_name") or "")
    if tool_name not in {"send_channel_file", "send_media", "send_audio", "send_video"}:
        return False
    if str(payload.get("status") or "") != "done":
        return False
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return False
    if not isinstance(result, dict):
        return False
    if str(result.get("type") or "") not in {
        "platform_file_delivery",
        "platform_media_delivery",
    }:
        return False
    if result.get("status") not in {None, "sent", "already_sent"}:
        return False
    return str(result.get("path") or result.get("file_path") or "") == path


async def authorize_message_media_path_impl(
    api: Any,
    db: AsyncSession,
    user: User,
    agent_id: uuid.UUID,
    message_id: str,
    path: str,
) -> None:
    try:
        message_uuid = uuid.UUID(message_id)
        session_uuid = None
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    message = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.id == message_uuid,
                ChatMessage.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if message is not None:
        try:
            session_uuid = uuid.UUID(str(message.conversation_id))
        except (TypeError, ValueError):
            session_uuid = None
    if message is None or session_uuid is None or not api._message_references_media_path(message, path):
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    from app.api.chat_sessions import _load_accessible_session
    try:
        await _load_accessible_session(db, user, agent_id, session_uuid)
    except HTTPException:
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))


def parse_media_range_impl(value: str | None, size: int) -> tuple[int, int, bool]:
    """Return an inclusive, bounded range and whether the client sent Range."""

    if size <= 0:
        return 0, -1, bool(value)
    if not value:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid range")
    spec = value[6:].strip()
    start_raw, separator, end_raw = spec.partition("-")
    if not separator:
        raise ValueError("invalid range")
    if not start_raw:
        suffix = int(end_raw)
        if suffix <= 0:
            raise ValueError("invalid suffix")
        start = max(0, size - min(suffix, MAX_RANGE_BYTES))
        return start, size - 1, True
    start = int(start_raw)
    if start < 0 or start >= size:
        raise ValueError("range start outside object")
    requested_end = int(end_raw) if end_raw else size - 1
    if requested_end < start:
        raise ValueError("range end before start")
    end = min(requested_end, size - 1, start + MAX_RANGE_BYTES - 1)
    return start, end, True


async def create_media_playback_ticket_impl(
    api: Any,
    *,
    agent_id: uuid.UUID,
    body: Any,
    request: Request,
    current_user: User,
    db: AsyncSession,
):
    """Create a fresh, cookie-bound URL for one audio/video playback."""

    await api.check_agent_access(db, current_user, agent_id)
    await api._authorize_message_media_path(
        db, current_user, agent_id, str(body.message_id), body.path
    )
    storage = api.get_storage_backend()
    key, _ = api._visible_storage_key(agent_id, body.path, current_user.tenant_id)
    if not await storage.exists(key) or not await storage.is_file(key):
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    filename = Path(body.path).name
    entry = await storage.stat(key)
    if entry.size <= MEDIA_PROBE_CHUNK_BYTES * 2:
        probe = await storage.read_range(key, 0, max(0, entry.size - 1))
    else:
        probe = (
            await storage.read_range(key, 0, MEDIA_PROBE_CHUNK_BYTES - 1)
        ) + (
            await storage.read_range(
                key,
                entry.size - MEDIA_PROBE_CHUNK_BYTES,
                entry.size - 1,
            )
        )
    actual_mime = sniff_media_mime_bytes(probe, filename)
    actual_kind = actual_mime.split("/", 1)[0] if actual_mime else None
    if actual_kind not in {"audio", "video"}:
        raise HTTPException(status_code=415, detail=playback_error_detail(
            "MEDIA_TYPE_UNSUPPORTED", "无法识别该附件的音视频格式"
        ))
    mime_type = canonical_media_mime(filename, actual_kind, actual_mime)
    try:
        ticket, signature, cookie_token = await create_ticket(
            user_id=str(current_user.id),
            agent_id=str(agent_id),
            path=body.path,
            mime_type=mime_type,
            size_bytes=entry.size,
            version_token=api._storage_entry_version_token(entry),
            message_id=str(body.message_id),
        )
    except Exception:
        raise HTTPException(status_code=503, detail=playback_error_detail(
            "PLAYBACK_SERVICE_UNAVAILABLE", "播放服务暂时不可用", retryable=True, retry_after_ms=1500
        ))
    playback_url = (
        f"/api/agents/{agent_id}/files/playback/{ticket.ticket_id}"
        f"?signature={signature}"
    )
    response = JSONResponse({
        "playback_session_id": ticket.ticket_id,
        "playback_url": playback_url,
        "mime_type": mime_type,
        "size_bytes": entry.size,
        "absolute_expires_at": ticket.absolute_expires_at,
    }, headers=api._playback_headers())
    response.set_cookie(
        PLAYBACK_COOKIE,
        cookie_token,
        max_age=max(1, ticket.absolute_expires_at - int(time.time())),
        httponly=True,
        secure=(
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower() == "https"
        ),
        samesite="lax",
        path=f"/api/agents/{agent_id}/files/playback",
    )
    return response


async def authorize_playback_ticket_impl(
    api: Any,
    *,
    agent_id: uuid.UUID,
    ticket_id: str,
    signature: str,
    cookie_token: str | None,
    db: AsyncSession,
):
    if not verify_signature(ticket_id, signature):
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    cookie_user_id = decode_cookie_user(cookie_token)
    if not cookie_user_id:
        raise HTTPException(status_code=401, detail=playback_error_detail(
            "PLAYBACK_AUTHORIZATION_REQUIRED", "播放授权已过期", retryable=True
        ))
    try:
        ticket = await load_ticket(ticket_id, touch=True)
    except Exception:
        raise HTTPException(status_code=503, detail=playback_error_detail(
            "PLAYBACK_SERVICE_UNAVAILABLE", "播放服务暂时不可用", retryable=True
        ))
    if not ticket or ticket.agent_id != str(agent_id) or ticket.user_id != cookie_user_id:
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "PLAYBACK_SESSION_EXPIRED", "播放授权已过期", retryable=True
        ))
    try:
        user_uuid = uuid.UUID(cookie_user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail=playback_error_detail(
            "PLAYBACK_AUTHORIZATION_REQUIRED", "播放授权已过期", retryable=True
        ))
    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    try:
        await api.check_agent_access(db, user, agent_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    if not ticket.message_id:
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "PLAYBACK_SESSION_EXPIRED", "播放授权已过期", retryable=True
        ))
    await api._authorize_message_media_path(
        db, user, agent_id, ticket.message_id, ticket.path
    )
    return ticket, user


async def get_media_playback_status_impl(
    api: Any,
    *,
    agent_id: uuid.UUID,
    ticket_id: str,
    signature: str,
    request: Request,
    db: AsyncSession,
):
    try:
        ticket, user = await api._authorize_playback_ticket(
            agent_id=agent_id,
            ticket_id=ticket_id,
            signature=signature,
            cookie_token=request.cookies.get(PLAYBACK_COOKIE),
            db=db,
        )
    except HTTPException as exc:
        code = "PLAYBACK_SESSION_EXPIRED" if exc.status_code in {401, 404} else "PLAYBACK_SERVICE_UNAVAILABLE"
        return JSONResponse(
            playback_error_detail(code, "播放授权已过期" if code.endswith("EXPIRED") else "播放服务暂时不可用", retryable=True),
            status_code=exc.status_code,
            headers=api._playback_headers(),
        )
    storage = api.get_storage_backend()
    key, _ = api._visible_storage_key(agent_id, ticket.path, user.tenant_id)
    if not await storage.exists(key) or not await storage.is_file(key):
        return JSONResponse(
            playback_error_detail(
                "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
            ),
            status_code=404,
            headers=api._playback_headers(),
        )
    entry = await storage.stat(key)
    if ticket.version_token and ticket.version_token != api._storage_entry_version_token(entry):
        return JSONResponse(
            playback_error_detail(
                "MEDIA_VERSION_CHANGED", "媒体文件已更新，请重新加载", retryable=True
            ),
            status_code=409,
            headers=api._playback_headers(),
        )
    return JSONResponse({
        "status": "ready",
        "absolute_expires_at": ticket.absolute_expires_at,
    }, headers=api._playback_headers())


async def stream_media_playback_impl(
    api: Any,
    *,
    agent_id: uuid.UUID,
    ticket_id: str,
    signature: str,
    request: Request,
    db: AsyncSession,
):
    ticket, user = await api._authorize_playback_ticket(
        agent_id=agent_id,
        ticket_id=ticket_id,
        signature=signature,
        cookie_token=request.cookies.get(PLAYBACK_COOKIE),
        db=db,
    )
    storage = api.get_storage_backend()
    key, _ = api._visible_storage_key(agent_id, ticket.path, user.tenant_id)
    if not await storage.exists(key) or not await storage.is_file(key):
        raise HTTPException(status_code=404, detail=playback_error_detail(
            "MEDIA_NOT_FOUND_OR_FORBIDDEN", "媒体不存在或当前不可访问"
        ))
    entry = await storage.stat(key)
    size = entry.size
    current_version = api._storage_entry_version_token(entry)
    if ticket.version_token and ticket.version_token != current_version:
        return JSONResponse(
            playback_error_detail(
                "MEDIA_VERSION_CHANGED", "媒体文件已更新，请重新加载", retryable=True
            ),
            status_code=409,
            headers=api._playback_headers(),
        )
    etag = entry.etag or current_version
    etag_header = f'"{etag}"' if etag else ""
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and if_range and if_range.strip() != etag_header:
        range_header = None
    try:
        start, end, had_range = api._parse_media_range(range_header, size)
    except (TypeError, ValueError):
        return Response(
            status_code=416,
            headers={**api._playback_headers(), "Content-Range": f"bytes */{size}"},
        )
    partial = had_range or start > 0 or end < size - 1
    content_length = max(0, end - start + 1)
    headers = {
        **api._playback_headers(),
        "Content-Length": str(content_length),
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(Path(ticket.path).name)}",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if etag_header:
        headers["ETag"] = etag_header
    if request.method == "HEAD" or content_length == 0:
        return Response(status_code=206 if partial else 200, media_type=ticket.mime_type, headers=headers)
    if not partial:
        async def iter_full_media():
            offset = 0
            while offset < size:
                chunk_end = min(size - 1, offset + MAX_RANGE_BYTES - 1)
                chunk = await storage.read_range(key, offset, chunk_end)
                if not chunk:
                    break
                yield chunk
                offset += len(chunk)

        return StreamingResponse(
            iter_full_media(),
            status_code=200,
            media_type=ticket.mime_type,
            headers=headers,
        )
    data = await storage.read_range(key, start, end)
    headers["Content-Length"] = str(len(data))
    return Response(
        content=data,
        status_code=206 if partial else 200,
        media_type=ticket.mime_type,
        headers=headers,
    )
