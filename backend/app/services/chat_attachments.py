"""Canonical chat-message attachment metadata and legacy marker parsing."""

from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath
from typing import Any

from app.services.storage import agent_storage_key, get_storage_backend

ATTACHMENT_KINDS = {"image", "file", "audio", "video"}
MAX_CLIENT_ATTACHMENTS = 10
MAX_DISPLAY_NAME_LENGTH = 255
MAX_ATTACHMENT_PATH_LENGTH = 1024
MAX_MIME_TYPE_LENGTH = 255

_FILE_MARKER_RE = re.compile(r"\[file:([^\]\r\n]+)\]", re.IGNORECASE)
_IMAGE_DATA_RE = re.compile(
    r"\[image_data:data:image/[^;\]]+;base64,[A-Za-z0-9+/=]+\]"
)
_ATTACHMENT_DISPLAY_RE = re.compile(r"^(?:\[Attachment: [^\]\r\n]+\]\s*)+", re.IGNORECASE)
_UPLOADED_FILE_RE = re.compile(
    r"^\[\u6587\u4ef6\u5df2\u4e0a\u4f20:\s*(?:workspace/uploads/)?([^\]\r\n]+)\]\s*"
)
_OLD_WEB_FILE_RE = re.compile(r"^\[File:\s*([^\]\r\n]+)\]", re.IGNORECASE)
_OLD_WEB_QUESTION_RE = re.compile(r"\nQuestion:\s*([\s\S]+)$", re.IGNORECASE)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".amr", ".m4a", ".aac"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MEDIA_PROBE_CHUNK_BYTES = 2 * 1024 * 1024
_CLIENT_ATTACHMENT_PATH_PREFIXES = ("workspace/", "skills/", "media/", "memory/")
_CLIENT_ATTACHMENT_ROOT_FILES = {"memory.md", "soul.md"}
_LEGACY_FALLBACK_SOURCES = {
    "web",
    "miniprogram",
    "wechat_miniprogram",
}

_ATTACHMENT_LABELS = {
    "image": "图片附件",
    "audio": "音频附件",
    "video": "视频附件",
    "file": "文件附件",
}


def _clean_workspace_path(raw_path: Any) -> str | None:
    path = str(raw_path or "").strip().replace("\\", "/")
    if not path or len(path) > MAX_ATTACHMENT_PATH_LENGTH:
        return None
    if path.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path):
        return None
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    canonical = candidate.as_posix()
    if canonical in {"", "."}:
        return None
    return canonical


def _safe_legacy_name(raw_name: Any) -> str | None:
    name = str(raw_name or "").strip().replace("\\", "/")
    if not name:
        return None
    basename = PurePosixPath(name).name.strip()
    if not basename or basename in {".", ".."} or len(basename) > MAX_DISPLAY_NAME_LENGTH:
        return None
    return basename


def infer_attachment_kind(name: str, mime_type: str | None = None) -> str:
    mime = str(mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    extension = PurePosixPath(name).suffix.lower()
    if extension in _IMAGE_EXTENSIONS:
        return "image"
    if extension in _AUDIO_EXTENSIONS:
        return "audio"
    if extension in _VIDEO_EXTENSIONS:
        return "video"
    return "file"


def sniff_media_mime_bytes(head: bytes, name: str = "") -> str | None:
    """Recognize a concrete playable container MIME from leading bytes."""
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"OggS"):
        return "video/ogg" if b"theora" in head.lower() else "audio/ogg"
    if head.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "audio/amr"
    if len(head) >= 12 and head[:4] == b"RIFF":
        if head[8:12] == b"WAVE":
            return "audio/wav"
        if head[8:12] == b"AVI ":
            return "video/x-msvideo"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm" if b"webm" in head.lower() else "video/x-matroska"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        handler_types: set[bytes] = set()
        cursor = 0
        while True:
            marker = head.find(b"hdlr", cursor)
            if marker < 0:
                break
            if marker + 16 <= len(head):
                handler_types.add(head[marker + 12 : marker + 16].lower())
            cursor = marker + 4
        if b"vide" in handler_types:
            return "video/quicktime" if brand == b"qt  " else "video/mp4"
        if b"soun" in handler_types:
            return "audio/mp4"
        if brand in {b"m4a ", b"m4b ", b"f4a "}:
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        return None
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "audio/aac"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "audio/mpeg"
    return None


def sniff_media_kind_bytes(head: bytes, name: str = "") -> str | None:
    """Recognize the audio/video class while retaining the concrete MIME API."""
    mime = sniff_media_mime_bytes(head, name)
    return mime.split("/", 1)[0] if mime else None


