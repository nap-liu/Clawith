"""Tests for tools/read_image/input_loader.py."""

import asyncio
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest

from app.services.tools.read_image.input_loader import (
    DEFAULT_CONFIG,
    LoadError,
    LoadedImage,
    load,
    merge_config,
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
    result = await load([url], workspace, DEFAULT_CONFIG)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "base64" in result.short_circuit.reason.lower()


@pytest.mark.asyncio
async def test_base64_happy_path_jpeg(workspace, jpeg_bytes):
    url = _make_data_url("jpeg", jpeg_bytes)
    result = await load([url], workspace, _b64_config())
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadedImage)
    assert "base64" in result.items[0].display_ref.lower()
    assert "base64," not in result.items[0].display_ref
    assert url.split(",", 1)[1] not in result.items[0].display_ref


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


# ─── URL mode ────────────────────────────────────────────────────────────────

def _url_config(allowlist=None) -> dict:
    import copy
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["input_modes"]["url"]["enabled"] = True
    c["input_modes"]["url"]["allowlist"] = ["*.example.com"] if allowlist is None else allowlist
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


@pytest.mark.asyncio
async def test_url_rejects_untrusted_tls_certificate(monkeypatch, workspace, jpeg_bytes):
    """Exercise the real HTTP client against a local self-signed HTTPS server."""
    from app.services.tools.read_image import input_loader

    cert_path = workspace / "cert.pem"
    key_path = workspace / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path), "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True, capture_output=True, timeout=10,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)

    async def serve_image(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
            + f"Content-Length: {len(jpeg_bytes)}\r\nConnection: close\r\n\r\n".encode()
            + jpeg_bytes
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def public_dns(_host):
        # Bypass only the separate SSRF guard to reach the local TLS fixture.
        return ["93.184.216.34"]

    monkeypatch.setattr(input_loader, "_resolve_host", public_dns)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = await asyncio.start_server(serve_image, "127.0.0.1", 0, ssl=context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        result = await load(
            [f"https://127.0.0.1:{port}/image.jpg"], workspace,
            _url_config(allowlist=["127.0.0.1"]),
        )
    assert result.short_circuit is None
    assert isinstance(result.items[0], LoadError)
    assert result.items[0].category == "B"
    assert "certificate verify failed" in result.items[0].reason.lower()


@pytest.mark.asyncio
async def test_url_redirect_to_private_ip_rejected(monkeypatch, workspace):
    """Verify we revalidate Location headers at each redirect hop."""
    from app.services.tools.read_image import input_loader

    async def fake_resolve(host):
        if host == "cdn.example.com":
            return ["93.184.216.34"]
        return ["10.0.0.5"]

    requested_urls = []

    def redirect(request):
        requested_urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://private.example.com/x.jpg"})

    monkeypatch.setattr(input_loader, "_resolve_host", fake_resolve)
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        input_loader.httpx, "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(redirect), **kwargs),
    )

    cfg = _url_config(allowlist=["*.example.com"])
    result = await load(["https://cdn.example.com/x.jpg"], workspace, cfg)
    assert result.short_circuit is not None
    assert result.short_circuit.category == "A"
    assert "blocked IP" in result.short_circuit.reason
    assert requested_urls == ["https://cdn.example.com/x.jpg"]


# ─── Config merge (tightening-only) ──────────────────────────────────────────

def test_merge_config_tightens_max_images_when_agent_lower():
    tool = {"max_images_per_call": 6}
    agent = {"max_images_per_call": 3}
    merged = merge_config(tool, agent)
    assert merged["max_images_per_call"] == 3


def test_merge_config_ignores_looser_agent_value():
    tool = {"max_images_per_call": 6}
    agent = {"max_images_per_call": 10}
    merged = merge_config(tool, agent)
    assert merged["max_images_per_call"] == 6  # tool is the upper bound


def test_merge_config_intersects_url_allowlist():
    tool = {"input_modes": {"url": {"allowlist": ["*.a.com", "*.b.com"]}}}
    agent = {"input_modes": {"url": {"allowlist": ["*.b.com", "*.c.com"]}}}
    merged = merge_config(tool, agent)
    assert merged["input_modes"]["url"]["allowlist"] == ["*.b.com"]


def test_merge_config_agent_can_disable_mode_tool_has_on():
    tool = {"input_modes": {"url": {"enabled": True}}}
    agent = {"input_modes": {"url": {"enabled": False}}}
    merged = merge_config(tool, agent)
    assert merged["input_modes"]["url"]["enabled"] is False


def test_merge_config_agent_cannot_enable_mode_tool_has_off():
    tool = {"input_modes": {"url": {"enabled": False}}}
    agent = {"input_modes": {"url": {"enabled": True}}}
    merged = merge_config(tool, agent)
    assert merged["input_modes"]["url"]["enabled"] is False


def test_merge_config_model_id_uses_agent_value_when_set():
    """model_id is a choice not a bound — agent precedence applies."""
    tool = {"model_id": "tool-uuid"}
    agent = {"model_id": "agent-uuid"}
    merged = merge_config(tool, agent)
    assert merged["model_id"] == "agent-uuid"


def test_merge_config_no_agent_returns_tool():
    tool = {"max_images_per_call": 6}
    merged = merge_config(tool, None)
    assert merged["max_images_per_call"] == 6


def test_merge_config_missing_tool_enabled_fails_closed():
    """Tightening invariant: if tool config omits url.enabled entirely,
    agent cannot open the mode. Missing is treated as disabled."""
    tool = {"input_modes": {"url": {}}}
    agent = {"input_modes": {"url": {"enabled": True}}}
    merged = merge_config(tool, agent)
    assert merged["input_modes"]["url"]["enabled"] is False


def test_merge_config_tightens_base64_max_bytes():
    """Numeric bound for base64 mode also takes min."""
    tool = {"input_modes": {"base64": {"max_bytes": 2_000_000}}}
    agent = {"input_modes": {"base64": {"max_bytes": 500_000}}}
    merged = merge_config(tool, agent)
    assert merged["input_modes"]["base64"]["max_bytes"] == 500_000


def test_merge_config_base64_max_bytes_agent_looser_ignored():
    """Reverse direction: agent asking for higher limit is ignored."""
    tool = {"input_modes": {"base64": {"max_bytes": 500_000}}}
    agent = {"input_modes": {"base64": {"max_bytes": 2_000_000}}}
    merged = merge_config(tool, agent)
    assert merged["input_modes"]["base64"]["max_bytes"] == 500_000
