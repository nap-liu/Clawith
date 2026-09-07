import asyncio
import threading

import httpx
import pytest

from app.services import media_url_source
from media_url_source_support import (
    MP4_BYTES,
    SESSION_ID,
    _async_addresses,
    _fail_temp_directory,
    _staging_dir,
)


@pytest.mark.asyncio
async def test_managed_url_rejects_wrong_media_bytes_before_final_move(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200,
        content=b"ID3-this-is-audio",
        request=request,
    ))

    async def fake_validate(url, *, external):
        return url

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(media_url_source, "validate_media_url", fake_validate)
    monkeypatch.setattr(media_url_source, "_resolve_host", lambda *_args: _async_addresses(["93.184.216.34"]))
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://media.example/fake.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="call-mismatch",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "MEDIA_KIND_MISMATCH"
    assert exc_info.value.actual_kind == "audio"
    assert list(_staging_dir(tmp_path).glob("*.partial")) == []
    assert list((tmp_path / "media" / "imported").iterdir()) == []


@pytest.mark.asyncio
async def test_managed_url_revalidates_cached_file_size(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    fetch_count = 0

    def handler(request):
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, content=MP4_BYTES, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="cached-size",
        max_bytes=1024,
        expected_media_kind="video",
    )
    imported.close()
    (tmp_path / imported.workspace_path).write_bytes(MP4_BYTES + b"x" * 2048)

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="cached-size",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "MEDIA_URL_TOO_LARGE"
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_managed_url_maps_cached_delivery_temp_failure(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    fetch_count = 0

    def handler(request):
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, content=MP4_BYTES, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="cached-temp-failure",
        max_bytes=1024,
        expected_media_kind="video",
    )
    imported.close()
    monkeypatch.setattr(
        media_url_source.tempfile,
        "mkdtemp",
        _fail_temp_directory,
    )

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="cached-temp-failure",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "MEDIA_STORAGE_FAILED"
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_external_url_requires_https(monkeypatch):
    monkeypatch.setattr(media_url_source, "_resolve_host", lambda *_args: _async_addresses(["93.184.216.34"]))

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.validate_media_url("http://example.com/a.mp3", external=True)

    assert exc_info.value.code == "INVALID_MEDIA_URL"


@pytest.mark.asyncio
async def test_external_url_rejects_invalid_port():
    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.validate_media_url(
            "https://example.com:not-a-port/a.mp3",
            external=True,
        )

    assert exc_info.value.code == "INVALID_MEDIA_URL"


@pytest.mark.asyncio
async def test_external_url_rejects_zero_port():
    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.validate_media_url(
            "https://example.com:0/a.mp3",
            external=True,
        )

    assert exc_info.value.code == "INVALID_MEDIA_URL"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Custom": "line-one\r\nInjected: true"},
        {"X-Custom": "nul\x00value"},
        {"X-Custom": "control\x01value"},
        {"X-Custom": "中文"},
        {"Bad Header": "value"},
        {"X-Number": 123},
    ],
)
def test_managed_headers_reject_invalid_names_and_values(headers):
    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        media_url_source.normalize_managed_media_headers(headers)

    assert exc_info.value.code == "INVALID_MEDIA_HEADERS"


def test_managed_headers_preserve_non_blocked_names_and_values():
    headers = {
        "Authorization": "Bearer exact-token",
        "Cookie": "media_session=exact-cookie",
        "X-Custom-Media": "  exact value  ",
        "user-agent": "Custom Browser/1.0",
    }

    assert media_url_source.normalize_managed_media_headers(headers) == headers


def test_managed_headers_filter_blocked_fields_after_merging():
    headers = media_url_source._managed_request_headers(
        {
            "Host": "untrusted.example",
            "Accept-Encoding": "identity",
            "X-Forwarded-For": "127.0.0.1",
            "X-Clawith-Trace": "internal",
            "X-Agent-ID": "agent",
            "Authorization": "Bearer exact-token",
            "User-Agent": "Neutral Client/1.0",
        },
        "media.example",
    )

    assert headers["Host"] == "media.example"
    assert headers["Authorization"] == "Bearer exact-token"
    assert headers["User-Agent"] == "Neutral Client/1.0"
    assert "Accept-Encoding" not in headers
    assert "X-Forwarded-For" not in headers
    assert "X-Clawith-Trace" not in headers
    assert "X-Agent-ID" not in headers


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [True, False])
async def test_malformed_ipv6_url_returns_structured_validation_error(external):
    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.validate_media_url(
            "https://[bad/a.mp4",
            external=external,
        )

    assert exc_info.value.code == "INVALID_MEDIA_URL"