def canonical_media_mime(
    name: str,
    media_kind: str,
    sniffed_mime: str | None = None,
) -> str:
    """Return a renderer-safe MIME derived from verified kind + container."""
    detected = str(sniffed_mime or "").lower()
    if detected.startswith(f"{media_kind}/"):
        return detected
    guessed = str(mimetypes.guess_type(name)[0] or "").lower()
    if guessed.startswith(f"{media_kind}/"):
        return guessed
    extension = PurePosixPath(name).suffix.lower()
    if media_kind == "audio":
        return {
            ".amr": "audio/amr",
            ".flac": "audio/flac",
            ".m4a": "audio/mp4",
            ".m4b": "audio/mp4",
            ".mp4": "audio/mp4",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
        }.get(extension, "audio/mpeg")
    return {
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }.get(extension, "video/mp4")


def sniff_image_kind_bytes(head: bytes) -> str | None:
    """Recognize the common image formats accepted as video covers."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def sniff_image_mime_bytes(head: bytes) -> str | None:
    """Return a provider-safe MIME only when image magic bytes agree."""
    kind = sniff_image_kind_bytes(head)
    return {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(kind)


def render_attachment_context(content: str, attachments: list[dict[str, Any]]) -> str:
    """Render one shared, provider-neutral attachment description."""
    blocks: list[str] = []
    for attachment in attachments:
        label = _ATTACHMENT_LABELS.get(attachment.get("kind"), "文件附件")
        block = (
            f"[{label}]\n"
            f"文件名：{attachment['display_name']}\n"
            f"路径：{attachment['path']}"
        )
        if attachment.get("available") is False:
            block += "\n状态：当前不可访问"
        blocks.append(block)
    if content:
        blocks.append(content)
    return "\n\n".join(blocks)


def attachment_from_workspace_path(
    workspace_path: str,
    *,
    display_name: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    path = _clean_workspace_path(workspace_path)
    if path is None:
        raise ValueError("attachment path must be a canonical agent file path")
    name = _safe_legacy_name(display_name or PurePosixPath(path).name)
    if name is None:
        raise ValueError("attachment display name is invalid")
    mime = str(mime_type or mimetypes.guess_type(name)[0] or "").strip().lower() or None
    item: dict[str, Any] = {
        "display_name": name,
        "path": path,
        "kind": infer_attachment_kind(name, mime),
    }
    if mime:
        item["mime_type"] = mime
    if isinstance(size_bytes, int) and size_bytes >= 0:
        item["size_bytes"] = size_bytes
    return item


def normalize_attachment_metadata(raw_attachments: Any) -> list[dict[str, Any]]:
    """Return safe client-facing fields while preserving order and duplicates."""
    if not isinstance(raw_attachments, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        path = _clean_workspace_path(raw.get("path"))
        display_name = _safe_legacy_name(raw.get("display_name") or (PurePosixPath(path).name if path else ""))
        if path is None or display_name is None:
            continue
        mime = str(raw.get("mime_type") or "").strip().lower()
        if len(mime) > MAX_MIME_TYPE_LENGTH:
            mime = ""
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in ATTACHMENT_KINDS:
            kind = infer_attachment_kind(display_name, mime)
        item: dict[str, Any] = {
            "display_name": display_name,
            "path": path,
            "kind": kind,
        }
        if mime:
            item["mime_type"] = mime
        size_bytes = raw.get("size_bytes")
        if isinstance(size_bytes, int) and size_bytes >= 0:
            item["size_bytes"] = size_bytes
        normalized.append(item)
    return normalized


def _attachment_from_legacy_name(raw_name: str) -> dict[str, Any] | None:
    name = _safe_legacy_name(raw_name)
    if name is None:
        return None
    return attachment_from_workspace_path(f"workspace/uploads/{name}", display_name=name)


def _consume_leading_file_markers(content: str) -> tuple[list[str], str]:
    names: list[str] = []
    cursor = 0
    while True:
        match = re.match(r"\s*\[file:([^\]\r\n]+)\]", content[cursor:], re.IGNORECASE)
        if not match:
            break
        names.append(match.group(1))
        cursor += match.end()
    return names, content[cursor:]


def _consume_slack_trailing_file_markers(content: str) -> tuple[list[str], str]:
    block = re.search(r"(?P<block>(?:\s+\[file:[^\]\r\n]+\])+\s*)$", content, re.IGNORECASE)
    if not block:
        return [], content
    names = [match.group(1) for match in _FILE_MARKER_RE.finditer(block.group("block"))]
    return names, content[: block.start()].rstrip()


def _strip_display_protocol(content: str) -> str:
    clean = strip_image_data_markers(content)
    clean = _ATTACHMENT_DISPLAY_RE.sub("", clean)
    return clean.strip()


def strip_image_data_markers(content: str) -> str:
    """Remove legacy inline image transport data without changing user text."""
    return _IMAGE_DATA_RE.sub("", content or "").strip()


def extract_image_data_markers(content: str) -> list[str]:
    """Extract exact legacy image transport markers for LLM-only compatibility."""
    return [match.group(0) for match in _IMAGE_DATA_RE.finditer(content or "")]


def _legacy_names_for_source(raw_names: list[str], source_channel: str) -> list[str]:
    if source_channel in {"web", "miniprogram", "wechat_miniprogram"} and len(raw_names) == 1:
        # Historical Web/H5 persisted all attachment names in one comma-joined marker.
        # This is inherently ambiguous when a real historical filename contains a comma.
        return [part.strip() for part in raw_names[0].split(",") if part.strip()]
    return raw_names


def parse_legacy_chat_attachments(
    content: str,
    source_channel: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Parse only historical, platform-reserved attachment envelopes."""
    raw = content or ""
    source = str(source_channel or "").strip().lower()
    raw_names, remainder = _consume_leading_file_markers(raw)
    if raw_names:
        names = _legacy_names_for_source(raw_names, source)
        attachments = [item for name in names if (item := _attachment_from_legacy_name(name)) is not None]
        return _strip_display_protocol(remainder), attachments

    if source == "slack":
        trailing_names, remainder = _consume_slack_trailing_file_markers(raw)
        if trailing_names:
            attachments = [
                item for name in trailing_names if (item := _attachment_from_legacy_name(name)) is not None
            ]
            return _strip_display_protocol(remainder), attachments

    uploaded = _UPLOADED_FILE_RE.match(raw)
    if uploaded:
        item = _attachment_from_legacy_name(uploaded.group(1))
        return _strip_display_protocol(raw[uploaded.end() :]), [item] if item else []

    old_web = _OLD_WEB_FILE_RE.match(raw)
    if old_web:
        item = _attachment_from_legacy_name(old_web.group(1))
        question = _OLD_WEB_QUESTION_RE.search(raw)
        display = question.group(1) if question else raw[old_web.end() :]
        return _strip_display_protocol(display), [item] if item else []

    return _strip_display_protocol(raw), []


