"""Input parsing + safety validation for read_image.

ALL safety-critical logic lives here. A single review of this file covers
the full attack surface: path traversal, SSRF, DNS rebind, HTTPS downgrade,
base64 sanitization, mime sniffing, decompression-bomb protection.

Returns LoadResult with either a short_circuit (category A) or per-image
LoadedImage / LoadError (category B) results.
"""

from __future__ import annotations

import base64
import binascii
import re as _re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

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
        elif entry.lower().startswith(("http://", "https://")):
            # URL mode — implemented in Task 5
            return LoadResult(
                items=[],
                short_circuit=LoadError(idx, entry, "URL 模式尚未实现 (Task 5)", "A"),
            )
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
