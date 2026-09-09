"""Temporary provider URLs over the existing AgentDir file download boundary."""

from __future__ import annotations

import time
import uuid
from pathlib import PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

from jose import JWTError, jwt

from app.config import get_settings
from app.services.im_markdown_media import _canonical_agent_path, _public_base_url
from app.services.storage import agent_storage_key, get_storage_backend


def verify_agent_file_ticket(agent_id: uuid.UUID, path: str, ticket: str) -> dict | None:
    settings = get_settings()
    try:
        payload = jwt.decode(ticket, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    if (payload.get("purpose") != "agent_media_file"
            or payload.get("agent_id") != str(agent_id)
            or payload.get("path") != _canonical_agent_path(path)
            or not isinstance(payload.get("storage_key"), str)
            or not str(payload.get("mime_type", "")).startswith(("image/", "audio/", "video/"))):
        return None
    return payload


async def presign_agent_file(agent_id: uuid.UUID, path: str, mime_type: str) -> str:
    """Use storage signing, with the existing exact-file JWT fallback for local storage."""
    path = _canonical_agent_path(path)
    if path is None:
        raise ValueError("Invalid AgentDir path")
    storage = get_storage_backend()
    key = agent_storage_key(agent_id, path)
    url = await storage.presign_download_url(
        key, filename=PurePosixPath(path).name, inline=True, content_type=mime_type,
    )
    if url:
        if urlsplit(url).scheme in {"http", "https"}:
            return url
        return urljoin(f"{await _public_base_url()}/", url.lstrip("/"))
    settings = get_settings()
    ticket = jwt.encode({
        "purpose": "agent_media_file", "agent_id": str(agent_id), "path": path,
        "storage_key": key, "mime_type": mime_type,
        "exp": int(time.time()) + settings.S3_PRESIGN_TTL_SECONDS,
    }, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return (f"{await _public_base_url()}/api/agents/{agent_id}/files/download"
            f"?path={quote(path, safe='')}&inline=1&im_ticket={quote(ticket, safe='')}")
