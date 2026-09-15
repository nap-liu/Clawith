"""One Web projection of MCP content into the existing text/attachment contract.

This presentation boundary never changes durable results or LLM input.
"""

import json
import mimetypes
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from app.services.chat_attachments import attachment_from_workspace_path
from app.services.tool_results import agent_resource_path, tool_content_visible, tool_result_text
from app.utils.internal_login_redaction import redact_internal_login_values


def tool_result_for_display(result: dict) -> dict:
    content = []
    has_media = False
    for block in result.get("content", []):
        if not tool_content_visible(block, "user"):
            continue
        resource = block.get("resource", block)
        if block["type"] == "text" or "text" in resource:
            content.append({"type": "text", "text": resource["text"]})
            continue
        has_media = True
        try:
            mime = resource.get("mimeType") or ""
            uri = resource.get("uri", "")
            path = agent_resource_path(uri)
            name = resource.get("title") or resource.get("name")
            if path is not None:
                filename = PurePosixPath(path).name
                if len(PurePosixPath(filename).stem) == 64:
                    name = f"{mime.split('/')[0] or 'file'}{PurePosixPath(filename).suffix}"
                attachment = attachment_from_workspace_path(path, display_name=name, mime_type=mime)
            else:
                encoded = block.get("data") if block["type"] in {"image", "audio"} else resource.get("blob")
                if encoded is not None and mime.startswith(("image/", "audio/", "video/")):
                    uri = f"data:{mime};base64,{encoded}"
                elif urlsplit(uri).scheme not in {"http", "https"}:
                    raise ValueError("Resource cannot be displayed")
                name = name or PurePosixPath(urlsplit(uri).path).name
                if encoded is not None:
                    name = "media" + (mimetypes.guess_extension(mime) or ".bin")
                attachment = attachment_from_workspace_path(
                    "resource", display_name=name or "resource", mime_type=mime,
                )
                attachment["path"] = uri
            content.append({"type": "attachment", "attachment": attachment})
        except (ValueError, TypeError):
            content.append({"type": "unavailable"})
    if result.get("structuredContent") is not None:
        content.append({"type": "text", "text": json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)})
    return redact_internal_login_values({"content": content, "isError": bool(result.get("isError")), "hasMedia": has_media})


def tool_event_for_display(event: dict) -> dict:
    public = {key: value for key, value in event.items() if not key.startswith("_") and key != "tool_result"}
    if isinstance(event.get("tool_result"), dict):
        public["toolResultContent"] = tool_result_for_display(event["tool_result"])
        public["result"] = tool_result_text(event["tool_result"], audience="user")
    if event.get("_durable_message_id"):
        public["persistedMessageId"] = event["_durable_message_id"]
    return redact_internal_login_values(public)
