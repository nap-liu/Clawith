"""Normalize quoted messages exposed by DingTalk chatbot callbacks."""

from __future__ import annotations

import json
import mimetypes
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from loguru import logger

from app.services.chat_attachments import (
    attachment_from_workspace_path,
    canonical_media_mime,
    sniff_image_mime_bytes,
    sniff_media_mime_bytes,
)
from app.services.quoted_message import normalize_quoted_message
from app.services.storage import sanitize_filename

DownloadMedia = Callable[[str, str, str], Awaitable[bytes | None]]
StoreUpload = Callable[..., Awaitable[str | None]]

_QUOTED_TYPE_MAP = {
    "text": "text",
    "picture": "image",
    "image": "image",
    "audio": "audio",
    "voice": "audio",
    "video": "video",
    "file": "file",
    "richtext": "rich_text",
    "markdown": "card",
    "actioncard": "card",
    "interactivecard": "card",
    "link": "card",
    "oa": "card",
}
_CARD_TEXT_KEYS = {
    "content",
    "description",
    "markdown",
    "message",
    "singleTitle",
    "summary",
    "text",
    "title",
}


def has_trusted_dingtalk_sender_alias(
    sender_staff_id: str,
    sender_id: str,
) -> bool:
    """Return whether one callback carried two distinct sender identifiers."""
    staff_ref = str(sender_staff_id or "").strip()
    opaque_ref = str(sender_id or "").strip()
    return bool(staff_ref and opaque_ref and staff_ref != opaque_ref)


def _content_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _download_code(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    direct = str(
        content.get("downloadCode")
        or content.get("download_code")
        or content.get("pictureDownloadCode")
        or content.get("picture_download_code")
        or ""
    ).strip()
    if direct:
        return direct
    nested = content.get("content")
    return _download_code(nested) if isinstance(nested, dict) else ""


def _attachment_content(content: Any) -> dict[str, Any]:
    """Flatten the one nested content layer found in some rich-text callbacks."""
    if not isinstance(content, dict):
        return {}
    nested = content.get("content")
    if not isinstance(nested, dict):
        return content
    return {**nested, **{key: value for key, value in content.items() if key != "content"}}


def _quoted_filename(content: Any, message_type: str) -> str:
    value = content if isinstance(content, dict) else {}
    raw_name = str(value.get("fileName") or value.get("filename") or value.get("name") or "").strip()
    if raw_name:
        return sanitize_filename(raw_name)[:255]
    suffix = {
        "image": ".jpg",
        "audio": ".amr",
        "video": ".mp4",
        "file": ".bin",
    }.get(message_type, ".bin")
    return f"quoted_{message_type or 'file'}{suffix}"


def _card_text(value: Any, *, depth: int = 0) -> list[str]:
    """Extract human-visible card text without persisting arbitrary payload fields."""
    if depth > 8:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_card_text(item, depth=depth + 1))
        return result
    if not isinstance(value, dict):
        return []

    result: list[str] = []
    for key, item in value.items():
        if key in _CARD_TEXT_KEYS and isinstance(item, str):
            text = item.strip()
            if text:
                result.append(text)
        elif isinstance(item, (dict, list)):
            result.extend(_card_text(item, depth=depth + 1))
    return result


