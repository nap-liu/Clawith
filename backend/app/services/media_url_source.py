"""Safe URL validation and agent-local managed media imports."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

import aiofiles
import httpx

from app.services.chat_attachments import MEDIA_PROBE_CHUNK_BYTES, sniff_media_mime_bytes
from app.services.tool_result_paths import tool_result_session_dir

MAX_MEDIA_URL_LENGTH = 4096
MAX_MEDIA_REDIRECTS = 3
MEDIA_URL_TOTAL_TIMEOUT_SECONDS = 120
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class MediaUrlError(Exception):
    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        actual_kind: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.actual_kind = actual_kind


@dataclass(frozen=True)
class ManagedMediaImport:
    file_path: Path
    workspace_path: str
    source_url: str
    mime_type: str


@dataclass(frozen=True)
class _ManagedRequestTarget:
    canonical_url: str
    connect_urls: tuple[str, ...]
    host_header: str
    sni_hostname: str


def _safe_filename(url: str) -> str:
    try:
        raw = unquote(PurePosixPath(urlsplit(url).path).name).strip()
    except ValueError:
        return "remote-media"
    clean = _SAFE_FILENAME_RE.sub("_", raw).strip(" ._")
    if not clean:
        return "remote-media"
    return clean[:160]


def _intent_key(intent_id: str) -> str:
    return hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:24]


async def _resolve_host(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except (OSError, socket.gaierror):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos if info[4]))


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _validated_url_parts(
    raw_url: str,
    *,
    external: bool,
) -> tuple[str, SplitResult, str, int]:
    url = str(raw_url or "").strip()
    if not url or len(url) > MAX_MEDIA_URL_LENGTH:
        raise MediaUrlError("INVALID_MEDIA_URL")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise MediaUrlError("INVALID_MEDIA_URL") from exc
    allowed_schemes = {"https"} if external else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        raise MediaUrlError("INVALID_MEDIA_URL")
    if parsed.username or parsed.password:
        raise MediaUrlError("INVALID_MEDIA_URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise MediaUrlError("MEDIA_URL_FORBIDDEN_TARGET")
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise MediaUrlError("MEDIA_URL_FORBIDDEN_TARGET")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise MediaUrlError("INVALID_MEDIA_URL") from exc
    port = parsed_port if parsed_port is not None else (
        443 if parsed.scheme.lower() == "https" else 80
    )
    if port <= 0:
        raise MediaUrlError("INVALID_MEDIA_URL")
    return url, parsed, hostname, port


async def validate_media_url(raw_url: str, *, external: bool) -> str:
    """Validate one URL without fetching its content."""
    url, _parsed, hostname, port = _validated_url_parts(raw_url, external=external)
    # External mode is a browser-facing reference only: the platform does not
    # resolve or fetch the third-party host. Managed mode performs strict DNS
    # checks before every request and redirect below.
    if external:
        return url
    addresses = await _resolve_host(hostname, port)
    if not addresses:
        raise MediaUrlError("MEDIA_URL_DNS_FAILED")
    if any(not _is_public_ip(address) for address in addresses):
        raise MediaUrlError("MEDIA_URL_FORBIDDEN_TARGET")
    return url


async def _managed_request_target(raw_url: str) -> _ManagedRequestTarget:
    """Validate and pin the current public DNS snapshot for one request hop."""
    canonical_url, parsed, hostname, port = _validated_url_parts(
        raw_url,
        external=False,
    )
    addresses = await _resolve_host(hostname, port)
    if not addresses:
        raise MediaUrlError("MEDIA_URL_DNS_FAILED")
    public_addresses = [address for address in addresses if _is_public_ip(address)]
    if len(public_addresses) != len(addresses):
        raise MediaUrlError("MEDIA_URL_FORBIDDEN_TARGET")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    connect_urls = []
    for address in public_addresses:
        ip_host = f"[{address}]" if ":" in address else address
        connect_netloc = ip_host if port == default_port else f"{ip_host}:{port}"
        connect_urls.append(urlunsplit((
            parsed.scheme,
            connect_netloc,
            parsed.path,
            parsed.query,
            "",
        )))
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    host_header = authority_host if port == default_port else f"{authority_host}:{port}"
    return _ManagedRequestTarget(
        canonical_url,
        tuple(connect_urls),
        host_header,
        hostname,
    )


def _managed_media_paths(
    agent_workspace: Path,
    session_id: str | None,
    intent_id: str,
    filename: str,
) -> tuple[Path, Path, str]:
    agent_root = agent_workspace.resolve()
    result_session_dir = agent_workspace / Path(tool_result_session_dir(session_id))
    staging_dir = result_session_dir / ".media"
    media_dir = agent_workspace / "media"
    imported_dir = media_dir / "imported"
    guarded_dirs = (
        agent_workspace / ".tool_results",
        result_session_dir,
        staging_dir,
        media_dir,
        imported_dir,
    )
    try:
        for directory in guarded_dirs:
            if directory.is_symlink():
                raise ValueError("managed media directory is a symlink")
            directory.resolve().relative_to(agent_root)
    except (OSError, ValueError) as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        imported_dir.mkdir(parents=True, exist_ok=True)
        # Re-check after creation so a raced path swap cannot silently escape.
        for directory in guarded_dirs:
            if directory.is_symlink():
                raise ValueError("managed media directory became a symlink")
            directory.resolve().relative_to(agent_root)
    except (OSError, ValueError) as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    key = _intent_key(intent_id)
    partial_path = (staging_dir / f"{key}.{uuid.uuid4().hex}.partial").resolve()
    final_path = (imported_dir / f"{key}-{filename}").resolve()
    try:
        partial_path.relative_to(agent_root)
        final_path.relative_to(agent_root)
        if partial_path.is_symlink() or final_path.is_symlink():
            raise ValueError("managed media file is a symlink")
        workspace_path = final_path.relative_to(agent_root).as_posix()
    except (OSError, ValueError) as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    return partial_path, final_path, workspace_path


def _sniff_file_mime(path: Path) -> str | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= MEDIA_PROBE_CHUNK_BYTES * 2:
                probe = handle.read()
            else:
                head = handle.read(MEDIA_PROBE_CHUNK_BYTES)
                handle.seek(max(0, size - MEDIA_PROBE_CHUNK_BYTES))
                probe = head + handle.read(MEDIA_PROBE_CHUNK_BYTES)
    except OSError:
        return None
    return sniff_media_mime_bytes(probe, path.name)


def _validated_media_mime(path: Path, expected_media_kind: str) -> str:
    mime_type = _sniff_file_mime(path)
    actual_kind = mime_type.split("/", 1)[0] if mime_type else None
    if actual_kind != expected_media_kind:
        raise MediaUrlError("MEDIA_KIND_MISMATCH", actual_kind=actual_kind)
    return mime_type


async def import_managed_media_url(
    raw_url: str,
    *,
    agent_workspace: Path,
    session_id: str | None,
    intent_id: str,
    max_bytes: int,
    expected_media_kind: str,
) -> ManagedMediaImport:
    """Stream a public URL into this Agent's managed media store atomically."""
    current_url = str(raw_url or "").strip()
    _validated_url_parts(current_url, external=False)
    filename = _safe_filename(current_url)
    partial_path, final_path, workspace_path = _managed_media_paths(
        agent_workspace,
        session_id,
        intent_id,
        filename,
    )
    if final_path.is_file():
        if final_path.is_symlink():
            raise MediaUrlError("MEDIA_STORAGE_FAILED")
        mime_type = _validated_media_mime(final_path, expected_media_kind)
        return ManagedMediaImport(final_path, workspace_path, current_url, mime_type)
    partial_path.unlink(missing_ok=True)

    timeout = httpx.Timeout(connect=10, read=60, write=10, pool=10)
    try:
        async with asyncio.timeout(MEDIA_URL_TOTAL_TIMEOUT_SECONDS):
            for redirect_index in range(MAX_MEDIA_REDIRECTS + 1):
                target = await _managed_request_target(current_url)
                current_url = target.canonical_url
                redirect_url: str | None = None
                last_transport_error: httpx.TransportError | None = None
                for connect_url in target.connect_urls:
                    # A fresh client per candidate IP prevents TLS connection
                    # reuse across redirect hostnames that resolve to the same IP.
                    async with httpx.AsyncClient(
                        follow_redirects=False,
                        timeout=timeout,
                        trust_env=False,
                        verify=True,
                    ) as client:
                        try:
                            response_context = client.stream(
                                "GET",
                                connect_url,
                                headers={
                                    "Accept": "audio/*,video/*,application/octet-stream",
                                    "User-Agent": "Clawith-send_media/1.0",
                                    "Host": target.host_header,
                                },
                                extensions={"sni_hostname": target.sni_hostname},
                            )
                            async with response_context as response:
                                if response.status_code in {301, 302, 303, 307, 308}:
                                    if redirect_index >= MAX_MEDIA_REDIRECTS:
                                        raise MediaUrlError("MEDIA_URL_TOO_MANY_REDIRECTS")
                                    location = response.headers.get("location")
                                    if not location:
                                        raise MediaUrlError(
                                            "MEDIA_URL_HTTP_ERROR",
                                            http_status=response.status_code,
                                        )
                                    redirect_url = urljoin(current_url, location)
                                    break
                                if response.status_code < 200 or response.status_code >= 300:
                                    raise MediaUrlError(
                                        "MEDIA_URL_HTTP_ERROR",
                                        http_status=response.status_code,
                                    )
                                raw_length = response.headers.get("content-length")
                                if raw_length:
                                    try:
                                        if int(raw_length) > max_bytes:
                                            raise MediaUrlError("MEDIA_URL_TOO_LARGE")
                                    except ValueError:
                                        pass
                                written = 0
                                async with aiofiles.open(partial_path, "wb") as output:
                                    async for chunk in response.aiter_bytes():
                                        written += len(chunk)
                                        if written > max_bytes:
                                            raise MediaUrlError("MEDIA_URL_TOO_LARGE")
                                        await output.write(chunk)
                                if written == 0:
                                    raise MediaUrlError("MEDIA_URL_EMPTY")
                                mime_type = _validated_media_mime(
                                    partial_path,
                                    expected_media_kind,
                                )
                                try:
                                    # A hard link publishes the fully validated file atomically.
                                    # Concurrent calls use unique partials; only one creates the
                                    # deterministic final path and every loser reuses that winner.
                                    await asyncio.to_thread(os.link, partial_path, final_path)
                                except FileExistsError:
                                    if final_path.is_symlink():
                                        raise MediaUrlError("MEDIA_STORAGE_FAILED")
                                    mime_type = _validated_media_mime(
                                        final_path,
                                        expected_media_kind,
                                    )
                                return ManagedMediaImport(
                                    final_path,
                                    workspace_path,
                                    current_url,
                                    mime_type,
                                )
                        except httpx.TransportError as exc:
                            last_transport_error = exc
                            continue
                if redirect_url is not None:
                    current_url = redirect_url
                    continue
                if last_transport_error is not None:
                    raise last_transport_error
                raise MediaUrlError("MEDIA_URL_FETCH_FAILED")
    except TimeoutError as exc:
        raise MediaUrlError("MEDIA_URL_FETCH_TIMEOUT") from exc
    except httpx.TimeoutException as exc:
        raise MediaUrlError("MEDIA_URL_FETCH_TIMEOUT") from exc
    except httpx.HTTPError as exc:
        raise MediaUrlError("MEDIA_URL_FETCH_FAILED") from exc
    except OSError as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    finally:
        partial_path.unlink(missing_ok=True)

    raise MediaUrlError("MEDIA_URL_FETCH_FAILED")
