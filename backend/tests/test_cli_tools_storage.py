"""BinaryStorage tests — filesystem-backed, content-addressed."""

from __future__ import annotations

import hashlib
import io

import pytest

from app.services.cli_tools.storage import (
    BinaryStorage,
    MagicNumberError,
)

_ELF = b"\x7fELF" + b"\x00" * 60 + b"rest-of-elf-header"
_SHEBANG = b"#!/bin/sh\necho hi\n"


@pytest.mark.asyncio
async def test_write_and_resolve_roundtrip(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, size = await storage.write(
        tenant_key="t1",
        tool_id="tool1",
        stream=io.BytesIO(_ELF),
    )
    assert sha == hashlib.sha256(_ELF).hexdigest()
    assert size == len(_ELF)

    path = storage.resolve(tenant_key="t1", tool_id="tool1", sha=sha)
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o555
    assert path.read_bytes() == _ELF


@pytest.mark.asyncio
async def test_write_rejects_unknown_magic(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    with pytest.raises(MagicNumberError):
        await storage.write(
            tenant_key="t1",
            tool_id="tool1",
            stream=io.BytesIO(b"\xff\xff\xff\xff not a binary"),
        )


@pytest.mark.asyncio
async def test_write_accepts_shebang_script(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, size = await storage.write(
        tenant_key="t1",
        tool_id="tool1",
        stream=io.BytesIO(_SHEBANG),
    )
    assert size == len(_SHEBANG)
    assert storage.resolve("t1", "tool1", sha).read_bytes() == _SHEBANG


@pytest.mark.asyncio
async def test_list_shas_for_tool(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    a, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF))
    b, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG))
    assert set(storage.list_shas("t1", "tool1")) == {a, b}


@pytest.mark.asyncio
async def test_unreferenced_shas_scan(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    a, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF))
    b, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG))
    # Only `a` is still referenced.
    orphans = list(storage.iter_orphans(referenced_shas={a}))
    assert len(orphans) == 1
    assert orphans[0].name == f"{b}.bin"


@pytest.mark.asyncio
async def test_content_addressed_dedup(tmp_path):
    """Uploading identical content twice yields one file (same SHA)."""
    storage = BinaryStorage(root=tmp_path)
    sha1, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF))
    sha2, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF))
    assert sha1 == sha2
    assert len(list(storage.list_shas("t1", "tool1"))) == 1


def test_delete_orphans_counts_successful_deletions(tmp_path):
    """delete_orphans returns the number of files actually removed."""
    storage = BinaryStorage(root=tmp_path)
    fake_path_a = tmp_path / "a.bin"
    fake_path_b = tmp_path / "b.bin"
    fake_path_a.write_bytes(b"x")
    # b.bin never existed.
    deleted = storage.delete_orphans([fake_path_a, fake_path_b])
    assert deleted == 1
    assert not fake_path_a.exists()


def test_resumable_upload_reports_offset_and_finalizes(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    upload_id = "12345678-1234-5678-1234-567812345678"
    first = _ELF[:25]
    second = _ELF[25:]

    received = storage.append_upload_chunk(
        tenant_key="t1",
        tool_id="tool1",
        upload_id=upload_id,
        offset=0,
        total=len(_ELF),
        original_name="tool.bin",
        chunk=first,
    )
    assert received == len(first)
    assert storage.upload_status(
        tenant_key="t1", tool_id="tool1", upload_id=upload_id
    ) == (len(first), len(_ELF), "tool.bin")

    received = storage.append_upload_chunk(
        tenant_key="t1",
        tool_id="tool1",
        upload_id=upload_id,
        offset=received,
        total=len(_ELF),
        original_name="tool.bin",
        chunk=second,
    )
    assert received == len(_ELF)

    sha, size, name = storage.finalize_upload(
        tenant_key="t1", tool_id="tool1", upload_id=upload_id
    )
    assert sha == hashlib.sha256(_ELF).hexdigest()
    assert size == len(_ELF)
    assert name == "tool.bin"
    assert storage.resolve("t1", "tool1", sha).read_bytes() == _ELF
    assert storage.upload_status(
        tenant_key="t1", tool_id="tool1", upload_id=upload_id
    ) is None


def test_resumable_upload_rejects_wrong_offset_without_writing(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    common = {
        "tenant_key": "t1",
        "tool_id": "tool1",
        "upload_id": "12345678-1234-5678-1234-567812345678",
        "total": len(_ELF),
        "original_name": "tool.bin",
    }
    storage.append_upload_chunk(**common, offset=0, chunk=_ELF[:10])

    with pytest.raises(ValueError, match="offset mismatch"):
        storage.append_upload_chunk(**common, offset=5, chunk=_ELF[10:20])

    assert storage.upload_status(**{
        "tenant_key": common["tenant_key"],
        "tool_id": common["tool_id"],
        "upload_id": common["upload_id"],
    })[0] == 10


def test_resumable_upload_cannot_finalize_incomplete_file(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    upload_id = "12345678-1234-5678-1234-567812345678"
    storage.append_upload_chunk(
        tenant_key="t1",
        tool_id="tool1",
        upload_id=upload_id,
        offset=0,
        total=len(_ELF),
        original_name="tool.bin",
        chunk=_ELF[:10],
    )
    with pytest.raises(ValueError, match="upload incomplete"):
        storage.finalize_upload(
            tenant_key="t1", tool_id="tool1", upload_id=upload_id
        )