def _iter_rich_items(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_rich_items(item)
        return
    if isinstance(value, dict):
        if any(
            key in value
            for key in (
                "text",
                "downloadCode",
                "download_code",
                "pictureDownloadCode",
                "msgType",
                "msgtype",
            )
        ):
            yield value
            return
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _iter_rich_items(item)


async def _download_attachment(
    *,
    app_key: str,
    app_secret: str,
    agent_id: uuid.UUID,
    content: dict[str, Any],
    message_type: str,
    download_media: DownloadMedia,
    store_upload: StoreUpload,
) -> dict[str, Any] | None:
    content = _attachment_content(content)
    download_code = _download_code(content)
    if not download_code:
        return None
    file_bytes = await download_media(app_key, app_secret, download_code)
    if not file_bytes:
        return None

    display_name = _quoted_filename(content, message_type)
    content_type = str(content.get("contentType") or content.get("mimeType") or "").strip().lower()
    if message_type == "image":
        content_type = sniff_image_mime_bytes(file_bytes[:64]) or (
            content_type if content_type.startswith("image/") else "image/jpeg"
        )
        if display_name == "quoted_image.jpg":
            extension = {
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(content_type, ".jpg")
            display_name = f"quoted_image{extension}"
    elif message_type in {"audio", "video"}:
        sniffed = sniff_media_mime_bytes(file_bytes[: 2 * 1024 * 1024], display_name)
        content_type = canonical_media_mime(display_name, message_type, sniffed)
    elif not content_type:
        content_type = str(mimetypes.guess_type(display_name)[0] or "")

    stored_name = sanitize_filename(f"dingtalk_quote_{uuid.uuid4().hex[:8]}_{display_name}")[:255]
    workspace_path = await store_upload(
        agent_id,
        stored_name,
        file_bytes,
        content_type=content_type or None,
    )
    if workspace_path is None:
        return None
    return attachment_from_workspace_path(
        workspace_path,
        display_name=display_name,
        mime_type=content_type or None,
        size_bytes=len(file_bytes),
    )


async def parse_dingtalk_quoted_message(
    msg_data: dict[str, Any],
    app_key: str,
    app_secret: str,
    agent_id: uuid.UUID,
    *,
    download_media: DownloadMedia,
    store_upload: StoreUpload,
) -> dict[str, Any] | None:
    """Normalize every quoted-message shape exposed by DingTalk Stream."""
    text_data = msg_data.get("text")
    if not isinstance(text_data, dict):
        return None
    replied_msg = text_data.get("repliedMsg")
    reply_flag = text_data.get("isReplyMsg")
    flag_enabled = reply_flag is True or str(reply_flag or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not flag_enabled and not isinstance(replied_msg, dict):
        return None
    if not isinstance(replied_msg, dict):
        return normalize_quoted_message(
            {
                "message_type": "unknown",
                "content_status": "unavailable",
                "attachments": [],
            }
        )

    raw_type = str(replied_msg.get("msgType") or replied_msg.get("msgtype") or "unknown").strip()
    message_type = _QUOTED_TYPE_MAP.get(raw_type.lower(), "unknown")
    raw_content = replied_msg.get("content")
    content = _content_dict(raw_content)
    text_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    expected_downloads = 0
    failed_downloads = 0

    if message_type == "text":
        if isinstance(raw_content, str) and not content:
            text_parts.append(raw_content.strip())
        else:
            quoted_text = str(content.get("text") or content.get("content") or "").strip()
            if quoted_text:
                text_parts.append(quoted_text)
    elif message_type in {"image", "audio", "video", "file"}:
        recognition = str(content.get("recognition") or "").strip()
        if recognition:
            text_parts.append(f"语音识别：{recognition}")
        download_code = _download_code(content)
        if download_code:
            expected_downloads += 1
            attachment = await _download_attachment(
                app_key=app_key,
                app_secret=app_secret,
                agent_id=agent_id,
                content=content,
                message_type=message_type,
                download_media=download_media,
                store_upload=store_upload,
            )
            if attachment is not None:
                attachments.append(attachment)
            else:
                failed_downloads += 1
        elif not recognition:
            failed_downloads += 1
    elif message_type == "rich_text":
        rich_value = content.get("richText") if content else None
        for item in _iter_rich_items(rich_value or raw_content):
            item_type = str(item.get("msgType") or item.get("msgtype") or "").lower()
            item_content = item.get("content")
            item_text = str(item.get("text") or (item_content if isinstance(item_content, str) else "") or "").strip()
            if item_text:
                text_parts.append(item_text)
            download_code = _download_code(item)
            if item_type in {"picture", "image"} or download_code:
                if download_code:
                    expected_downloads += 1
                    attachment = await _download_attachment(
                        app_key=app_key,
                        app_secret=app_secret,
                        agent_id=agent_id,
                        content=item,
                        message_type=("image" if item_type in {"", "picture", "image"} else "file"),
                        download_media=download_media,
                        store_upload=store_upload,
                    )
                    if attachment is not None:
                        attachments.append(attachment)
                    else:
                        failed_downloads += 1
                else:
                    failed_downloads += 1
    else:
        text_parts.extend(_card_text(content))
        if isinstance(raw_content, str) and not content:
            plain_text = raw_content.strip()
            if plain_text:
                text_parts.append(plain_text)
        download_code = _download_code(content)
        if download_code:
            expected_downloads += 1
            attachment = await _download_attachment(
                app_key=app_key,
                app_secret=app_secret,
                agent_id=agent_id,
                content=content,
                message_type="file",
                download_media=download_media,
                store_upload=store_upload,
            )
            if attachment is not None:
                attachments.append(attachment)
            else:
                failed_downloads += 1

    unique_text: list[str] = []
    for item in text_parts:
        clean = item.strip()
        if clean and clean not in unique_text:
            unique_text.append(clean)
    quoted_text = "\n".join(unique_text)
    if failed_downloads:
        content_status = "partial" if quoted_text or attachments else "failed"
    elif quoted_text or attachments:
        content_status = "available"
    else:
        content_status = "unavailable"

    created_at = replied_msg.get("createdAt") or replied_msg.get("createAt")
    try:
        created_at_ms = int(created_at) if created_at is not None else None
    except (TypeError, ValueError):
        created_at_ms = None
    normalized = normalize_quoted_message(
        {
            "message_type": message_type,
            "provider_message_type": raw_type,
            "provider_message_id": replied_msg.get("msgId"),
            "sender_ref": replied_msg.get("senderId") or replied_msg.get("senderStaffId"),
            "sender_name": replied_msg.get("senderNick"),
            "created_at_ms": created_at_ms,
            "content_status": content_status,
            "text": quoted_text,
            "attachments": attachments,
        }
    )
    logger.info(
        "[DingTalk] Parsed quoted message type={} status={} attachments={}/{}",
        raw_type or "unknown",
        content_status,
        len(attachments),
        expected_downloads,
    )
    return normalized