@pytest.mark.asyncio
async def test_managed_import_rejects_malformed_url_before_agent_storage_write(tmp_path):
    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://[bad/a.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="malformed-url",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "INVALID_MEDIA_URL"
    assert not (tmp_path / ".tool_results").exists()
    assert not (tmp_path / "media").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symlink_location",
    ["tool_results", "session", "staging", "media", "imported"],
)
async def test_managed_import_rejects_agent_directory_symlink_escape(
    tmp_path,
    symlink_location,
):
    agent_root = tmp_path / "agent"
    outside = tmp_path / f"outside-{symlink_location}"
    agent_root.mkdir()
    tool_results = agent_root / ".tool_results"
    if symlink_location == "tool_results":
        tool_results.symlink_to(outside, target_is_directory=True)
    elif symlink_location == "session":
        tool_results.mkdir()
        (tool_results / SESSION_ID).symlink_to(outside, target_is_directory=True)
    elif symlink_location == "staging":
        (tool_results / SESSION_ID).mkdir(parents=True)
        (tool_results / SESSION_ID / ".media").symlink_to(
            outside,
            target_is_directory=True,
        )
    elif symlink_location == "media":
        (agent_root / "media").symlink_to(outside, target_is_directory=True)
    elif symlink_location == "imported":
        (agent_root / "media").mkdir()
        (agent_root / "media" / "imported").symlink_to(
            outside,
            target_is_directory=True,
        )

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=agent_root,
            session_id=SESSION_ID,
            intent_id=f"symlink-{symlink_location}",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "MEDIA_STORAGE_FAILED"
    assert outside.exists() is False


@pytest.mark.asyncio
async def test_managed_import_keeps_open_directory_anchor_during_symlink_swap(
    tmp_path,
    monkeypatch,
):
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    agent_root.mkdir()
    outside.mkdir()
    original_client = httpx.AsyncClient
    original_target = media_url_source._managed_request_target
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MP4_BYTES, request=request)
    )

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    async def swap_visible_staging_dir(url):
        target = await original_target(url)
        staging = _staging_dir(agent_root)
        anchored_staging = staging.with_name(".media-anchored")
        staging.rename(anchored_staging)
        staging.symlink_to(outside, target_is_directory=True)
        return target

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(
        media_url_source,
        "_managed_request_target",
        swap_visible_staging_dir,
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=agent_root,
        session_id=SESSION_ID,
        intent_id="swap-staging",
        max_bytes=1024,
        expected_media_kind="video",
    )

    assert imported.file_path.read_bytes() == MP4_BYTES
    assert list(outside.iterdir()) == []
    assert list(
        (_staging_dir(agent_root).with_name(".media-anchored")).iterdir()
    ) == []


@pytest.mark.asyncio
async def test_managed_import_rejects_visible_imported_directory_swap(
    tmp_path,
    monkeypatch,
):
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    agent_root.mkdir()
    outside.mkdir()
    original_client = httpx.AsyncClient
    original_target = media_url_source._managed_request_target
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MP4_BYTES, request=request)
    )

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    async def swap_visible_imported_dir(url):
        target = await original_target(url)
        imported = agent_root / "media" / "imported"
        imported.rename(imported.with_name("imported-anchored"))
        imported.symlink_to(outside, target_is_directory=True)
        return target

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(
        media_url_source,
        "_managed_request_target",
        swap_visible_imported_dir,
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=agent_root,
            session_id=SESSION_ID,
            intent_id="swap-imported",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "MEDIA_STORAGE_FAILED"
    anchored_files = list(
        (agent_root / "media" / "imported-anchored").iterdir()
    )
    assert len(anchored_files) == 1
    assert anchored_files[0].read_bytes() == MP4_BYTES
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_managed_import_delivery_copy_stays_bound_to_validated_bytes(
    tmp_path,
    monkeypatch,
):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MP4_BYTES, request=request)
    )

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="stable-delivery-copy",
        max_bytes=1024,
        expected_media_kind="video",
    )
    delivery_path = imported.file_path
    (tmp_path / imported.workspace_path).write_bytes(MP4_BYTES + b"ATTACKER")

    assert delivery_path.read_bytes() == MP4_BYTES
    imported.close()
    assert delivery_path.exists() is False


