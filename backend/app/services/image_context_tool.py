"""Agent tool for adding durable AgentDir images to active model context."""

from __future__ import annotations

import base64
from typing import Any

from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.chat_attachments import sniff_image_mime_bytes
from app.services.image_context import MAX_IMAGE_BYTES
from app.services.storage import get_storage_backend

TOOL_NAME = "add_media_to_ctx"

IMAGE_CONTEXT_FUNCTION_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Add one or more existing images from the current AgentDir to the model's "
            "active context, using their exact Agent-relative paths."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Exact Agent-relative image paths, in desired context order.",
                },
            },
            "required": ["files"],
        },
    },
}

IMAGE_CONTEXT_TOOL_SEED = {
    "name": TOOL_NAME,
    "display_name": "Add Media to Context",
    "description": IMAGE_CONTEXT_FUNCTION_TOOL["function"]["description"],
    "category": "media",
    "icon": "🖼️",
    "is_default": True,
    "parameters_schema": IMAGE_CONTEXT_FUNCTION_TOOL["function"]["parameters"],
    "config": {},
    "config_schema": {"fields": []},
}


async def add_media_to_ctx(agent_id: Any, files: list[Any]) -> dict:
    """Return standard image result blocks for exact accessible workspace paths."""
    if not isinstance(files, list) or not files:
        raise ValueError("files must contain at least one Agent-relative image path")

    workspace = current_agent_runtime_workspace(agent_id)
    storage = get_storage_backend()
    blocks: list[dict] = []
    loaded_paths: list[str] = []
    for raw_path in files:
        path = str(raw_path or "").strip().replace("\\", "/")
        if not path:
            raise ValueError("image path cannot be empty")
        # Both calls enforce the active Agent workspace boundary. The storage
        # key is provider-neutral and works for local and object storage.
        workspace.local_path(path)
        key = workspace.storage_key(path)
        if not await storage.exists(key) or not await storage.is_file(key):
            raise ValueError(f"image is not accessible: {path}")
        stat = await storage.stat(key)
        if stat.size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds the {MAX_IMAGE_BYTES}-byte per-file limit: {path}")
        raw = await storage.read_bytes(key)
        mime = sniff_image_mime_bytes(raw[:32])
        if mime is None:
            raise ValueError(f"file is not a supported image: {path}")
        blocks.append({
            "type": "image",
            "mimeType": mime,
            "data": base64.b64encode(raw).decode("ascii"),
            "annotations": {"audience": ["assistant"]},
        })
        loaded_paths.append(path)

    label = "\n".join(f"- {path}" for path in loaded_paths)
    return {
        "content": [
            {
                "type": "text",
                "text": f"Loaded {len(loaded_paths)} image(s) into current context:\n{label}",
            },
            *blocks,
        ],
        "structuredContent": {
            "count": len(loaded_paths),
            "files": loaded_paths,
        },
    }


__all__ = (
    "IMAGE_CONTEXT_FUNCTION_TOOL",
    "IMAGE_CONTEXT_TOOL_SEED",
    "TOOL_NAME",
    "add_media_to_ctx",
)
