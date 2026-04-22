"""Input parsing + safety validation for read_image.

ALL safety-critical logic lives here. A single review of this file covers
the full attack surface: path traversal, SSRF, DNS rebind, HTTPS downgrade,
base64 sanitization, mime sniffing, decompression-bomb protection.

Returns LoadResult with either a short_circuit (category A) or per-image
LoadedImage / LoadError (category B) results.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fnmatch
import ipaddress
import re as _re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union
from urllib.parse import urlparse

import httpx
from loguru import logger
from PIL import Image

# ─── Global Pillow hardening ─────────────────────────────────────────────────
# Process-wide setting; caps decompression-bomb attacks. Safe for all callers
# including vision_inject.py (screenshots above 40 MP are implausible).
Image.MAX_IMAGE_PIXELS = 40_000_000


DEFAULT_CONFIG: dict = {
    "model_id": None,
    "fallback_model_id": None,
    "input_modes": {
        "workspace_path": {"enabled": True},
        "url": {
            "enabled": False,
            "allowlist": [],
            "fetch_timeout_seconds": 10,
            "max_redirects": 3,
        },
        "base64": {
            "enabled": False,
            "max_bytes": 1048576,
        },
    },
    "max_images_per_call": 6,
    "max_image_bytes_per_file": 5242880,
    "image_compression": {
        "max_width": 1920,
        "jpeg_quality": 85,
    },
    "vision_call_timeout_seconds": 90,
    "vision_max_output_tokens": 4096,
}


@dataclass(frozen=True)
class LoadedImage:
    display_ref: str
    data_url: str


@dataclass(frozen=True)
class LoadError:
    input_index: int
    display_ref: str
    reason: str
    category: Literal["A", "B"]


@dataclass
class LoadResult:
    items: list[Union[LoadedImage, LoadError]]
    short_circuit: LoadError | None = None


# ─── Mime magic-number sniff ─────────────────────────────────────────────────

def _sniff_mime(data: bytes) -> str | None:
    """Return 'jpeg' | 'png' | 'webp' | 'gif' or None."""
    if len(data) < 16:
        return None
    if data[:3] == b"\xFF\xD8\xFF":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


# ─── Compression wrapper (reuses vision_inject) ──────────────────────────────

def _compress_to_data_url(raw: bytes) -> str | None:
    """Compress and base64-encode, returning a data URL, or None on failure.

    Security note: decompression-bomb protection comes from the module-level
    `Image.MAX_IMAGE_PIXELS = 40_000_000` setting, which Pillow enforces inside
    `compress_bytes_to_base64` (from app.services.vision_inject). That helper
    currently swallows all exceptions and returns None, so both specific
    `DecompressionBombError` and generic failures collapse to the same `None`
    outcome here. The specific catch below is belt-and-suspenders in case
    vision_inject is ever refactored to re-raise.
    """
    try:
        from app.services.vision_inject import compress_bytes_to_base64
        return compress_bytes_to_base64(raw)
    except Image.DecompressionBombError:
        logger.warning("[read_image] decompression bomb rejected")
        return None
    except Exception as e:
        logger.warning(f"[read_image] compression failed: {e}")
        return None


# ─── Workspace path mode ─────────────────────────────────────────────────────

def _load_workspace_path(
    rel_path: str, workspace: Path, config: dict, input_index: int
) -> Union[LoadedImage, LoadError]:
    """Load a workspace-relative image. Symlinks and path-escape are category A."""
    ws_resolved = workspace.resolve()

    # Symlink rejection — checked BEFORE resolve() so the reason message
    # correctly identifies the symlink (rather than the traversal it enables).
    pre_resolved = workspace / rel_path
    if pre_resolved.is_symlink():
        return LoadError(input_index, rel_path, "路径指向 symlink (rejected)", "A")

    try:
        candidate = (workspace / rel_path).resolve()
    except (OSError, ValueError) as e:
        return LoadError(input_index, rel_path, f"路径解析失败: {e}", "A")

    # Path traversal guard — use Path.relative_to for OS-safe component-wise check.
    try:
        candidate.relative_to(ws_resolved)
    except ValueError:
        return LoadError(input_index, rel_path, "路径越界 (path escape)", "A")

    # File existence (category B inline)
    if not candidate.exists() or not candidate.is_file():
        return LoadError(input_index, rel_path, "文件不存在或不可读", "B")

    # Size cap
    size = candidate.stat().st_size
    max_bytes = config.get("max_image_bytes_per_file", 5242880)
    if size > max_bytes:
        return LoadError(input_index, rel_path, f"超过单图大小上限 ({size} > {max_bytes})", "B")

    # Read + mime sniff
    # TODO(Task 5+): wrap sync I/O in asyncio.to_thread when URL mode lands —
    # currently OK for workspace mode at MVP scale.
    try:
        raw = candidate.read_bytes()
    except OSError as e:
        return LoadError(input_index, rel_path, f"读取失败: {e}", "B")

    mime = _sniff_mime(raw)
    if mime is None:
        return LoadError(input_index, rel_path, "不是有效的图片 (jpeg/png/webp/gif)", "B")

    data_url = _compress_to_data_url(raw)
    if data_url is None:
        return LoadError(input_index, rel_path, "图片解码失败", "B")

    return LoadedImage(display_ref=rel_path, data_url=data_url)


# ─── Base64 mode ─────────────────────────────────────────────────────────────

_DATA_URI_PREFIX_RE = _re.compile(
    r"^data:image/(jpeg|png|webp|gif);base64,",
    _re.IGNORECASE,
)


def _load_base64(
    data_uri: str, config: dict, input_index: int, ordinal: int
) -> Union[LoadedImage, LoadError]:
    """Decode, size-cap, and mime-sniff a data:image/*;base64,… URL."""
    mode_cfg = config["input_modes"]["base64"]

    # Never put the payload in display_ref.
    def _display_ref(size_kb: int | None = None) -> str:
        if size_kb is None:
            return f"[base64 image #{ordinal}]"
        return f"[base64 image #{ordinal}, {size_kb} KB]"

    # Strict-prefix check
    m = _DATA_URI_PREFIX_RE.match(data_uri)
    if not m:
        return LoadError(input_index, _display_ref(), "base64 data URI 格式不符", "B")

    payload = data_uri[m.end():]
    # Size estimate before decode
    approx_bytes = (len(payload) * 3) // 4
    max_bytes = mode_cfg.get("max_bytes", 1048576)
    if approx_bytes > max_bytes:
        return LoadError(
            input_index,
            _display_ref(),
            f"base64 size {approx_bytes} > max {max_bytes}",
            "B",
        )

    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as e:
        return LoadError(input_index, _display_ref(), f"base64 解码失败: {e}", "B")

    # Also enforce the overall per-file cap
    per_file_cap = config.get("max_image_bytes_per_file", 5242880)
    if len(raw) > min(max_bytes, per_file_cap):
        return LoadError(
            input_index,
            _display_ref(len(raw) // 1024),
            f"decoded size {len(raw)} exceeds cap",
            "B",
        )

    if _sniff_mime(raw) is None:
        return LoadError(
            input_index,
            _display_ref(len(raw) // 1024),
            "base64 解码后不是有效图片 (jpeg/png/webp/gif)",
            "B",
        )

    data_url = _compress_to_data_url(raw)
    if data_url is None:
        return LoadError(input_index, _display_ref(len(raw) // 1024), "图片解码失败", "B")

    return LoadedImage(display_ref=_display_ref(len(raw) // 1024), data_url=data_url)


# ─── URL mode ────────────────────────────────────────────────────────────────

class _RedirectToPrivateIP(Exception):
    """Raised when a redirect target resolves to a private/loopback/reserved IP."""


class _RedirectOutsideAllowlist(Exception):
    """Raised when a redirect target's host is outside the admin allowlist."""


class _SchemeDowngrade(Exception):
    """Raised when a redirect changes scheme to something non-http(s)."""


async def _resolve_host(host: str) -> list[str]:
    """Resolve host to a list of IP strings via getaddrinfo.

    Wrapped for monkeypatching in tests. Returns [] on any DNS failure,
    which collapses cleanly to category-A "DNS 解析无结果" refusal upstream.
    """
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError):
        return []
    ips: list[str] = []
    for _family, *_rest, sockaddr in infos:
        ip = sockaddr[0]
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def _is_ip_blocked(ip_str: str) -> bool:
    """True if this IP is private / loopback / link-local / reserved — must refuse."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable ⇒ refuse
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    return any(fnmatch.fnmatch(host.lower(), p.lower()) for p in allowlist)


async def _validate_url_target(url: str, config: dict) -> str:
    """Pre-fetch checks: scheme, allowlist, DNS → private-IP blocklist.
    Returns the validated hostname on success.
    Raises ValueError with a message on refusal.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme {parsed.scheme!r} not in (http, https)")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    mode_cfg = config["input_modes"]["url"]
    if not _host_matches_allowlist(parsed.hostname, mode_cfg.get("allowlist", [])):
        raise ValueError("URL 不在允许列表中")

    ips = await _resolve_host(parsed.hostname)
    if not ips:
        raise ValueError("DNS 解析无结果")
    for ip in ips:
        if _is_ip_blocked(ip):
            raise ValueError(
                f"目标 IP 属于内网/保留段，已拒绝 (SSRF 防护): {ip}"
            )
    return parsed.hostname


async def _fetch_with_revalidation(
    url: str, config: dict
) -> tuple[bytes, str]:
    """Fetch with manual redirect handling — each redirect target is re-resolved
    and re-checked against private-IP blocklist and allowlist. HTTPS verify is
    ALWAYS True. Stream size-capped.

    Returns (bytes, final_url).
    Raises _RedirectToPrivateIP / _RedirectOutsideAllowlist / _SchemeDowngrade
    on redirect-policy failures.
    """
    # TOCTOU note: between _validate_url_target's DNS resolve and httpx's
    # per-request DNS resolve here, a DNS rebind could flip the record.
    # Residual risk is bounded by (a) the admin allowlist (the attacker must
    # first control DNS for an allowlisted domain), and (b) the redirect loop
    # below, which re-runs the full 4-check chain on every hop. Closing this
    # window fully requires direct-IP connect with SNI/Host override, which
    # is documented as spec §9 follow-up item #5.
    mode_cfg = config["input_modes"]["url"]
    timeout = mode_cfg.get("fetch_timeout_seconds", 10)
    max_redirects = mode_cfg.get("max_redirects", 3)
    max_bytes = config.get("max_image_bytes_per_file", 5242880)

    headers = {
        "User-Agent": "Clawith-read_image/1.0",
        "Accept": "image/jpeg,image/png,image/webp,image/gif",
    }

    current_url = url
    budget = max_redirects
    async with httpx.AsyncClient(
        verify=True, timeout=timeout, follow_redirects=False,
    ) as client:
        while True:
            resp = await client.get(current_url, headers=headers)
            if 300 <= resp.status_code < 400:
                if budget <= 0:
                    raise ValueError("redirect budget exhausted")
                loc = resp.headers.get("location")
                if not loc:
                    raise ValueError(f"redirect {resp.status_code} without Location header")
                next_parsed = urlparse(loc)
                if next_parsed.scheme not in ("http", "https"):
                    raise _SchemeDowngrade(f"redirect to non-http scheme {next_parsed.scheme!r}")
                if not _host_matches_allowlist(next_parsed.hostname or "", mode_cfg.get("allowlist", [])):
                    raise _RedirectOutsideAllowlist(f"redirect host {next_parsed.hostname!r} not in allowlist")
                ips = await _resolve_host(next_parsed.hostname)
                if not ips or any(_is_ip_blocked(ip) for ip in ips):
                    raise _RedirectToPrivateIP(f"redirect target {next_parsed.hostname!r} resolved to blocked IP")
                current_url = loc
                budget -= 1
                continue

            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"response size exceeds {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks), current_url


async def _load_url(
    url: str, config: dict, input_index: int
) -> Union[LoadedImage, LoadError]:
    try:
        await _validate_url_target(url, config)
    except ValueError as e:
        return LoadError(input_index, url, str(e), "A")
    try:
        raw, final_url = await _fetch_with_revalidation(url, config)
    except (_RedirectToPrivateIP, _RedirectOutsideAllowlist, _SchemeDowngrade) as e:
        return LoadError(input_index, url, str(e), "A")
    except (httpx.HTTPError, ValueError) as e:
        return LoadError(input_index, url, f"fetch failed: {e}", "B")

    if _sniff_mime(raw) is None:
        return LoadError(input_index, url, "response is not a valid image", "B")

    data_url = _compress_to_data_url(raw)
    if data_url is None:
        return LoadError(input_index, url, "图片解码失败", "B")

    return LoadedImage(display_ref=final_url, data_url=data_url)


# ─── Top-level entry ─────────────────────────────────────────────────────────

async def load(
    image_paths: list[str], workspace: Path, config: dict
) -> LoadResult:
    """Parse, validate, compress all inputs. Short-circuit on category A."""
    # Bound check (category A)
    max_images = config.get("max_images_per_call", 6)
    if len(image_paths) > max_images:
        return LoadResult(
            items=[],
            short_circuit=LoadError(
                -1,
                "",
                f"image_paths 数量超过上限 (got {len(image_paths)}, max {max_images})",
                "A",
            ),
        )
    if len(image_paths) == 0:
        return LoadResult(
            items=[],
            short_circuit=LoadError(-1, "", "image_paths 为空", "A"),
        )

    items: list[Union[LoadedImage, LoadError]] = []
    b64_ordinal = 0
    for idx, entry in enumerate(image_paths):
        entry = entry.strip()
        if entry.lower().startswith("data:"):
            # base64 mode
            if not config["input_modes"]["base64"]["enabled"]:
                return LoadResult(
                    items=[],
                    short_circuit=LoadError(
                        idx, f"[base64 image #{b64_ordinal + 1}]",
                        "base64 输入模式未启用",
                        "A",
                    ),
                )
            b64_ordinal += 1
            result = _load_base64(entry, config, idx, b64_ordinal)
        elif entry.lower().startswith(("http://", "https://")) or entry.lower().startswith(("file://", "gopher://", "ftp://")):
            if not config["input_modes"]["url"]["enabled"]:
                return LoadResult(
                    items=[],
                    short_circuit=LoadError(idx, entry, "URL 输入模式未启用", "A"),
                )
            result = await _load_url(entry, config, idx)
        else:
            # workspace path
            if not config["input_modes"]["workspace_path"]["enabled"]:
                return LoadResult(
                    items=[],
                    short_circuit=LoadError(idx, entry, "workspace_path 模式未启用", "A"),
                )
            result = _load_workspace_path(entry, workspace, config, idx)

        if isinstance(result, LoadError) and result.category == "A":
            return LoadResult(items=[], short_circuit=result)
        items.append(result)

    return LoadResult(items=items)
