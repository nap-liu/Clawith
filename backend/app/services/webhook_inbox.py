"""Durable file-backed inbox helpers for webhook triggers."""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trigger_execution import webhook_event_id_seq
from app.services.storage import get_storage_backend, normalize_storage_key


@dataclass(frozen=True)
class StagedWebhookPayload:
    """One request body staged on disk without changing its bytes."""

    path: Path
    size: int
    sha256: str
    received_at_ms: int
    received_date: str
    signature: str | None

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


class WebhookPayloadTooLarge(ValueError):
    def __init__(self, size: int, limit: int):
        super().__init__(f"webhook payload is {size} bytes; limit is {limit}")
        self.size = size
        self.limit = limit


async def stage_webhook_payload(
    chunks: AsyncIterable[bytes],
    *,
    max_bytes: int,
    secret: str | None = None,
) -> StagedWebhookPayload:
    """Stream a request body to a temporary file and compute exact digests."""
    received_at_ms = time.time_ns() // 1_000_000
    received_at = datetime.fromtimestamp(received_at_ms / 1000, tz=timezone.utc)
    fd, raw_path = tempfile.mkstemp(prefix="clawith-webhook-", suffix=".payload")
    os.close(fd)
    path = Path(raw_path)
    size = 0
    sha256 = hashlib.sha256()
    signature = hmac.new(secret.encode(), digestmod=hashlib.sha256) if secret else None
    try:
        async with aiofiles.open(path, "wb") as staged:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise WebhookPayloadTooLarge(size, max_bytes)
                sha256.update(chunk)
                if signature is not None:
                    signature.update(chunk)
                await staged.write(chunk)
        return StagedWebhookPayload(
            path=path,
            size=size,
            sha256=sha256.hexdigest(),
            received_at_ms=received_at_ms,
            received_date=received_at.strftime("%Y%m%d"),
            signature=("sha256=" + signature.hexdigest()) if signature is not None else None,
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def allocate_webhook_event_id(db: AsyncSession) -> int:
    """Allocate a globally unique monotonic BIGINT event ID."""
    result = await db.execute(select(webhook_event_id_seq.next_value()))
    return int(result.scalar_one())


def _payload_filename(content_type: str) -> str:
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json"):
        return "payload.json"
    if media_type.startswith("text/"):
        return "payload.txt"
    return "payload.raw"


async def persist_webhook_payload(
    staged: StagedWebhookPayload,
    *,
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    event_id: int,
    content_type: str,
) -> dict:
    """Persist exact request bytes and return the lightweight queue reference."""
    event_key = f"{staged.received_at_ms}_{event_id:020d}"
    relative_path = (
        f"webhook/{trigger_id}/{staged.received_date}/{event_key}/"
        f"{_payload_filename(content_type)}"
    )
    storage_key = normalize_storage_key(f"{agent_id}/{relative_path}")
    storage = get_storage_backend()
    await storage.write_local_file(
        storage_key,
        staged.path,
        content_type=content_type or "application/octet-stream",
    )
    return {
        "kind": "webhook_inbox_event_v1",
        "event_id": event_id,
        "received_at_ms": staged.received_at_ms,
        "event_key": event_key,
        "path": relative_path,
        "size": staged.size,
        "sha256": staged.sha256,
        "content_type": content_type or "application/octet-stream",
    }


def is_webhook_event_ref(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == "webhook_inbox_event_v1"
        and isinstance(value.get("event_id"), int)
        and isinstance(value.get("path"), str)
    )


def format_webhook_inbox_context(config: dict, *, language: str = "en") -> str:
    """Render stable event references, never payload bytes, into wake context."""
    items = config.get("_webhook_batch")
    if not isinstance(items, list):
        event = config.get("_webhook_event")
        if is_webhook_event_ref(event):
            items = [event]
        else:
            items = []
    refs = [item for item in items if is_webhook_event_ref(item)]
    if not refs:
        return ""

    if language == "zh":
        lines = [f"\nWebhook Inbox（{len(refs)} 条提交）："]
        for ref in refs:
            lines.extend(
                (
                    f"- Event ID：{ref['event_id']}",
                    f"  接收时间戳（毫秒）：{ref['received_at_ms']}",
                    f"  Payload 文件：{ref['path']}",
                    f"  大小：{ref['size']} bytes；SHA-256：{ref['sha256']}",
                )
            )
        lines.append("请先使用 read_file 读取对应文件，再根据完整原始数据处理；不要猜测缺失字段。")
        return "\n".join(lines)

    lines = [f"\nWebhook Inbox ({len(refs)} submission(s)):"]
    for ref in refs:
        lines.extend(
            (
                f"- Event ID: {ref['event_id']}",
                f"  Received timestamp (ms): {ref['received_at_ms']}",
                f"  Payload file: {ref['path']}",
                f"  Size: {ref['size']} bytes; SHA-256: {ref['sha256']}",
            )
        )
    lines.append(
        "Read each file with read_file before processing it. Use the complete original data and do not infer missing fields."
    )
    return "\n".join(lines)
