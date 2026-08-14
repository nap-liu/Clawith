"""Prepare attachment-aware messages for one concrete model attempt."""

import base64
import binascii
import copy
import re
from typing import Any

from loguru import logger

from app.services.chat_attachments import (
    normalize_attachment_metadata,
    normalize_chat_message_attachments,
    render_attachment_context,
    sniff_image_mime_bytes,
)
from app.services.storage import agent_storage_key, get_storage_backend

IMAGE_DATA_PATTERN = re.compile(
    r'\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]'
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # Match the inbound upload limit.
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024


def _text_from_content(content: Any) -> tuple[str, list[dict[str, Any]], bool]:
    """Return text, retained image blocks, and whether image data was removed."""
    if not isinstance(content, list):
        raw = str(content or "")
        matches = list(IMAGE_DATA_PATTERN.finditer(raw))
        image_parts = [
            part
            for match in matches
            if (part := _validated_legacy_image_part(match.group(1))) is not None
        ]
        return IMAGE_DATA_PATTERN.sub("", raw).strip(), image_parts, bool(matches)

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
            had_image = True
            canonical = _canonical_content_image_part(part)
            if canonical is not None:
                image_parts.append(canonical)
    return "\n".join(text_parts).strip(), image_parts, had_image


def _validated_legacy_image_part(data_url: str) -> dict[str, Any] | None:
    """Accept old inline transport only when its decoded bytes are an image."""
    try:
        encoded = data_url.split(",", 1)[1]
        # Avoid decoding an oversized legacy payload just to reject it later.
        if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            return None
        image_bytes = base64.b64decode(encoded, validate=True)
    except (IndexError, ValueError, binascii.Error):
        return None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return None
    mime = sniff_image_mime_bytes(image_bytes[:32])
    if mime is None:
        return None
    canonical_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": canonical_url}}


def _canonical_content_image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize supported provider-neutral image blocks to one safe shape."""
    raw_url: Any = None
    part_type = part.get("type")
    if part_type in {"image_url", "input_image"}:
        raw_url = part.get("image_url")
        if isinstance(raw_url, dict):
            raw_url = raw_url.get("url")
    elif part_type == "image":
        source = part.get("source")
        if isinstance(source, dict):
            if source.get("type") == "base64":
                raw_url = (
                    f"data:{source.get('media_type')};base64,{source.get('data')}"
                )
            elif source.get("type") == "url":
                raw_url = source.get("url")

    if not isinstance(raw_url, str):
        return None
    if raw_url.startswith("data:image/"):
        return _validated_legacy_image_part(raw_url)
    if raw_url.startswith(("https://", "http://")):
        return {"type": "image_url", "image_url": {"url": raw_url}}
    return None


def _vision_image_positions(
    states: list[dict[str, Any]],
    *,
    max_historical_images: int,
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Select structured or pathless legacy images under one history cap."""
    last_user_index = next(
        (index for index in range(len(states) - 1, -1, -1) if states[index]["message"].get("role") == "user"),
        -1,
    )
    selected_attachments: set[tuple[int, int]] = set()
    selected_legacy: set[tuple[int, int]] = set()
    selected_paths: set[str] = set()
    historical_count = 0
    for message_index in range(len(states) - 1, -1, -1):
        state = states[message_index]
        if state["message"].get("role") != "user":
            continue
        attachments = state["attachments"]
        has_structured_images = any(item.get("kind") == "image" for item in attachments)
        for attachment_index in range(len(attachments) - 1, -1, -1):
            if attachments[attachment_index].get("kind") != "image":
                continue
            path = attachments[attachment_index]["path"]
            if path in selected_paths:
                continue
            if message_index == last_user_index:
                selected_attachments.add((message_index, attachment_index))
                selected_paths.add(path)
            elif historical_count < max_historical_images:
                selected_attachments.add((message_index, attachment_index))
                selected_paths.add(path)
                historical_count += 1
        if has_structured_images:
            continue
        for legacy_index in range(len(state["legacy_image_parts"]) - 1, -1, -1):
            if message_index == last_user_index:
                selected_legacy.add((message_index, legacy_index))
            elif historical_count < max_historical_images:
                selected_legacy.add((message_index, legacy_index))
                historical_count += 1
    return selected_attachments, selected_legacy


async def _mark_attachment_availability(
    *, agent_id: Any, attachments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    storage = get_storage_backend()
    checked: list[dict[str, Any]] = []
    for attachment in attachments:
        item = dict(attachment)
        try:
            key = agent_storage_key(agent_id, item["path"])
            item["available"] = bool(
                agent_id is not None
                and await storage.exists(key)
                and await storage.is_file(key)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ImageContext] Failed to check attachment {item['path']}: {exc}")
            item["available"] = False
        checked.append(item)
    return checked


async def _attachment_image_part(
    *,
    agent_id: Any,
    attachment: dict[str, Any],
    remaining_bytes: int,
) -> tuple[dict[str, Any], int] | None:
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
        if stat.size > remaining_bytes:
            logger.info(f"[ImageContext] Skipping image beyond turn payload budget: {key}")
            return None
        image_bytes = await storage.read_bytes(key)
    except Exception as exc:  # noqa: BLE001 - storage backends expose heterogeneous transport errors
        logger.warning(f"[ImageContext] Failed to read attachment {key}: {exc}")
        return None

    mime = sniff_image_mime_bytes(image_bytes[:32])
    if mime is None:
        logger.warning(f"[ImageContext] Attachment is not a supported image: {key}")
        return None
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": data_url}}, len(image_bytes)


def _legacy_image_size(part: dict[str, Any]) -> int:
    url = str(part.get("image_url", {}).get("url") or "")
    encoded = url.split(",", 1)[1] if "," in url else ""
    return max(0, (len(encoded.rstrip("=")) * 3) // 4)


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
    states: list[dict[str, Any]] = []
    for message in source:
        text, legacy_image_parts, had_image_data = _text_from_content(message.get("content"))
        attachments = normalize_attachment_metadata(message.get("attachments"))
        if (
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and "attachments" not in message
        ):
            text, legacy_attachments = normalize_chat_message_attachments(
                message.get("content") or "",
                {},
                message.get("source_channel"),
            )
            attachments = legacy_attachments
        states.append(
            {
                "message": message,
                "text": text,
                "legacy_image_parts": legacy_image_parts,
                "had_image_data": had_image_data,
                "attachments": attachments,
            }
        )

    selected_attachments: set[tuple[int, int]] = set()
    selected_legacy: set[tuple[int, int]] = set()
    if supports_vision:
        selected_attachments, selected_legacy = _vision_image_positions(
            states, max_historical_images=max_historical_images
        )
    prepared: list[dict[str, Any] | None] = [None] * len(states)
    total_image_bytes = 0

    # Newest-to-oldest loading gives the current turn first claim on the
    # bounded payload budget, then the selected recent history.
    for message_index in range(len(states) - 1, -1, -1):
        state = states[message_index]
        message = state["message"]
        attachments = await _mark_attachment_availability(
            agent_id=agent_id, attachments=state["attachments"]
        )
        rendered_text = render_attachment_context(state["text"], attachments)
        if (
            state["had_image_data"]
            and not attachments
            and message.get("role") == "user"
            and (not supports_vision or not state["legacy_image_parts"])
        ):
            missing_path = "[历史图片附件未记录可访问路径]"
            rendered_text = f"{missing_path}\n\n{rendered_text}" if rendered_text else missing_path

        image_parts: list[dict[str, Any]] = []
        if supports_vision:
            for attachment_index, attachment in enumerate(attachments):
                if (message_index, attachment_index) not in selected_attachments:
                    continue
                loaded = await _attachment_image_part(
                    agent_id=agent_id,
                    attachment=attachment,
                    remaining_bytes=max(0, MAX_TOTAL_IMAGE_BYTES - total_image_bytes),
                )
                if loaded is not None:
                    image_part, image_size = loaded
                    image_parts.append(image_part)
                    total_image_bytes += image_size
            legacy_selected = [
                part
                for legacy_index, part in enumerate(state["legacy_image_parts"])
                if (message_index, legacy_index) in selected_legacy
            ]
            if legacy_selected:
                for legacy_part in legacy_selected:
                    image_size = _legacy_image_size(legacy_part)
                    if total_image_bytes + image_size <= MAX_TOTAL_IMAGE_BYTES:
                        image_parts.append(legacy_part)
                        total_image_bytes += image_size
            elif not image_parts:
                # A selected structured legacy path may no longer exist. Use
                # only as many co-located old transport blocks as were selected,
                # never bypassing the historical image cap.
                fallback_count = sum(
                    1 for index, _ in selected_attachments if index == message_index
                )
                if fallback_count:
                    for legacy_part in state["legacy_image_parts"][-fallback_count:]:
                        image_size = _legacy_image_size(legacy_part)
                        if total_image_bytes + image_size <= MAX_TOTAL_IMAGE_BYTES:
                            image_parts.append(legacy_part)
                            total_image_bytes += image_size

        updated = {key: value for key, value in message.items() if key != "attachments"}
        if image_parts:
            updated["content"] = image_parts + ([{"type": "text", "text": rendered_text}] if rendered_text else [])
        else:
            updated["content"] = rendered_text
        prepared[message_index] = updated

    return [message for message in prepared if message is not None]
