"""MCP-compatible tool results and durable Agent-scoped content resources."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from mcp.types import CallToolResult

from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.storage import get_storage_backend
from app.services.tool_result_paths import tool_result_session_dir

def normalize_tool_result(value, *, is_error: bool = False) -> dict:
    """Preserve standard fields; adapt legacy text without guessing JSON strings."""
    if isinstance(value, CallToolResult):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, str):
        value = {"content": [{"type": "text", "text": value}]}
    elif not isinstance(value, dict):
        raise ValueError("Tool result must be text or a CallToolResult object")
    else:
        value = copy.deepcopy(value)
    if "content" not in value and "structuredContent" in value:
        value["content"] = []
    if is_error:
        value["isError"] = True
    return CallToolResult.model_validate(value).model_dump(
        mode="json", by_alias=True, exclude_none=True,
    )


def tool_content_visible(block: dict, audience: str) -> bool:
    recipients = (block.get("annotations") or {}).get("audience")
    return not recipients or audience in recipients


def tool_result_text(value, *, audience: str = "assistant") -> str:
    """A textual view only; binary data and private MCP metadata never become prose."""
    if isinstance(value, str):
        return value
    parts = []
    if value.get("isError"):
        parts.append("[Tool error]")
    for block in value.get("content", []):
        if not tool_content_visible(block, audience):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block["text"])
        elif kind == "resource" and "text" in block.get("resource", {}):
            parts.append(block["resource"]["text"])
        elif kind == "resource_link":
            parts.append(f"[{block.get('mimeType', 'resource')}: {block['uri']}]")
        else:
            parts.append(f"[{kind}: {block.get('mimeType', 'resource')}]")
    if value.get("structuredContent") is not None:
        parts.append(json.dumps(value["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)


def agent_resource_path(uri: str) -> str | None:
    """An exact virtual path under the execution Agent, never an arbitrary file URI."""
    parsed = urlsplit(uri)
    if parsed.scheme != "agent-file":
        return None
    path = unquote(parsed.path).lstrip("/")
    if parsed.netloc or parsed.query or parsed.fragment or not path:
        raise ValueError("Invalid Agent resource URI")
    if "\\" in path or "\x00" in path or ".." in PurePosixPath(path).parts:
        raise ValueError("Invalid Agent resource path")
    # Only immutable output artifacts produced by this service use this scheme.
    if not path.startswith(".tool_results/"):
        raise ValueError("Agent resource is outside tool results")
    return path


async def persist_tool_result(value, *, agent_id, session_id: str) -> dict:
    """Move inline bytes into durable storage while retaining standard resource blocks."""
    result = normalize_tool_result(value)
    storage = get_storage_backend()
    workspace = current_agent_runtime_workspace(agent_id)
    blocks = []
    for block in result["content"]:
        kind = block["type"]
        resource = block.get("resource", {}) if kind == "resource" else {}
        encoded = block.get("data") if kind in {"image", "audio"} else resource.get("blob")
        if encoded is None:
            blocks.append(block)
            continue
        raw = base64.b64decode(encoded, validate=True)
        mime = block.get("mimeType") or resource.get("mimeType") or "application/octet-stream"
        suffix = mimetypes.guess_extension(mime) or ".bin"
        digest = hashlib.sha256(raw).hexdigest()
        path = str(tool_result_session_dir(session_id) / f"{digest}{suffix}")
        workspace.local_path(path)
        key = workspace.storage_key(path)
        if not await storage.exists(key):
            await storage.write_bytes(key, raw, content_type=mime)
        blocks.append({
            "type": "resource_link",
            "uri": "agent-file:///" + quote(path, safe="/"),
            "name": PurePosixPath(path).name,
            "mimeType": mime,
            **({"annotations": block["annotations"]} if "annotations" in block else {}),
            **({"_meta": block["_meta"]} if "_meta" in block else {}),
        })
    return {**result, "content": blocks}
