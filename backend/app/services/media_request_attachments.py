"""Project durable media inputs into the shared client attachment contract."""

import mimetypes
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from app.services.chat_attachments import attachment_from_workspace_path, infer_attachment_kind


def media_request_attachments(meta: dict) -> list[dict]:
    request = meta.get("media_request") or {}
    files = (request.get("arguments") or {}).get("files") or []
    result = []
    for value in files:
        item = value if isinstance(value, dict) else {"source": value}
        source = item.get("source")
        if not isinstance(source, str) or not source:
            continue
        try:
            parsed = urlsplit(source)
        except ValueError:
            continue
        mime = mimetypes.guess_type(parsed.path)[0] or ""
        name = unquote(PurePosixPath(parsed.path).name) or "media"
        kind = item.get("kind") or infer_attachment_kind(name, mime)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
            attachment = {"path": source, "display_name": name, "kind": kind}
        elif source.startswith(("data:image/", "data:audio/", "data:video/")):
            mime = source[5:].split(";", 1)[0]
            kind = mime.split("/", 1)[0]
            attachment = {"path": source, "display_name": kind + (mimetypes.guess_extension(mime) or ""), "kind": kind}
        else:
            try:
                attachment = attachment_from_workspace_path(source)
            except ValueError:
                continue
            if kind in {"image", "audio", "video"}:
                attachment["kind"] = kind
        if mime:
            attachment["mime_type"] = mime
        result.append(attachment)
    return result
