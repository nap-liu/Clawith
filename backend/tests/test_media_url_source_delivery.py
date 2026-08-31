"""Delivery, logging, and concurrency tests for managed media URLs."""

import asyncio

import httpx
import pytest
from loguru import logger

from app.services import media_url_source
from media_url_source_support import MP4_BYTES, SESSION_ID, _async_addresses, _staging_dir

@pytest.mark.asyncio
async def test_managed_import_logs_exact_request_and_response(tmp_path, monkeypatch):
    original_client = httpx.AsyncClient
    secret = "SIGNED-URL-987"
    authorization = "Bearer AUTH-HEADER-654"
    cookie = "media_session=COOKIE-321"
    signed_url = f"https://media.example/demo.mp4?token={secret}"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"X-Origin-Debug": "response-header-value"},
            content=MP4_BYTES,
            request=request,
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return original_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        media_url_source,
        "_resolve_host",
        lambda *_args: _async_addresses(["93.184.216.34"]),
    )
    monkeypatch.setattr(media_url_source.httpx, "AsyncClient", client_factory)

    captured = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    try:
        imported = await media_url_source.import_managed_media_url(
            signed_url,
            agent_workspace=tmp_path,
            session_id=SESSION_ID,
            intent_id="logging-complete",
            max_bytes=1024,
            expected_media_kind="video",
            request_headers={
                "Authorization": authorization,
                "Cookie": cookie,
                "X-Business-Trace": "business-trace-123",
                "Host": "untrusted.example",
                "Accept-Encoding": "identity",
                "X-Clawith-Trace": "internal",
            },
        )
    finally:
        logger.remove(sink_id)

    combined = "".join(captured)
    assert imported.file_path.is_file()
    assert signed_url in combined
    assert authorization in combined
    assert cookie in combined
    assert '"body":""' in combined
    assert '"status_code":200' in combined
    assert "response-header-value" in combined
    assert str(tmp_path) not in combined
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["authorization"] == authorization
    assert request.headers["cookie"] == cookie
    assert request.headers["x-business-trace"] == "business-trace-123"
    assert request.headers["host"] == "media.example"
    assert request.headers.get("accept-encoding") != "identity"
    assert "x-clawith-trace" not in request.headers
    assert request.headers["user-agent"].startswith("Mozilla/5.0")
    assert "Clawith" not in request.headers["user-agent"]
    assert not any(name.lower().startswith("x-clawith-") for name in request.headers)


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

    assert first.workspace_path == second.workspace_path
    assert first.file_path.read_bytes() == MP4_BYTES
    assert (tmp_path / first.workspace_path).read_bytes() == MP4_BYTES
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

    assert first.workspace_path != second.workspace_path
    assert first.file_path.read_bytes().endswith(b"ONE")
    assert second.file_path.read_bytes().endswith(b"TWO")
    assert (tmp_path / first.workspace_path).read_bytes().endswith(b"ONE")
    assert (tmp_path / second.workspace_path).read_bytes().endswith(b"TWO")
    assert fetch_count == 2


