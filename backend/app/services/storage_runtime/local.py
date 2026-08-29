"""Local filesystem storage backend."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat as stat_module
import uuid
from pathlib import Path, PurePosixPath

import aiofiles
from fastapi import HTTPException, status

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageVersion,
    TextLineRange,
    WriteCondition,
    content_hash_bytes,
)
from app.services.storage_runtime.utils import normalize_storage_key


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root)

    def _full_path(self, key: str) -> Path:
        normalized = normalize_storage_key(key)
        full = (self.root / normalized).resolve()
        root_resolved = self.root.resolve()
        try:
            full.relative_to(root_resolved)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal not allowed")
        return full

    def _open_readonly_fd(self, key: str, *, directory: bool = False) -> int:
        """Open one storage key without following symlinks in any component."""
        normalized = normalize_storage_key(key)
        parts = PurePosixPath(normalized).parts if normalized else ()
        current_fd = os.open(
            self.root.resolve(),
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            for index, part in enumerate(parts):
                is_final = index == len(parts) - 1
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if not is_final or directory:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    async def exists(self, key: str) -> bool:
        try:
            fd = self._open_readonly_fd(key)
        except OSError:
            return False
        os.close(fd)
        return True

    async def is_file(self, key: str) -> bool:
        try:
            fd = self._open_readonly_fd(key)
        except OSError:
            return False
        try:
            return stat_module.S_ISREG(os.fstat(fd).st_mode)
        finally:
            os.close(fd)

    async def is_dir(self, key: str) -> bool:
        try:
            fd = self._open_readonly_fd(key, directory=True)
        except OSError:
            return False
        os.close(fd)
        return True

    async def list_dir(self, key: str) -> list[StorageEntry]:
        normalized = normalize_storage_key(key)
        try:
            base_fd = self._open_readonly_fd(normalized, directory=True)
        except OSError:
            return []
        try:
            entries: list[StorageEntry] = []
            entry_stats = []
            for name in os.listdir(base_fd):
                if name == ".gitkeep":
                    continue
                entry_stat = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                if stat_module.S_ISLNK(entry_stat.st_mode):
                    continue
                entry_stats.append((name, entry_stat))
            for name, entry_stat in sorted(
                entry_stats,
                key=lambda item: (not stat_module.S_ISDIR(item[1].st_mode), item[0]),
            ):
                is_dir = stat_module.S_ISDIR(entry_stat.st_mode)
                is_file = stat_module.S_ISREG(entry_stat.st_mode)
                rel = f"{normalized.rstrip('/')}/{name}" if normalized else name
                entries.append(
                    StorageEntry(
                        name=name,
                        key=rel,
                        is_dir=is_dir,
                        size=entry_stat.st_size if is_file else 0,
                        modified_at=str(entry_stat.st_mtime),
                        version_id=_local_version_token(entry_stat, None),
                    )
                )
            return entries
        finally:
            os.close(base_fd)

    async def read_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._read_bytes_sync, key)

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        length = max(0, end - start + 1)
        return await asyncio.to_thread(
            self._read_range_sync,
            key,
            max(0, start),
            length,
        )

    async def read_text_lines(
        self,
        key: str,
        *,
        offset: int = 0,
        limit: int = 2000,
        encoding: str = "utf-8",
        errors: str = "replace",
    ) -> TextLineRange:
        return await asyncio.to_thread(
            _local_read_text_lines,
            self,
            key,
            max(0, offset),
            max(0, limit),
            encoding,
            errors,
        )

    def _read_bytes_sync(self, key: str) -> bytes:
        fd = self._open_readonly_fd(key)
        with os.fdopen(fd, "rb") as file_obj:
            return file_obj.read()

    def _read_range_sync(self, key: str, start: int, length: int) -> bytes:
        fd = self._open_readonly_fd(key)
        with os.fdopen(fd, "rb") as file_obj:
            file_obj.seek(start)
            return file_obj.read(length)

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.writing")
        try:
            async with aiofiles.open(partial, "wb") as f:
                await f.write(data)
                await f.flush()
            await asyncio.to_thread(os.replace, partial, path)
        finally:
            partial.unlink(missing_ok=True)

    async def write_local_file(
        self,
        key: str,
        path: Path,
        content_type: str | None = None,
    ) -> None:
        target = self._full_path(key)
        if path.resolve() == target.resolve():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.importing")
        try:
            await asyncio.to_thread(shutil.copyfile, path, partial)
            await asyncio.to_thread(os.replace, partial, target)
        finally:
            partial.unlink(missing_ok=True)

    async def delete(self, key: str) -> None:
        path = self._full_path(key)
        if not path.exists():
            return
        if path.is_dir():
            await self.delete_tree(key)
        else:
            path.unlink()

    async def delete_tree(self, key: str) -> None:
        path = self._full_path(key)
        if not path.exists():
            return
        await asyncio.to_thread(_local_delete_tree, path)

    async def stat(self, key: str) -> StorageEntry:
        normalized = normalize_storage_key(key)
        fd = self._open_readonly_fd(normalized)
        try:
            stat = os.fstat(fd)
            version_id = _local_version_token(stat, None)
            return StorageEntry(
                name=PurePosixPath(normalized).name,
                key=normalized,
                is_dir=stat_module.S_ISDIR(stat.st_mode),
                size=stat.st_size if stat_module.S_ISREG(stat.st_mode) else 0,
                modified_at=str(stat.st_mtime),
                version_id=version_id,
            )
        finally:
            os.close(fd)

    async def get_version(self, key: str) -> StorageVersion:
        normalized = normalize_storage_key(key)
        try:
            fd = self._open_readonly_fd(normalized)
        except OSError:
            return StorageVersion(key=normalize_storage_key(key), exists=False, is_dir=False)
        try:
            stat = os.fstat(fd)
            if stat_module.S_ISDIR(stat.st_mode):
                return StorageVersion(
                    key=normalized,
                    exists=True,
                    is_dir=True,
                    modified_at=str(stat.st_mtime),
                    version_id=_local_version_token(stat, None),
                )
        finally:
            os.close(fd)
        data = await self.read_bytes(key)
        file_hash = content_hash_bytes(data)
        return StorageVersion(
            key=normalized,
            exists=True,
            is_dir=False,
            size=stat.st_size,
            modified_at=str(stat.st_mtime),
            etag=file_hash,
            version_id=_local_version_token(stat, file_hash),
            content_hash=file_hash,
        )

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        if condition and condition.require_absent:
            path = self._full_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(_local_create_exclusive, path, data)
            except FileExistsError:
                return ConditionalWriteResult(
                    ok=False,
                    conflict=True,
                    current_version=await self.get_version(key),
                )
            return ConditionalWriteResult(
                ok=True,
                current_version=await self.get_version(key),
            )

        current = await self.get_version(key)
        if condition:
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))

    async def local_path_for(self, key: str) -> Path | None:
        return self._full_path(key)


def _local_delete_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


def _local_create_exclusive(path: Path, data: bytes) -> None:
    """Atomically create ``path`` and fail if another writer won the race."""
    with path.open("xb") as file_obj:
        file_obj.write(data)


def _local_read_text_lines(
    backend: LocalStorageBackend,
    key: str,
    offset: int,
    limit: int,
    encoding: str,
    errors: str,
) -> TextLineRange:
    selected: list[str] = []
    end = offset + limit
    total_lines = 0
    fd = backend._open_readonly_fd(key)
    with os.fdopen(
        fd,
        "r",
        encoding=encoding,
        errors=errors,
        newline=None,
    ) as file_obj:
        for line_index, line in enumerate(file_obj):
            total_lines = line_index + 1
            if offset <= line_index < end:
                selected.append(line.removesuffix("\n").removesuffix("\r"))
    return TextLineRange(lines=selected, total_lines=total_lines)


def _local_version_token(stat, file_hash: str | None) -> str:
    hash_part = file_hash or ""
    return f"{stat.st_mtime_ns}:{stat.st_size}:{hash_part}"
