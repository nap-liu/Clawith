"""Tests for tools/read_image/input_loader.py."""

import asyncio
from pathlib import Path

import pytest

from app.services.tools.read_image.input_loader import (
    DEFAULT_CONFIG,
    LoadError,
    LoadResult,
    LoadedImage,
    load,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def jpeg_bytes() -> bytes:
    """A real, Pillow-decodable 8x8 JPEG."""
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGB", (8, 8), color=(128, 128, 200))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    """A real, Pillow-decodable 8x8 PNG."""
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGBA", (8, 8), color=(0, 255, 0, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─── Config merge + defaults ─────────────────────────────────────────────────

def test_default_config_workspace_only():
    assert DEFAULT_CONFIG["input_modes"]["workspace_path"]["enabled"] is True
    assert DEFAULT_CONFIG["input_modes"]["url"]["enabled"] is False
    assert DEFAULT_CONFIG["input_modes"]["base64"]["enabled"] is False
    assert DEFAULT_CONFIG["max_images_per_call"] == 6


# ─── Workspace path — happy path ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_workspace_path_happy(workspace, jpeg_bytes):
    (workspace / "img.jpg").write_bytes(jpeg_bytes)
    result = await load(["img.jpg"], workspace, DEFAULT_CONFIG)
    assert result.short_circuit is None
    assert len(result.items) == 1
    item = result.items[0]
    assert isinstance(item, LoadedImage)
    assert item.display_ref == "img.jpg"
    assert item.data_url.startswith("data:image/")


# ─── Workspace path — defenses ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_workspace_path_traversal_is_category_A(workspace):
    result = await load(["../etc/passwd"], workspace, DEFAULT_CONFIG)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "路径越界" in result.short_circuit.reason or "path" in result.short_circuit.reason.lower()


@pytest.mark.asyncio
async def test_workspace_path_absolute_is_category_A(workspace):
    # Absolute paths should not escape workspace scope
    result = await load(["/etc/passwd"], workspace, DEFAULT_CONFIG)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"


@pytest.mark.asyncio
async def test_workspace_path_symlink_rejected_category_A(workspace, jpeg_bytes):
    # Set up: a symlink inside workspace pointing to a file outside
    outside = workspace.parent / "outside.jpg"
    outside.write_bytes(jpeg_bytes)
    link = workspace / "sneaky.jpg"
    link.symlink_to(outside)
    result = await load(["sneaky.jpg"], workspace, DEFAULT_CONFIG)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "symlink" in result.short_circuit.reason.lower()


@pytest.mark.asyncio
async def test_workspace_path_nonexistent_is_category_B_inline(workspace, jpeg_bytes):
    # One good + one missing → category B inline on the missing one, no short-circuit
    (workspace / "good.jpg").write_bytes(jpeg_bytes)
    result = await load(["good.jpg", "missing.jpg"], workspace, DEFAULT_CONFIG)
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadedImage)
    assert isinstance(result.items[1], LoadError)
    assert result.items[1].category == "B"
    assert "not exist" in result.items[1].reason.lower() or "不存在" in result.items[1].reason


# ─── Parameter bounds ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_too_many_images_is_category_A(workspace, jpeg_bytes):
    for i in range(7):
        (workspace / f"img{i}.jpg").write_bytes(jpeg_bytes)
    paths = [f"img{i}.jpg" for i in range(7)]
    result = await load(paths, workspace, DEFAULT_CONFIG)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "6" in result.short_circuit.reason  # cites the limit


# ─── Mime sniff — spec table ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mime_sniff_rejects_zip_with_jpg_extension(workspace):
    # ZIP magic: 50 4B 03 04
    (workspace / "fake.jpg").write_bytes(b"PK\x03\x04" + b"\x00" * 16)
    result = await load(["fake.jpg"], workspace, DEFAULT_CONFIG)
    # Category B inline: file exists but is not a valid image
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadError)
    assert result.items[0].category == "B"
    assert "image" in result.items[0].reason.lower() or "图片" in result.items[0].reason


@pytest.mark.asyncio
async def test_mime_sniff_accepts_png(workspace, png_bytes):
    (workspace / "x.png").write_bytes(png_bytes)
    result = await load(["x.png"], workspace, DEFAULT_CONFIG)
    assert isinstance(result.items[0], LoadedImage)
