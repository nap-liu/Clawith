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


# ─── Base64 mode ─────────────────────────────────────────────────────────────

def _b64_config(enabled=True, max_bytes=1048576) -> dict:
    import copy
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["input_modes"]["base64"]["enabled"] = enabled
    c["input_modes"]["base64"]["max_bytes"] = max_bytes
    return c


def _make_data_url(mime: str, raw: bytes) -> str:
    import base64 as b64
    return f"data:image/{mime};base64," + b64.b64encode(raw).decode("ascii")


@pytest.mark.asyncio
async def test_base64_disabled_is_category_A(workspace, jpeg_bytes):
    url = _make_data_url("jpeg", jpeg_bytes)
    result = await load([url], workspace, _b64_config(enabled=False))
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "base64" in result.short_circuit.reason.lower()


@pytest.mark.asyncio
async def test_base64_happy_path_jpeg(workspace, jpeg_bytes):
    url = _make_data_url("jpeg", jpeg_bytes)
    result = await load([url], workspace, _b64_config())
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadedImage)
    # Display ref must NOT contain the base64 payload
    assert "base64" in result.items[0].display_ref.lower() or "[base64" in result.items[0].display_ref
    assert "AAAA" not in result.items[0].display_ref  # no payload leak


@pytest.mark.asyncio
async def test_non_data_prefix_routes_to_workspace_path(workspace):
    """A string not starting with 'data:' / 'http(s)://' routes to workspace-path.
    Workspace-path treats 'notadatauri:...' as a (nonexistent) filename and returns
    a category-B "文件不存在或不可读" per-item error, NOT a short-circuit.
    """
    result = await load(["notadatauri:image/jpeg;base64,AAAA"], workspace, _b64_config())
    assert result.short_circuit is None
    assert len(result.items) == 1
    assert isinstance(result.items[0], LoadError)
    assert result.items[0].category == "B"


@pytest.mark.asyncio
async def test_data_prefix_with_wrong_mime_is_category_B_malformed(workspace):
    """A 'data:' URI that doesn't match the strict image prefix regex
    routes to base64 mode and fails the format check as category B."""
    result = await load(["data:text/plain;base64,SGVsbG8="], workspace, _b64_config())
    assert result.short_circuit is None
    assert len(result.items) == 1
    assert isinstance(result.items[0], LoadError)
    assert result.items[0].category == "B"
    assert "格式" in result.items[0].reason or "prefix" in result.items[0].reason.lower()


@pytest.mark.asyncio
async def test_base64_oversize_before_decode_is_category_B(workspace):
    huge = "A" * 20_000_000  # base64 size way over 1 MB cap
    url = "data:image/jpeg;base64," + huge
    result = await load([url], workspace, _b64_config(max_bytes=1_000_000))
    # Must fail without attempting decode
    assert result.short_circuit is None  # category B, not A
    assert isinstance(result.items[0], LoadError)
    assert result.items[0].category == "B"
    assert "size" in result.items[0].reason.lower() or "超" in result.items[0].reason


@pytest.mark.asyncio
async def test_base64_fake_image_bytes_is_category_B(workspace):
    # Valid base64 of ZIP magic bytes — decodes fine, mime sniff rejects
    import base64 as b64
    raw = b"PK\x03\x04" + b"\x00" * 16
    url = "data:image/jpeg;base64," + b64.b64encode(raw).decode("ascii")
    result = await load([url], workspace, _b64_config())
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadError)
    assert result.items[0].category == "B"


@pytest.mark.asyncio
async def test_base64_display_ref_never_contains_payload(workspace, jpeg_bytes):
    url = _make_data_url("jpeg", jpeg_bytes)
    result = await load([url], workspace, _b64_config())
    item = result.items[0]
    # Regardless of success or failure, the display_ref must not contain the payload
    assert "base64," not in item.display_ref


# ─── URL mode ────────────────────────────────────────────────────────────────

def _url_config(allowlist=None) -> dict:
    import copy
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["input_modes"]["url"]["enabled"] = True
    c["input_modes"]["url"]["allowlist"] = allowlist or ["*.example.com"]
    return c


@pytest.mark.asyncio
async def test_url_disabled_is_category_A(workspace):
    result = await load(
        ["https://cdn.example.com/x.jpg"], workspace, DEFAULT_CONFIG
    )
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "url" in result.short_circuit.reason.lower() or "URL" in result.short_circuit.reason


