import asyncio
import threading

import pytest

from app.services.storage_runtime import local as local_storage


@pytest.mark.asyncio
async def test_concurrent_local_file_writes_to_same_key_are_atomic(
    tmp_path,
    monkeypatch,
):
    backend = local_storage.LocalStorageBackend(str(tmp_path / "storage"))
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first-media")
    second.write_bytes(b"second-media")
    copy_barrier = threading.Barrier(2)
    original_copyfile = local_storage.shutil.copyfile

    def synchronized_copy(source, destination):
        result = original_copyfile(source, destination)
        copy_barrier.wait(timeout=2)
        return result

    monkeypatch.setattr(local_storage.shutil, "copyfile", synchronized_copy)

    await asyncio.gather(
        backend.write_local_file("agent/media/imported/demo.mp4", first),
        backend.write_local_file("agent/media/imported/demo.mp4", second),
    )

    target = tmp_path / "storage" / "agent" / "media" / "imported" / "demo.mp4"
    assert target.read_bytes() in {b"first-media", b"second-media"}
    assert list(target.parent.glob("*.importing")) == []
