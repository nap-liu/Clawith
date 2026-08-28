"""Filesystem-backed content-addressed binary storage.

Layout (see spec §5.2):
    <root>/<tenant_key>/<tool_id>/<sha256>.bin

`tenant_key` is either a stringified UUID for tenant-scoped tools or the
literal "_global" for platform-scoped tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

logger = logging.getLogger(__name__)

BINARY_ROOT = Path("/data/cli_binaries")


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

    def _upload_paths(
        self,
        tenant_key: str,
        tool_id: str,
        upload_id: str,
    ) -> tuple[Path, Path]:
        upload_dir = self.root / tenant_key / tool_id / ".uploads"
        return upload_dir / f"{upload_id}.part", upload_dir / f"{upload_id}.json"

    def upload_status(
        self,
        *,
        tenant_key: str,
        tool_id: str,
        upload_id: str,
    ) -> tuple[int, int, str] | None:
        """Return ``(received, total, original_name)`` for a partial upload."""
        part, metadata = self._upload_paths(tenant_key, tool_id, upload_id)
        if not metadata.is_file():
            return None
        try:
            info = json.loads(metadata.read_text(encoding="utf-8"))
            received = part.stat().st_size if part.exists() else 0
            return received, int(info["total"]), str(info["original_name"])
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    def append_upload_chunk(
        self,
        *,
        tenant_key: str,
        tool_id: str,
        upload_id: str,
        offset: int,
        total: int,
        original_name: str,
        chunk: bytes,
    ) -> int:
        """Append one sequential chunk and return the new server offset.

        The part file itself is the resume cursor, so interrupted requests do
        not need database state. A mismatched cursor is rejected before any
        bytes are written and the caller can query the current offset.
        """
        part, metadata = self._upload_paths(tenant_key, tool_id, upload_id)
        part.parent.mkdir(parents=True, exist_ok=True)

        current = self.upload_status(
            tenant_key=tenant_key,
            tool_id=tool_id,
            upload_id=upload_id,
        )
        if current is None:
            if offset != 0:
                raise ValueError("upload does not exist")
            metadata.write_text(
                json.dumps({"total": total, "original_name": original_name}),
                encoding="utf-8",
            )
            received = 0
        else:
            received, stored_total, stored_name = current
            if stored_total != total or stored_name != original_name:
                raise ValueError("upload metadata does not match")

        if offset != received:
            raise ValueError(f"offset mismatch: expected {received}")
        if received + len(chunk) > total:
            raise ValueError("chunk exceeds declared file size")

        with part.open("ab") as output:
            output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        return received + len(chunk)

    def finalize_upload(
        self,
        *,
        tenant_key: str,
        tool_id: str,
        upload_id: str,
    ) -> tuple[str, int, str]:
        """Validate and promote a complete partial upload into binary storage."""
        part, metadata = self._upload_paths(tenant_key, tool_id, upload_id)
        current = self.upload_status(
            tenant_key=tenant_key,
            tool_id=tool_id,
            upload_id=upload_id,
        )
        if current is None:
            raise FileNotFoundError("upload does not exist")
        received, total, original_name = current
        if received != total:
            raise ValueError(f"upload incomplete: received {received} of {total}")

        hasher = hashlib.sha256()
        with part.open("rb") as source:
            magic = source.read(8)
            if not any(magic.startswith(accepted) for accepted in _ACCEPTED_MAGICS):
                raise MagicNumberError(f"magic bytes {magic[:4]!r} not accepted")
            hasher.update(magic)
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)

        sha = hasher.hexdigest()
        final = part.parent.parent / f"{sha}.bin"
        if final.exists():
            part.unlink()
        else:
            part.replace(final)
        final.chmod(
            stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH
            | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        metadata.unlink(missing_ok=True)
        return sha, total, original_name

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
