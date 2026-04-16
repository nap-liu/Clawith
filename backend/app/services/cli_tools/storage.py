"""Filesystem-backed content-addressed binary storage.

Layout (see spec §5.2):
    <root>/<tenant_key>/<tool_id>/<sha256>.bin

`tenant_key` is either a stringified UUID for tenant-scoped tools or the
literal "_global" for platform-scoped tools.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

logger = logging.getLogger(__name__)


def _dir_size_bytes(path: Path) -> int:
    """Sum file sizes under `path`, tolerating missing entries / races."""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except (FileNotFoundError, OSError):
                continue
    except (FileNotFoundError, OSError):
        return 0
    return total


_ACCEPTED_MAGICS: tuple[bytes, ...] = (
    b"\x7fELF",           # ELF (Linux)
    b"\xfe\xed\xfa\xce",  # Mach-O 32
    b"\xfe\xed\xfa\xcf",  # Mach-O 64
    b"\xce\xfa\xed\xfe",  # Mach-O 32 LE
    b"\xcf\xfa\xed\xfe",  # Mach-O 64 LE
    b"\xca\xfe\xba\xbe",  # Mach-O universal
    b"#!",                # shebang script
)


class MagicNumberError(ValueError):
    """Uploaded bytes do not start with a recognised executable magic number."""


class SizeLimitExceededError(ValueError):
    """Uploaded bytes exceeded the per-call max."""


class BinaryStorage:
    """Write / resolve / list content-addressed binaries under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def write(
        self,
        *,
        tenant_key: str,
        tool_id: str,
        stream: BinaryIO,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> tuple[str, int]:
        """Stream-read `stream`, validate, write. Returns (sha256, size)."""
        target_dir = self.root / tenant_key / tool_id
        target_dir.mkdir(parents=True, exist_ok=True)

        hasher = hashlib.sha256()
        size = 0
        magic_seen = False
        magic_buffer = b""

        fd, tmp_path_str = tempfile.mkstemp(dir=target_dir, suffix=".partial")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise SizeLimitExceededError(f"binary exceeds {max_bytes} bytes")
                    hasher.update(chunk)
                    out.write(chunk)

                    if not magic_seen:
                        magic_buffer = (magic_buffer + chunk)[:8]
                        if len(magic_buffer) >= 4:
                            if not any(magic_buffer.startswith(m) for m in _ACCEPTED_MAGICS):
                                raise MagicNumberError(
                                    f"magic bytes {magic_buffer[:4]!r} not accepted"
                                )
                            magic_seen = True

            if not magic_seen:
                raise MagicNumberError("file too short to identify magic")

            sha = hasher.hexdigest()
            final = target_dir / f"{sha}.bin"
            if final.exists():
                # Content-addressed: identical content already stored; keep perms strict.
                tmp_path.unlink()
            else:
                tmp_path.replace(final)
            final.chmod(
                stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH
                | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )
            return sha, size
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def resolve(self, tenant_key: str, tool_id: str, sha: str) -> Path:
        return self.root / tenant_key / tool_id / f"{sha}.bin"

    def list_shas(self, tenant_key: str, tool_id: str) -> Iterator[str]:
        d = self.root / tenant_key / tool_id
        if not d.is_dir():
            return
        for entry in d.iterdir():
            if entry.suffix == ".bin" and len(entry.stem) == 64:
                yield entry.stem

    def iter_orphans(self, referenced_shas: set[str]) -> Iterator[Path]:
        for tenant_dir in self.root.iterdir():
            if not tenant_dir.is_dir():
                continue
            for tool_dir in tenant_dir.iterdir():
                if not tool_dir.is_dir():
                    continue
                for entry in tool_dir.iterdir():
                    if entry.suffix == ".bin" and entry.stem not in referenced_shas:
                        yield entry

    def delete_orphans(self, orphans: Iterable[Path]) -> int:
        count = 0
        for path in orphans:
            try:
                path.unlink()
                count += 1
            except FileNotFoundError:
                pass
        return count

    def delete_version(self, tenant_key: str, tool_id: str, sha: str) -> int:
        """Hard-delete a single ``<tenant>/<tool>/<sha>.bin``. Returns bytes freed.

        Used by the version-history GC when a binary falls out of the
        retention window (``MAX_RETAINED_VERSIONS``). Safe to call on a
        missing file (returns 0) so callers don't need pre-existence
        checks. Never raises for IO errors — a stuck file is logged and
        picked up by the nightly orphan sweep.
        """
        target = self.root / tenant_key / tool_id / f"{sha}.bin"
        try:
            size = target.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError:
            size = 0
        try:
            target.unlink()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            logger.warning(
                "cli-tools.gc: failed to delete version binary %s: %s", target, exc
            )
            return 0
        return size

    def delete_tool(self, tenant_key: str, tool_id: str) -> int:
        """Hard-delete the `<tenant>/<tool>/` subtree. Returns bytes freed.

        Tolerates a missing directory (returns 0). Never raises for IO errors
        inside the tree; `shutil.rmtree(ignore_errors=True)` swallows those,
        and we log a warning if the directory still exists afterwards so an
        orphaned file can't silently outlive its Tool row.
        """
        target = self.root / tenant_key / tool_id
        if not target.exists():
            return 0
        freed = _dir_size_bytes(target)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            logger.warning(
                "cli-tools.gc: failed to fully remove %s (partial rmtree)", target
            )
        return freed

    def delete_tenant(self, tenant_key: str) -> int:
        """Hard-delete the entire `<tenant>/` subtree. Returns bytes freed."""
        target = self.root / tenant_key
        if not target.exists():
            return 0
        freed = _dir_size_bytes(target)
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            logger.warning(
                "cli-tools.gc: failed to fully remove %s (partial rmtree)", target
            )
        return freed
