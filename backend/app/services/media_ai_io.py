"""URL-first media inputs over AgentDir signing and the shared URL policy."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.services.agent_file_urls import presign_agent_file
from app.services.chat_attachments import sniff_image_mime_bytes, sniff_media_mime_bytes
from app.services.im_markdown_media import _canonical_agent_path
from app.services.media_url_source import _managed_request_target
from app.services.storage import agent_storage_key, get_storage_backend

# Input size is governed by the selected provider. Only metadata probes are bounded.
PROBE_BYTES = 256 * 1024
MAX_RESULT_BYTES = None


class MediaAIError(ValueError):
    def __init__(self, code: str, *, provider_code: str = "", retryable: bool = False):
        self.code = code
        self.provider_code = provider_code[:100]
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class MediaInput:
    source: str
    mime_type: str
    data: bytes = b""
    url: str = ""
    role: str = ""

    @property
    def kind(self) -> str:
        return self.mime_type.split("/", 1)[0]

    @property
    def data_url(self) -> str:
        # Byte inputs remain useful to callers that already hold a small media payload.
        return self.url or f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"


async def _read_url(url: str, *, max_bytes: int | None = None, probe=False) -> tuple[bytes, str]:
    for _ in range(4):
        target = await _managed_request_target(url)
        headers = {"Host": target.host_header}
        if probe:
            headers["Range"] = f"bytes=0-{PROBE_BYTES - 1}"
        async with httpx.AsyncClient(timeout=120, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "GET", target.connect_urls[0], headers=headers,
                extensions={"sni_hostname": target.sni_hostname},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaAIError("downloadFailed")
                    url = urljoin(target.canonical_url, location)
                    continue
                response.raise_for_status()
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise MediaAIError("fileTooLarge")
                    chunks.append(chunk)
                    if probe and size >= PROBE_BYTES:
                        break
                if not size:
                    raise MediaAIError("invalidMedia")
                return b"".join(chunks), response.headers.get("content-type", "").split(";")[0]
    raise MediaAIError("downloadFailed")


async def download_media(url: str, *, max_bytes: int | None = None) -> bytes:
    data, _ = await _read_url(url, max_bytes=max_bytes)
    return data


def media_mime(data: bytes, name: str = "") -> str:
    mime = sniff_image_mime_bytes(data[:2 * 1024 * 1024]) or sniff_media_mime_bytes(data, name)
    if not mime or not mime.startswith(("image/", "audio/", "video/")):
        raise MediaAIError("invalidMedia")
    return mime


def normalize_sources(files: list) -> list[dict]:
    result = []
    for value in files:
        item = dict(value) if isinstance(value, dict) else {"source": value}
        source = item["source"]
        if source.startswith("data:"):
            _data_media(source)
        elif source.startswith(("https://", "http://")):
            parsed = urlsplit(source)
            if not parsed.hostname or parsed.username or parsed.password:
                raise MediaAIError("unsafeUrl")
        else:
            source = _canonical_agent_path(source)
            if source is None:
                raise MediaAIError("invalidPath")
        result.append({**item, "source": source})
    return result


async def load_media(agent_id, files: list) -> list[MediaInput]:
    storage = get_storage_backend()
    result = []
    for item in normalize_sources(files):
        source = item["source"]
        hint = item.get("kind")
        if source.startswith("data:"):
            mime, data = _data_media(source)
            result.append(MediaInput(source, mime, data=data, role=item.get("role", "")))
            continue
        if source.startswith(("https://", "http://")):
            await _managed_request_target(source)
            if hint:
                mime = f"{hint}/unknown"
            else:
                head, mime = await _read_url(source, probe=True)
                if not mime.startswith(("image/", "audio/", "video/")):
                    try:
                        mime = media_mime(head, source)
                    except MediaAIError:
                        mime = mimetypes.guess_type(urlsplit(source).path)[0] or ""
            url = source  # Never rewrite third-party signatures or query parameters.
        else:
            key = agent_storage_key(agent_id, source)
            try:
                info = await storage.stat(key)
            except FileNotFoundError as exc:
                raise MediaAIError("fileUnavailable") from exc
            if info is None or info.is_dir:
                raise MediaAIError("fileUnavailable")
            head = await storage.read_range(key, 0, PROBE_BYTES - 1)
            try:
                mime = media_mime(head, source)
            except MediaAIError:
                tail = await storage.read_range(key, max(0, info.size - PROBE_BYTES), max(0, info.size - 1))
                try:
                    mime = media_mime(head + tail, source)
                except MediaAIError:
                    mime = mimetypes.guess_type(source)[0] or (f"{hint}/unknown" if hint else "")
            url = await presign_agent_file(agent_id, source, mime)
        if not mime.startswith(("image/", "audio/", "video/")):
            raise MediaAIError("invalidMedia")
        result.append(MediaInput(source, mime, url=url, role=item.get("role", "")))
    return result


def _data_media(source: str) -> tuple[str, bytes]:
    try:
        header, encoded = source.split(",", 1)
        if not header.endswith(";base64"):
            raise ValueError("base64 encoding required")
        data = base64.b64decode(encoded, validate=True)
        declared = header[5:-7]
        mime = media_mime(data)
        if declared != mime:
            raise ValueError("media MIME mismatch")
        return mime, data
    except (ValueError, binascii.Error) as exc:
        raise MediaAIError("invalidMedia") from exc


async def load_understanding_media(agent_id, files: list, *, loader=load_media):
    """Keep readable members of a batch and report every failed input explicitly."""
    from app.services.media_url_source import MediaUrlError

    media, failures = [], []
    for index, item in enumerate(files):
        try:
            media.extend(await loader(agent_id, [item]))
        except (MediaAIError, MediaUrlError, httpx.HTTPError, OSError) as exc:
            source = item.get("source", "") if isinstance(item, dict) else item
            failures.append({"index": index + 1, "source": source,
                             "code": exc.code if isinstance(exc, MediaAIError) else "fileUnavailable"})
    if files and not media:
        raise MediaAIError(failures[0]["code"])
    return media, failures
