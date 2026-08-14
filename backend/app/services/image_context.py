"""Prepare attachment-aware messages for one concrete model attempt."""

import base64
import copy
import mimetypes
import re
from typing import Any

from loguru import logger

from app.services.chat_attachments import (
    normalize_attachment_metadata,
    normalize_chat_message_attachments,
)
from app.services.storage import agent_storage_key, get_storage_backend

IMAGE_DATA_PATTERN = re.compile(
    r'\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]'
)
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB per image

_IMAGE_LABELS = {
    "image": "图片附件",
    "audio": "音频附件",
    "video": "视频附件",
    "file": "文件附件",
}


def _text_from_content(content: Any) -> tuple[str, list[dict[str, Any]], bool]:
    """Return text, retained image blocks, and whether image data was removed."""
    if not isinstance(content, list):
        raw = str(content or "")
        image_parts = [
            {"type": "image_url", "image_url": {"url": match.group(1)}}
            for match in IMAGE_DATA_PATTERN.finditer(raw)
        ]
        return IMAGE_DATA_PATTERN.sub("", raw).strip(), image_parts, bool(image_parts)

    text_parts: list[str] = []
    image_parts: list[dict[str, Any]] = []
    had_image = False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = str(part.get("text") or "")
            had_image = had_image or bool(IMAGE_DATA_PATTERN.search(text))
            cleaned = IMAGE_DATA_PATTERN.sub("", text).strip()
            if cleaned:
                text_parts.append(cleaned)
        elif part.get("type") in {"image", "image_url", "input_image"}:
            image_parts.append(copy.deepcopy(part))
            had_image = True
    return "\n".join(text_parts).strip(), image_parts, had_image


def _render_attachment_text(content: str, attachments: list[dict[str, Any]]) -> str:
    blocks = []
    for attachment in attachments:
        label = _IMAGE_LABELS.get(attachment.get("kind"), "文件附件")
        blocks.append(
            f"[{label}]\n"
            f"文件名：{attachment['display_name']}\n"
            f"路径：{attachment['path']}"
        )
    if content:
        blocks.append(content)
    return "\n\n".join(blocks)


def _vision_attachment_positions(
    messages: list[dict[str, Any]],
    *,
    max_historical_images: int,
) -> set[tuple[int, int]]:
    """Select all current-turn images plus the newest historical images."""
    last_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
        -1,
    )
    selected: set[tuple[int, int]] = set()
    selected_paths: set[str] = set()
    historical_count = 0
    for message_index in range(len(messages) - 1, -1, -1):
        if messages[message_index].get("role") != "user":
            continue
        attachments = normalize_attachment_metadata(messages[message_index].get("attachments"))
        for attachment_index in range(len(attachments) - 1, -1, -1):
            if attachments[attachment_index].get("kind") != "image":
                continue
            path = attachments[attachment_index]["path"]
            if path in selected_paths:
                continue
            if message_index == last_user_index:
                selected.add((message_index, attachment_index))
                selected_paths.add(path)
            elif historical_count < max_historical_images:
                selected.add((message_index, attachment_index))
                selected_paths.add(path)
                historical_count += 1
    return selected


async def _attachment_image_part(
    *,
    agent_id: Any,
    attachment: dict[str, Any],
) -> dict[str, Any] | None:
    if agent_id is None:
        return None
    key = agent_storage_key(agent_id, attachment["path"])
    storage = get_storage_backend()
    try:
        if not await storage.exists(key) or not await storage.is_file(key):
            logger.warning(f"[ImageContext] Attachment file not found: {key}")
            return None
        stat = await storage.stat(key)
        if stat.size > MAX_IMAGE_BYTES:
            logger.info(f"[ImageContext] Skipping large image: {key} ({stat.size} bytes)")
            return None
        image_bytes = await storage.read_bytes(key)
    except Exception as exc:  # noqa: BLE001 - storage backends expose heterogeneous transport errors
        logger.warning(f"[ImageContext] Failed to read attachment {key}: {exc}")
        return None

    mime = str(attachment.get("mime_type") or mimetypes.guess_type(attachment["display_name"])[0] or "")
    if not mime.startswith("image/") or mime == "image/svg+xml":
        return None
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": data_url}}


async def prepare_messages_for_model(
    messages: list[dict[str, Any]],
    *,
    agent_id: Any,
    supports_vision: bool,
    max_historical_images: int = 3,
) -> list[dict[str, Any]]:
    """Prepare one immutable message view for the actual model attempt.

    Attachment metadata and workspace paths are the source of truth. Vision
    payloads are materialized only for a vision-capable model; text-only models
    receive the same user text plus accurate attachment paths and never receive
    image blocks or base64 transport data.
    """
    source = [copy.deepcopy(message) for message in messages]
    selected_images = (
        _vision_attachment_positions(source, max_historical_images=max_historical_images)
        if supports_vision
        else set()
    )
    prepared: list[dict[str, Any]] = []

    for message_index, message in enumerate(source):
        text, retained_image_parts, had_image_data = _text_from_content(message.get("content"))
        attachments = normalize_attachment_metadata(message.get("attachments"))

        if (
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and not attachments
        ):
            text, legacy_attachments = normalize_chat_message_attachments(
                message.get("content") or "",
                {},
                message.get("source_channel"),
            )
            attachments = legacy_attachments

        rendered_text = _render_attachment_text(text, attachments)
        if had_image_data and not attachments and message.get("role") == "user" and not supports_vision:
            missing_path = "[历史图片附件未记录可访问路径]"
            rendered_text = f"{missing_path}\n\n{rendered_text}" if rendered_text else missing_path

        image_parts: list[dict[str, Any]] = []
        if supports_vision:
            for attachment_index, attachment in enumerate(attachments):
                if (message_index, attachment_index) not in selected_images:
                    continue
                image_part = await _attachment_image_part(agent_id=agent_id, attachment=attachment)
                if image_part is not None:
                    image_parts.append(image_part)
            # Older persisted turns may have transport data but no structured
            # metadata (or their referenced file may since have disappeared).
            # Keep that historical input working only for vision models.
            if not image_parts:
                image_parts.extend(retained_image_parts)

        updated = {key: value for key, value in message.items() if key != "attachments"}
        if image_parts:
            updated["content"] = image_parts + ([{"type": "text", "text": rendered_text}] if rendered_text else [])
        else:
            updated["content"] = rendered_text
        prepared.append(updated)

    return prepared
