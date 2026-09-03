"""Project Agent-relative Markdown images into transient IM delivery URLs."""

from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from loguru import logger

from app.database import async_session
from app.services.chat_attachments import sniff_image_mime_bytes
from app.services.platform_service import platform_service
from app.services.storage import agent_storage_key, get_storage_backend

_MARKDOWN_IMAGE_RE = re.compile(
    r"(?P<prefix>!\[(?:\\.|[^\]\\])*\]\(\s*)"
    r"(?P<destination><[^>\r\n]+>|[^\s)\r\n]+)"
    r"(?P<suffix>(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\r\n)]*\)))?\s*\))"
)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<run>`{3,}|~{3,})")
_BACKTICK_RUN_RE = re.compile(r"`+")
_MAX_FAILURE_LOGS_PER_MESSAGE = 3


def _code_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return fenced and inline code ranges that Markdown images must not enter."""
    ranges: list[tuple[int, int]] = []
    fence_start: int | None = None
    fence_char = ""
    fence_length = 0
    offset = 0
    for line in markdown.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if fence_start is None and match:
            run = match.group("run")
            fence_start, fence_char, fence_length = offset, run[0], len(run)
        elif fence_start is not None and match:
            run = match.group("run")
            remainder = line[match.end() :].strip()
            if run[0] == fence_char and len(run) >= fence_length and not remainder:
                ranges.append((fence_start, offset + len(line)))
                fence_start = None
        offset += len(line)
    if fence_start is not None:
        ranges.append((fence_start, len(markdown)))

    cursor = 0
    for fenced_start, fenced_end in ranges + [(len(markdown), len(markdown))]:
        segment = markdown[cursor:fenced_start]
        runs = list(_BACKTICK_RUN_RE.finditer(segment))
        index = 0
        while index < len(runs):
            opener = runs[index]
            closer_index = index + 1
            while closer_index < len(runs) and len(runs[closer_index].group()) != len(opener.group()):
                closer_index += 1
            if closer_index < len(runs):
                ranges.append((cursor + opener.start(), cursor + runs[closer_index].end()))
                index = closer_index + 1
            else:
                index += 1
        cursor = fenced_end
    return sorted(ranges)


def _overlaps_code(start: int, end: int, code_ranges: list[tuple[int, int]]) -> bool:
    return any(start < code_end and end > code_start for code_start, code_end in code_ranges)


def _canonical_agent_path(raw_destination: str) -> str | None:
    destination = raw_destination[1:-1] if raw_destination.startswith("<") else raw_destination
    destination = unicodedata.normalize("NFC", unquote(destination)).strip()
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    if not destination or destination.startswith(("/", "\\")) or "\\" in destination:
        return None
    parts = destination.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    canonical = PurePosixPath(*parts).as_posix()
    if canonical in {"", "."}:
        return None
    return canonical


async def _public_base_url() -> str:
    async with async_session() as db:
        return (await platform_service.get_public_base_url(db=db)).rstrip("/")


async def _presign_agent_image(agent_id: uuid.UUID, relative_path: str) -> str | None:
    storage = get_storage_backend()
    key = agent_storage_key(agent_id, relative_path)
    if not await storage.is_file(key):
        return None
    mime_type = sniff_image_mime_bytes(await storage.read_range(key, 0, 511))
    if mime_type is None:
        return None
    url = await storage.presign_download_url(
        key,
        filename=PurePosixPath(relative_path).name,
        inline=True,
        content_type=mime_type,
    )
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    if not url.startswith("/") or url.startswith("//"):
        return None
    return urljoin(f"{await _public_base_url()}/", url.lstrip("/"))


async def project_agent_images_for_im(agent_id: uuid.UUID, markdown: str) -> str:
    """Replace safe Agent-relative image destinations only for one IM send."""
    code_ranges = _code_ranges(markdown or "")
    matches = [
        match
        for match in _MARKDOWN_IMAGE_RE.finditer(markdown or "")
        if not _overlaps_code(match.start(), match.end(), code_ranges)
    ]
    if not matches:
        return markdown

    replacements: dict[str, str | None] = {}
    match_paths: list[str | None] = []
    failure_logs = 0
    for match in matches:
        raw_destination = match.group("destination")
        try:
            path = _canonical_agent_path(raw_destination)
        except Exception as exc:
            path = None
            if failure_logs < _MAX_FAILURE_LOGS_PER_MESSAGE:
                logger.warning(
                    "[im_markdown_media] invalid image reference agent={} error_type={}",
                    str(agent_id)[:8],
                    type(exc).__name__,
                )
                failure_logs += 1
        match_paths.append(path)
        if path is None or path in replacements:
            continue
        try:
            replacements[path] = await _presign_agent_image(agent_id, path)
        except Exception as exc:
            replacements[path] = None
            if failure_logs < _MAX_FAILURE_LOGS_PER_MESSAGE:
                logger.warning(
                    "[im_markdown_media] projection skipped agent={} file={} error_type={}",
                    str(agent_id)[:8],
                    PurePosixPath(path).name[:80],
                    type(exc).__name__,
                )
                failure_logs += 1

    rendered: list[str] = []
    cursor = 0
    for match, path in zip(matches, match_paths, strict=True):
        rendered.append(markdown[cursor : match.start()])
        raw_destination = match.group("destination")
        signed_url = replacements.get(path or "")
        if signed_url:
            destination = f"<{signed_url}>" if raw_destination.startswith("<") else signed_url
            rendered.append(f"{match.group('prefix')}{destination}{match.group('suffix')}")
        else:
            rendered.append(match.group(0))
        cursor = match.end()
    rendered.append(markdown[cursor:])
    return "".join(rendered)


__all__ = ["project_agent_images_for_im"]
