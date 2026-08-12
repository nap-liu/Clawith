"""Safe URL validation and agent-local managed media imports."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

import aiofiles
import httpx
from loguru import logger

from app.services.chat_attachments import MEDIA_PROBE_CHUNK_BYTES, sniff_media_mime_bytes
from app.services.media_tool_contract import (
    MANAGED_MEDIA_BLOCKED_HEADER_PREFIXES,
    MANAGED_MEDIA_BLOCKED_HEADERS,
)
from app.services.tool_result_paths import tool_result_session_dir

MAX_MEDIA_URL_LENGTH = 4096
MAX_MEDIA_REDIRECTS = 3
MEDIA_URL_TOTAL_TIMEOUT_SECONDS = 120
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HTTP_HEADER_VALUE_RE = re.compile(r"^[\t\x20-\x7e]*$")
_BROWSER_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


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


@dataclass
class ManagedMediaImport:
    file_path: Path
    workspace_path: str
    source_url: str
    mime_type: str
    _delivery_dir: Path | None = field(default=None, repr=False)

    def close(self) -> None:
        delivery_dir = self._delivery_dir
        self._delivery_dir = None
        if delivery_dir is not None:
            shutil.rmtree(delivery_dir, ignore_errors=True)

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True)
class _ManagedRequestTarget:
    canonical_url: str
    connect_urls: tuple[str, ...]
    host_header: str
    sni_hostname: str


@dataclass
class _ManagedMediaPaths:
    final_path: Path
    workspace_path: str
    partial_name: str
    final_name: str
    staging_fd: int
    imported_fd: int

    def close(self) -> None:
        for attribute in ("staging_fd", "imported_fd"):
            fd = getattr(self, attribute)
            if fd < 0:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
            setattr(self, attribute, -1)


def normalize_managed_media_headers(raw_headers: object) -> dict[str, str]:
    """Validate managed-download header syntax and preserve caller input."""
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, dict):
        raise MediaUrlError("INVALID_MEDIA_HEADERS")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise MediaUrlError("INVALID_MEDIA_HEADERS")
        name = raw_name
        if (
            not name
            or not _HTTP_HEADER_NAME_RE.fullmatch(name)
            or not _HTTP_HEADER_VALUE_RE.fullmatch(raw_value)
        ):
            raise MediaUrlError("INVALID_MEDIA_HEADERS")
        normalized[name] = raw_value
    return normalized


def _managed_request_headers(
    custom_headers: dict[str, str],
) -> dict[str, str]:
    headers = dict(_BROWSER_REQUEST_HEADERS)
    for name, value in custom_headers.items():
        existing = next(
            (candidate for candidate in headers if candidate.lower() == name.lower()),
            None,
        )
        if existing is not None:
            headers.pop(existing)
        headers[name] = value
    return headers


def _finalize_managed_request_headers(
    headers: httpx.Headers,
    host_header: str,
) -> None:
    """Remove caller and client defaults that must not reach the origin."""
    for name in list(headers.keys()):
        lowered = name.lower()
        if (
            lowered in MANAGED_MEDIA_BLOCKED_HEADERS
            or any(
                lowered.startswith(prefix)
                for prefix in MANAGED_MEDIA_BLOCKED_HEADER_PREFIXES
            )
        ):
            del headers[name]
    # Host is always reconstructed from the validated request target. Caller
    # input cannot alter DNS pinning, redirect validation, or TLS identity.
    headers["Host"] = host_header


def _raw_header_pairs(headers: httpx.Headers) -> list[list[str]]:
    return [
        [name.decode("ascii"), value.decode("latin-1")]
        for name, value in headers.raw
    ]


def _log_managed_http_event(event: str, **details: object) -> None:
    logger.info(
        "[ManagedMediaHTTP] {}",
        json.dumps(
            {"event": event, **details},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
    )


def _safe_filename(url: str) -> str:
    try:
        raw = unquote(PurePosixPath(urlsplit(url).path).name).strip()
    except ValueError:
        return "remote-media"
    clean = _SAFE_FILENAME_RE.sub("_", raw).strip(" ._")
    if not clean:
        return "remote-media"
    return clean[:160]


def _intent_key(
    *,
    session_id: str | None,
    intent_id: str,
    operation_scope: str | None,
) -> str:
    identity = operation_scope
    if not identity:
        intent_scope = intent_id or uuid.uuid4().hex
        identity = f"session={session_id or 'nosession'}\0intent={intent_scope}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


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


def _open_managed_dir(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )


def _managed_media_paths(
    agent_workspace: Path,
    session_id: str | None,
    intent_id: str,
    filename: str,
    operation_scope: str | None,
) -> _ManagedMediaPaths:
    agent_root = agent_workspace.resolve()
    result_session_path = tool_result_session_dir(session_id)
    media_dir = agent_root / "media"
    imported_dir = media_dir / "imported"
    root_fd: int | None = None
    tool_results_fd: int | None = None
    session_fd: int | None = None
    staging_fd: int | None = None
    media_fd: int | None = None
    imported_fd: int | None = None
    success = False
    try:
        root_fd = os.open(agent_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        tool_results_fd = _open_managed_dir(root_fd, ".tool_results")
        session_fd = _open_managed_dir(tool_results_fd, result_session_path.name)
        staging_fd = _open_managed_dir(session_fd, ".media")
        media_fd = _open_managed_dir(root_fd, "media")
        imported_fd = _open_managed_dir(media_fd, "imported")
        key = _intent_key(
            session_id=session_id,
            intent_id=intent_id,
            operation_scope=operation_scope,
        )
        partial_name = f"{key}.{uuid.uuid4().hex}.partial"
        final_name = f"{key}-{filename}"
        final_path = imported_dir / final_name
        workspace_path = (PurePosixPath("media") / "imported" / final_name).as_posix()
        success = True
        return _ManagedMediaPaths(
            final_path=final_path,
            workspace_path=workspace_path,
            partial_name=partial_name,
            final_name=final_name,
            staging_fd=staging_fd,
            imported_fd=imported_fd,
        )
    except (OSError, ValueError) as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    finally:
        for fd in (root_fd, tool_results_fd, session_fd, media_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if not success:
            for fd in (staging_fd, imported_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


def _sniff_file_mime_fd(fd: int, name: str) -> str | None:
    try:
        size = os.fstat(fd).st_size
        if size <= MEDIA_PROBE_CHUNK_BYTES * 2:
            probe = os.pread(fd, size, 0)
        else:
            head = os.pread(fd, MEDIA_PROBE_CHUNK_BYTES, 0)
            tail = os.pread(
                fd,
                MEDIA_PROBE_CHUNK_BYTES,
                max(0, size - MEDIA_PROBE_CHUNK_BYTES),
            )
            probe = head + tail
    except OSError:
        return None
    return sniff_media_mime_bytes(probe, name)


def _validated_media_mime_fd(
    fd: int,
    name: str,
    expected_media_kind: str,
    max_bytes: int,
) -> str:
    try:
        size = os.fstat(fd).st_size
    except OSError as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    if size == 0:
        raise MediaUrlError("MEDIA_URL_EMPTY")
    if size > max_bytes:
        raise MediaUrlError("MEDIA_URL_TOO_LARGE")
    mime_type = _sniff_file_mime_fd(fd, name)
    actual_kind = mime_type.split("/", 1)[0] if mime_type else None
    if actual_kind != expected_media_kind:
        raise MediaUrlError("MEDIA_KIND_MISMATCH", actual_kind=actual_kind)
    return mime_type


def _unlink_partial(paths: _ManagedMediaPaths) -> None:
    try:
        os.unlink(paths.partial_name, dir_fd=paths.staging_fd)
    except FileNotFoundError:
        pass


def _verify_visible_final(paths: _ManagedMediaPaths, final_fd: int) -> None:
    try:
        anchored_dir = os.fstat(paths.imported_fd)
        visible_dir = os.stat(paths.final_path.parent, follow_symlinks=False)
        anchored_file = os.fstat(final_fd)
        visible_file = os.stat(paths.final_path, follow_symlinks=False)
    except OSError as exc:
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    if (
        (anchored_dir.st_dev, anchored_dir.st_ino)
        != (visible_dir.st_dev, visible_dir.st_ino)
        or (anchored_file.st_dev, anchored_file.st_ino)
        != (visible_file.st_dev, visible_file.st_ino)
    ):
        raise MediaUrlError("MEDIA_STORAGE_FAILED")


def _copy_delivery_file(final_fd: int, final_name: str, delivery_dir: Path) -> Path:
    delivery_path = delivery_dir / final_name
    read_fd = os.dup(final_fd)
    with os.fdopen(read_fd, "rb") as source, delivery_path.open("xb") as target:
        shutil.copyfileobj(source, target)
    return delivery_path


async def _managed_import_result(
    *,
    paths: _ManagedMediaPaths,
    final_fd: int,
    source_url: str,
    expected_media_kind: str,
    max_bytes: int,
) -> ManagedMediaImport:
    _verify_visible_final(paths, final_fd)
    delivery_dir: Path | None = None
    try:
        delivery_dir = Path(tempfile.mkdtemp(prefix="clawith-media-delivery-"))
        delivery_path = await asyncio.to_thread(
            _copy_delivery_file,
            final_fd,
            paths.final_name,
            delivery_dir,
        )
        delivery_fd = os.open(delivery_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            mime_type = _validated_media_mime_fd(
                delivery_fd,
                paths.final_name,
                expected_media_kind,
                max_bytes,
            )
        finally:
            os.close(delivery_fd)
    except BaseException as exc:
        if delivery_dir is not None:
            shutil.rmtree(delivery_dir, ignore_errors=True)
        if isinstance(exc, OSError):
            raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
        raise
    return ManagedMediaImport(
        file_path=delivery_path,
        workspace_path=paths.workspace_path,
        source_url=source_url,
        mime_type=mime_type,
        _delivery_dir=delivery_dir,
    )


async def import_managed_media_url(
    raw_url: str,
    *,
    agent_workspace: Path,
    session_id: str | None,
    intent_id: str,
    max_bytes: int,
    expected_media_kind: str,
    operation_scope: str | None = None,
    request_headers: dict[str, str] | None = None,
) -> ManagedMediaImport:
    """Stream a public URL into this Agent's managed media store atomically."""
    current_url = str(raw_url or "").strip()
    _validated_url_parts(current_url, external=False)
    custom_headers = normalize_managed_media_headers(request_headers)
    filename = _safe_filename(current_url)
    paths = _managed_media_paths(
        agent_workspace,
        session_id,
        intent_id,
        filename,
        operation_scope,
    )
    try:
        existing_fd = os.open(
            paths.final_name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=paths.imported_fd,
        )
    except FileNotFoundError:
        existing_fd = None
    except OSError as exc:
        paths.close()
        raise MediaUrlError("MEDIA_STORAGE_FAILED") from exc
    if existing_fd is not None:
        try:
            _validated_media_mime_fd(
                existing_fd,
                paths.final_name,
                expected_media_kind,
                max_bytes,
            )
            return await _managed_import_result(
                paths=paths,
                final_fd=existing_fd,
                source_url=current_url,
                expected_media_kind=expected_media_kind,
                max_bytes=max_bytes,
            )
        finally:
            os.close(existing_fd)
            paths.close()

    try:
        timeout = httpx.Timeout(connect=10, read=60, write=10, pool=10)
        async with asyncio.timeout(MEDIA_URL_TOTAL_TIMEOUT_SECONDS):
            for redirect_index in range(MAX_MEDIA_REDIRECTS + 1):
                target = await _managed_request_target(current_url)
                current_url = target.canonical_url
                redirect_url: str | None = None
                last_transport_error: httpx.TransportError | None = None
                for connect_index, connect_url in enumerate(target.connect_urls):
                    # A fresh client per candidate IP prevents TLS connection
                    # reuse across redirect hostnames that resolve to the same IP.
                    async with httpx.AsyncClient(
                        follow_redirects=False,
                        timeout=timeout,
                        trust_env=False,
                        verify=True,
                    ) as client:
                        request = client.build_request(
                            "GET",
                            connect_url,
                            headers=_managed_request_headers(custom_headers),
                            extensions={"sni_hostname": target.sni_hostname},
                        )
                        _finalize_managed_request_headers(
                            request.headers,
                            target.host_header,
                        )
                        request_started = time.perf_counter()
                        request_log = {
                            "method": request.method,
                            "url": current_url,
                            "request_url": str(request.url),
                            "connect_ip": urlsplit(connect_url).hostname or "",
                            "sni_hostname": target.sni_hostname,
                            "headers": _raw_header_pairs(request.headers),
                            "body": "",
                            "redirect_index": redirect_index,
                            "connect_index": connect_index,
                        }
                        _log_managed_http_event(
                            "managed_media_http_request",
                            **request_log,
                        )
                        try:
                            response = await client.send(request, stream=True)
                            _log_managed_http_event(
                                "managed_media_http_response",
                                **request_log,
                                status_code=response.status_code,
                                response_headers=_raw_header_pairs(response.headers),
                                elapsed_ms=round(
                                    (time.perf_counter() - request_started) * 1000,
                                    3,
                                ),
                            )
                            try:
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
                                _unlink_partial(paths)
                                partial_fd = os.open(
                                    paths.partial_name,
                                    os.O_WRONLY
                                    | os.O_CREAT
                                    | os.O_EXCL
                                    | os.O_NOFOLLOW,
                                    0o600,
                                    dir_fd=paths.staging_fd,
                                )
                                async with aiofiles.open(
                                    partial_fd,
                                    "wb",
                                    closefd=True,
                                ) as output:
                                    async for chunk in response.aiter_bytes():
                                        written += len(chunk)
                                        if written > max_bytes:
                                            raise MediaUrlError("MEDIA_URL_TOO_LARGE")
                                        await output.write(chunk)
                                if written == 0:
                                    raise MediaUrlError("MEDIA_URL_EMPTY")
                                partial_read_fd = os.open(
                                    paths.partial_name,
                                    os.O_RDONLY | os.O_NOFOLLOW,
                                    dir_fd=paths.staging_fd,
                                )
                                try:
                                    _validated_media_mime_fd(
                                        partial_read_fd,
                                        paths.partial_name,
                                        expected_media_kind,
                                        max_bytes,
                                    )
                                    try:
                                        # dir_fd anchors both sides even if an Agent renames or
                                        # replaces the visible directories during the download.
                                        await asyncio.to_thread(
                                            os.link,
                                            paths.partial_name,
                                            paths.final_name,
                                            src_dir_fd=paths.staging_fd,
                                            dst_dir_fd=paths.imported_fd,
                                            follow_symlinks=False,
                                        )
                                    except FileExistsError:
                                        pass
                                finally:
                                    os.close(partial_read_fd)
                                final_fd = os.open(
                                    paths.final_name,
                                    os.O_RDONLY | os.O_NOFOLLOW,
                                    dir_fd=paths.imported_fd,
                                )
                                try:
                                    _validated_media_mime_fd(
                                        final_fd,
                                        paths.final_name,
                                        expected_media_kind,
                                        max_bytes,
                                    )
                                    return await _managed_import_result(
                                        paths=paths,
                                        final_fd=final_fd,
                                        source_url=current_url,
                                        expected_media_kind=expected_media_kind,
                                        max_bytes=max_bytes,
                                    )
                                finally:
                                    os.close(final_fd)
                            finally:
                                await response.aclose()
                        except httpx.TransportError as exc:
                            _log_managed_http_event(
                                "managed_media_http_error",
                                **request_log,
                                error_type=type(exc).__name__,
                                error=str(exc),
                                elapsed_ms=round(
                                    (time.perf_counter() - request_started) * 1000,
                                    3,
                                ),
                            )
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
        try:
            _unlink_partial(paths)
        except OSError:
            pass
        paths.close()

    raise MediaUrlError("MEDIA_URL_FETCH_FAILED")