@pytest.mark.asyncio
async def test_managed_import_validates_the_exact_delivery_copy(
    tmp_path,
    monkeypatch,
):
    original_client = httpx.AsyncClient
    original_copy = media_url_source._copy_delivery_file
    delivery_dirs = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MP4_BYTES, request=request)
    )

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    def replace_content_before_copy(final_fd, final_name, delivery_dir):
        delivery_dirs.append(delivery_dir)
        durable_file = tmp_path / "media" / "imported" / final_name
        durable_file.write_bytes(b"attacker-not-media")
        return original_copy(final_fd, final_name, delivery_dir)

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        media_url_source,
        "_copy_delivery_file",
        replace_content_before_copy,
    )

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="validate-delivery-copy",
            max_bytes=1024,
            expected_media_kind="video",
        )

    assert exc_info.value.code == "MEDIA_KIND_MISMATCH"
    assert delivery_dirs and delivery_dirs[0].exists() is False


@pytest.mark.asyncio
async def test_managed_import_cancellation_cleans_owned_delivery_directory(
    tmp_path,
    monkeypatch,
):
    original_client = httpx.AsyncClient
    original_copy = media_url_source._copy_delivery_file
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    captured = {}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MP4_BYTES, request=request)
    )

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    def blocking_copy(final_fd, final_name, delivery_dir):
        captured["delivery_dir"] = delivery_dir
        started.set()
        try:
            release.wait(timeout=2)
            return original_copy(final_fd, final_name, delivery_dir)
        finally:
            finished.set()

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(media_url_source, "_copy_delivery_file", blocking_copy)

    task = asyncio.create_task(
        media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="cancel-delivery-copy",
            max_bytes=1024,
            expected_media_kind="video",
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)

    assert captured["delivery_dir"].exists() is False


@pytest.mark.asyncio
async def test_managed_request_target_reports_empty_dns_as_dns_failure(monkeypatch):
    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses([]),
    )

    with pytest.raises(media_url_source.MediaUrlError) as exc_info:
        await media_url_source._managed_request_target(
            "https://missing.example/demo.mp4"
        )

    assert exc_info.value.code == "MEDIA_URL_DNS_FAILED"


@pytest.mark.asyncio
async def test_managed_redirect_uses_fresh_client_for_each_hostname(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    client_count = 0
    seen_requests = []

    def handler(request):
        seen_requests.append(request)
        if request.headers["host"] == "media.example":
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example/final.mp4"},
                request=request,
            )
        return httpx.Response(200, content=MP4_BYTES, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        nonlocal client_count
        client_count += 1
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="redirect-hosts",
        max_bytes=1024,
        expected_media_kind="video",
        request_headers={
            "Authorization": "Bearer redirect-token",
            "X-Custom-Media": "transparent",
        },
    )

    assert imported.file_path.read_bytes() == MP4_BYTES
    assert client_count == 2
    assert [request.headers["host"] for request in seen_requests] == [
        "media.example",
        "cdn.example",
    ]
    assert [request.headers["authorization"] for request in seen_requests] == [
        "Bearer redirect-token",
        "Bearer redirect-token",
    ]
    assert [request.headers["x-custom-media"] for request in seen_requests] == [
        "transparent",
        "transparent",
    ]


@pytest.mark.asyncio
async def test_managed_url_falls_back_across_validated_public_ips(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    attempted_ips = []

    def handler(request):
        attempted_ips.append(request.url.host)
        if request.url.host == "2001:4860:4860::8888":
            raise httpx.ConnectError("IPv6 unavailable", request=request)
        return httpx.Response(200, content=MP4_BYTES, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses([
            "2001:4860:4860::8888",
            "93.184.216.34",
        ]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="dual-stack",
        max_bytes=1024,
        expected_media_kind="video",
    )

    assert imported.file_path.read_bytes() == MP4_BYTES
    assert attempted_ips == ["2001:4860:4860::8888", "93.184.216.34"]


@pytest.mark.asyncio
async def test_managed_url_cleans_partial_before_fallback_after_read_error(
    tmp_path,
    monkeypatch,
):
    original_client = httpx.AsyncClient
    attempted_ips = []

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield MP4_BYTES[:12]
            raise httpx.ReadError("stream interrupted")

    def handler(request):
        attempted_ips.append(request.url.host)
        if request.url.host == "93.184.216.34":
            return httpx.Response(200, stream=InterruptedStream(), request=request)
        return httpx.Response(200, content=MP4_BYTES, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34", "93.184.216.35"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="read-error-fallback",
        max_bytes=1024,
        expected_media_kind="video",
    )

    assert imported.file_path.read_bytes() == MP4_BYTES
    assert attempted_ips == ["93.184.216.34", "93.184.216.35"]
    assert list(_staging_dir(tmp_path).glob("*.partial")) == []