def normalize_chat_message_attachments(
    content: str,
    message_meta: Any,
    source_channel: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return display content + attachments using metadata or legacy fallback."""
    meta = message_meta if isinstance(message_meta, dict) else {}
    if "attachments" not in meta:
        return parse_legacy_chat_attachments(content, source_channel)

    attachments = normalize_attachment_metadata(meta.get("attachments"))
    source = str(source_channel or "").strip().lower()
    if (
        not attachments
        and "display_content" not in meta
        and source in _LEGACY_FALLBACK_SOURCES
    ):
        return parse_legacy_chat_attachments(content, source)
    if "display_content" in meta:
        return str(meta.get("display_content") or ""), attachments
    legacy_display, legacy_attachments = parse_legacy_chat_attachments(content, source_channel)
    if attachments and legacy_attachments:
        return legacy_display, attachments
    return _strip_display_protocol(content), attachments


async def validate_client_attachments(agent_id: Any, raw_attachments: Any) -> list[dict[str, Any]]:
    if raw_attachments is None:
        return []
    if not isinstance(raw_attachments, list):
        raise TypeError("attachments must be an array")
    if len(raw_attachments) > MAX_CLIENT_ATTACHMENTS:
        raise ValueError(f"at most {MAX_CLIENT_ATTACHMENTS} attachments are allowed")

    normalized = normalize_attachment_metadata(raw_attachments)
    if len(normalized) != len(raw_attachments):
        raise ValueError("one or more attachments are invalid")
    if any(
        item["path"] not in _CLIENT_ATTACHMENT_ROOT_FILES
        and not item["path"].startswith(_CLIENT_ATTACHMENT_PATH_PREFIXES)
        for item in normalized
    ):
        raise ValueError("one or more client attachment paths are not allowed")
    storage = get_storage_backend()
    for item in normalized:
        key = agent_storage_key(agent_id, item["path"])
        if not await storage.exists(key) or not await storage.is_file(key):
            raise ValueError(f"attachment is not available: {item['display_name']}")
        # Client kind/MIME declarations are hints only. Image payloads are
        # enabled exclusively by server-observed bytes so a renamed document
        # cannot be sent to a multimodal provider as an image.
        head = await storage.read_range(key, 0, 31)
        image_mime = sniff_image_mime_bytes(head)
        if image_mime:
            item["kind"] = "image"
            item["mime_type"] = image_mime
        else:
            item["kind"] = infer_attachment_kind(item["display_name"])
            if item["kind"] == "image":
                item["kind"] = "file"
            guessed = str(mimetypes.guess_type(item["display_name"])[0] or "").lower()
            if guessed and not guessed.startswith("image/"):
                item["mime_type"] = guessed
            else:
                item.pop("mime_type", None)
    return normalized
