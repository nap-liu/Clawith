import asyncio
import logging

import httpx
import pytest
from loguru import logger

from app.core.logging_config import intercept_standard_logging
from app.services import media_url_source

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
SESSION_ID = "session-managed"


def _staging_dir(agent_root, session_id=SESSION_ID):
    return agent_root / ".tool_results" / session_id / ".media"


@pytest.mark.asyncio
async def test_managed_url_streams_into_agent_media_store_and_finishes_atomically(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200,
        headers={"content-type": "video/mp4"},
        content=MP4_BYTES,
        request=request,
    ))

    async def fake_validate(url, *, external):
        assert external is False
        return url

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(media_url_source, "validate_media_url", fake_validate)
    monkeypatch.setattr(media_url_source, "_resolve_host", lambda *_args: _async_addresses(["93.184.216.34"]))
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    imported = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4",
        agent_workspace=tmp_path,
        session_id=SESSION_ID,
        intent_id="call-managed",
        max_bytes=1024,
        expected_media_kind="video",
    )

    assert imported.file_path.read_bytes() == MP4_BYTES
    assert imported.workspace_path.startswith("media/imported/")
    assert imported.mime_type == "video/mp4"
    assert list(_staging_dir(tmp_path).glob("*.partial")) == []


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
    seen_hosts = []

    def handler(request):
        seen_hosts.append(request.headers["host"])
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
    )

    assert imported.file_path.read_bytes() == MP4_BYTES
    assert client_count == 2
    assert seen_hosts == ["media.example", "cdn.example"]


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
async def test_managed_import_never_logs_signed_url_or_local_path(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    secret = "DO-NOT-LOG-987"
    signed_url = f"https://media.example/demo.mp4?token={secret}"
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200,
        content=MP4_BYTES,
        request=request,
    ))

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    intercept_standard_logging()
    captured = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    try:
        imported = await media_url_source.import_managed_media_url(
            signed_url,
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="logging-redaction",
            max_bytes=1024,
            expected_media_kind="video",
        )
        logging.getLogger("httpx").warning("managed media transport warning")
    finally:
        logger.remove(sink_id)

    combined = "".join(captured)
    assert imported.file_path.is_file()
    assert "managed media transport warning" in combined
    assert secret not in combined
    assert signed_url not in combined
    assert str(tmp_path) not in combined


@pytest.mark.asyncio
async def test_managed_ipv6_literal_uses_bracketed_host_header(monkeypatch):
    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["2001:4860:4860::8888"]),
    )

    target = await media_url_source._managed_request_target(
        "https://[2001:4860:4860::8888]/demo.mp4"
    )

    assert target.host_header == "[2001:4860:4860::8888]"
    assert target.connect_urls == (
        "https://[2001:4860:4860::8888]/demo.mp4",
    )


@pytest.mark.asyncio
async def test_concurrent_managed_imports_converge_on_one_atomic_final(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient

    async def handler(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, content=MP4_BYTES, request=request)

    transport = httpx.MockTransport(handler)

    async def fake_validate(url, *, external):
        return url

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(media_url_source, "validate_media_url", fake_validate)
    monkeypatch.setattr(media_url_source, "_resolve_host", lambda *_args: _async_addresses(["93.184.216.34"]))
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    first, second = await asyncio.gather(*[
        media_url_source.import_managed_media_url(
            "https://media.example/demo.mp4",
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="same-intent",
            max_bytes=1024,
            expected_media_kind="video",
        )
        for _ in range(2)
    ])

    assert first.file_path == second.file_path
    assert first.file_path.read_bytes() == MP4_BYTES
    assert list(_staging_dir(tmp_path).glob("*.partial")) == []


@pytest.mark.asyncio
async def test_same_provider_intent_in_different_sessions_does_not_reuse_media(
    tmp_path,
    monkeypatch,
):
    original_client = httpx.AsyncClient
    fetch_count = 0

    def handler(request):
        nonlocal fetch_count
        fetch_count += 1
        suffix = b"ONE" if request.url.params.get("variant") == "one" else b"TWO"
        return httpx.Response(200, content=MP4_BYTES + suffix, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    first = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4?variant=one",
        agent_workspace=tmp_path,
        session_id="session-one",
        intent_id="provider-call-1",
        max_bytes=1024,
        expected_media_kind="video",
    )
    second = await media_url_source.import_managed_media_url(
        "https://media.example/demo.mp4?variant=two",
        agent_workspace=tmp_path,
        session_id="session-two",
        intent_id="provider-call-1",
        max_bytes=1024,
        expected_media_kind="video",
    )

    assert first.file_path != second.file_path
    assert first.file_path.read_bytes().endswith(b"ONE")
    assert second.file_path.read_bytes().endswith(b"TWO")
    assert fetch_count == 2


async def _async_addresses(value):
    return value
