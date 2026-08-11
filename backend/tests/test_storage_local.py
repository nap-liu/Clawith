import asyncio
import threading

import pytest

from app.services.storage_runtime import local as local_storage


@pytest.mark.asyncio
async def test_local_storage_reads_regular_files_through_anchored_descriptors(tmp_path):
    storage_root = tmp_path / "storage"
    report = storage_root / "agent-a" / "reports" / "summary.txt"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"first\nsecond\nthird\n")
    backend = local_storage.LocalStorageBackend(str(storage_root))

    assert await backend.exists("agent-a/reports/summary.txt") is True
    assert await backend.is_file("agent-a/reports/summary.txt") is True
    assert await backend.is_dir("agent-a/reports") is True
    assert await backend.read_bytes("agent-a/reports/summary.txt") == report.read_bytes()
    assert await backend.read_range("agent-a/reports/summary.txt", 6, 11) == b"second"
    lines = await backend.read_text_lines(
        "agent-a/reports/summary.txt",
        offset=1,
        limit=1,
    )
    assert lines.lines == ["second"]
    assert lines.total_lines == 3
    entries = await backend.list_dir("agent-a/reports")
    assert [(entry.name, entry.is_dir) for entry in entries] == [("summary.txt", False)]
    assert (await backend.stat("agent-a/reports/summary.txt")).size == len(report.read_bytes())
    assert (await backend.get_version("agent-a/reports/summary.txt")).exists is True


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


@pytest.mark.asyncio
async def test_local_storage_does_not_follow_symlinks_between_agent_roots(tmp_path):
    storage_root = tmp_path / "storage"
    first_agent = storage_root / "agent-a"
    second_media = storage_root / "agent-b" / "private" / "secret.mp4"
    first_agent.mkdir(parents=True)
    second_media.parent.mkdir(parents=True)
    second_media.write_bytes(b"agent-b-private-media")
    (first_agent / "linked.mp4").symlink_to(second_media)
    backend = local_storage.LocalStorageBackend(str(storage_root))

    assert await backend.exists("agent-a/linked.mp4") is False
    assert await backend.is_file("agent-a/linked.mp4") is False
    assert all(entry.name != "linked.mp4" for entry in await backend.list_dir("agent-a"))
    with pytest.raises(OSError):
        await backend.read_bytes("agent-a/linked.mp4")
