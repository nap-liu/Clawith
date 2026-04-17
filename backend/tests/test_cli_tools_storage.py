"""BinaryStorage tests — filesystem-backed, content-addressed."""

from __future__ import annotations

import hashlib
import io

import pytest

from app.services.cli_tools.storage import (
    BinaryStorage,
    MagicNumberError,
    SizeLimitExceededError,
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
        max_bytes=1_000_000,
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
            max_bytes=1_000,
        )


@pytest.mark.asyncio
async def test_write_accepts_shebang_script(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, size = await storage.write(
        tenant_key="t1",
        tool_id="tool1",
        stream=io.BytesIO(_SHEBANG),
        max_bytes=1_000_000,
    )
    assert size == len(_SHEBANG)
    assert storage.resolve("t1", "tool1", sha).read_bytes() == _SHEBANG


@pytest.mark.asyncio
async def test_write_rejects_oversize(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    with pytest.raises(SizeLimitExceededError):
        await storage.write(
            tenant_key="t1",
            tool_id="tool1",
            stream=io.BytesIO(_ELF + b"x" * 10_000),
            max_bytes=1_000,
        )


@pytest.mark.asyncio
async def test_list_shas_for_tool(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    a, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF), max_bytes=1_000_000)
    b, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    assert set(storage.list_shas("t1", "tool1")) == {a, b}


@pytest.mark.asyncio
async def test_unreferenced_shas_scan(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    a, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF), max_bytes=1_000_000)
    b, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000)
    # Only `a` is still referenced.
    orphans = list(storage.iter_orphans(referenced_shas={a}))
    assert len(orphans) == 1
    assert orphans[0].name == f"{b}.bin"


@pytest.mark.asyncio
async def test_content_addressed_dedup(tmp_path):
    """Uploading identical content twice yields one file (same SHA)."""
    storage = BinaryStorage(root=tmp_path)
    sha1, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF), max_bytes=1_000_000)
    sha2, _ = await storage.write(tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_ELF), max_bytes=1_000_000)
    assert sha1 == sha2
    assert len(list(storage.list_shas("t1", "tool1"))) == 1


def _write_positional_wrapper(storage: BinaryStorage, tenant: str, tool: str, data: bytes):
    """Tiny helper so iter_orphans test can seed quickly via sync write."""
    import asyncio

    return asyncio.get_event_loop().run_until_complete(
        storage.write(tenant_key=tenant, tool_id=tool, stream=io.BytesIO(data), max_bytes=1_000_000)
    )


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