@pytest.mark.asyncio
async def test_url_non_http_scheme_is_category_A(workspace):
    cfg = _url_config(allowlist=["*"])
    for bad in [
        "file:///etc/passwd",
        "gopher://attacker/",
        "ftp://example.com/x.jpg",
    ]:
        result = await load([bad], workspace, cfg)
        assert result.short_circuit is not None, f"expected refusal for {bad}"
        assert result.short_circuit.category == "A"


@pytest.mark.asyncio
async def test_url_empty_allowlist_denies_all(workspace):
    cfg = _url_config(allowlist=[])
    result = await load(["https://cdn.example.com/x.jpg"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"


@pytest.mark.asyncio
async def test_url_allowlist_miss_is_category_A(workspace):
    cfg = _url_config(allowlist=["*.allowed.example"])
    result = await load(["https://attacker.example/x.jpg"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"


@pytest.mark.asyncio
async def test_url_private_ip_is_rejected(monkeypatch, workspace):
    """Even with allowlist hit, private-IP DNS resolution → refusal."""
    from app.services.tools.read_image import input_loader

    async def fake_resolve(host):
        return ["10.0.0.5"]

    monkeypatch.setattr(input_loader, "_resolve_host", fake_resolve)
    cfg = _url_config(allowlist=["*.example.com"])
    result = await load(["https://internal.example.com/x.jpg"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "private" in result.short_circuit.reason.lower() or "内网" in result.short_circuit.reason


@pytest.mark.asyncio
async def test_url_loopback_rejected(monkeypatch, workspace):
    from app.services.tools.read_image import input_loader

    async def fake_resolve(host):
        return ["127.0.0.1"]

    monkeypatch.setattr(input_loader, "_resolve_host", fake_resolve)
    cfg = _url_config(allowlist=["*"])
    result = await load(["https://any.example.com/x.jpg"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"


@pytest.mark.asyncio
async def test_url_link_local_rejected(monkeypatch, workspace):
    from app.services.tools.read_image import input_loader

    async def fake_resolve(host):
        return ["169.254.169.254"]  # EC2 IMDS

    monkeypatch.setattr(input_loader, "_resolve_host", fake_resolve)
    cfg = _url_config(allowlist=["*"])
    result = await load(["https://metadata.example/x"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"


@pytest.mark.asyncio
async def test_url_happy_path_fetch(monkeypatch, workspace, jpeg_bytes):
    from app.services.tools.read_image import input_loader

    async def fake_resolve(host):
        return ["93.184.216.34"]  # example.com, public

    async def fake_fetch(url, config):
        return (jpeg_bytes, url)

    monkeypatch.setattr(input_loader, "_resolve_host", fake_resolve)
    monkeypatch.setattr(input_loader, "_fetch_with_revalidation", fake_fetch)

    cfg = _url_config(allowlist=["*.example.com"])
    result = await load(["https://cdn.example.com/x.jpg"], workspace, cfg)
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadedImage)
    assert result.items[0].display_ref == "https://cdn.example.com/x.jpg"


def test_verify_true_is_hardcoded_in_input_loader():
    """Source-scan lock: input_loader must hardcode httpx verify=True.

    This test catches a whole class of "someone disabled TLS verification
    to support an internal self-signed cert" regressions. If that need
    ever arises, it must land as a spec/ADR change, not a one-line flip.
    """
    import inspect
    from app.services.tools.read_image import input_loader

    src = inspect.getsource(input_loader)
    assert "verify=True" in src, "input_loader must hardcode verify=True"
    assert "verify=False" not in src, (
        "verify=False is never acceptable for read_image; "
        "see spec §5 Security 2 (HTTPS verification is mandatory)."
    )


@pytest.mark.asyncio
async def test_url_redirect_to_private_ip_rejected(monkeypatch, workspace):
    """Verify we revalidate Location headers at each redirect hop."""
    from app.services.tools.read_image import input_loader

    async def fake_resolve(host):
        if host == "cdn.example.com":
            return ["93.184.216.34"]
        return ["10.0.0.5"]

    async def fake_fetch(url, config):
        raise input_loader._RedirectToPrivateIP("Location resolved to private IP")

    monkeypatch.setattr(input_loader, "_resolve_host", fake_resolve)
    monkeypatch.setattr(input_loader, "_fetch_with_revalidation", fake_fetch)

    cfg = _url_config(allowlist=["*.example.com"])
    result = await load(["https://cdn.example.com/x.jpg"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
