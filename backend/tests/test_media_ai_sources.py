"""Original URL preservation, bounded probing and generic AgentDir signing."""

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.services import media_ai_io
from app.services.agent_file_urls import presign_agent_file, verify_agent_file_ticket
from app.services.media_ai_io import MediaInput
from app.services.media_ai_provider import generation_payload
from test_media_ai_provider import MP4, PNG, WAV, config, mock_http

pytestmark = pytest.mark.asyncio


async def test_opaque_third_party_url_is_preserved_and_not_downloaded_when_kind_known(monkeypatch):
    calls = []

    async def validate(url):
        calls.append(url)

    async def forbidden(*args, **kwargs):
        pytest.fail("Known media kind must not download a large input")

    monkeypatch.setattr(media_ai_io, "_managed_request_target", validate)
    monkeypatch.setattr(media_ai_io, "_read_url", forbidden)
    url = "https://third.example/object?id=123&Signature=a%2Fb%3D&Expires=999999"
    items = await media_ai_io.load_media(uuid.uuid4(), [{"source": url, "kind": "video"}])
    assert items[0].data_url == url and items[0].data == b""
    assert calls == [url]


async def test_large_agentdir_input_uses_ranges_and_signing_without_full_read(monkeypatch):
    reads = []
    signed = []

    class Storage:
        async def stat(self, key):
            return SimpleNamespace(size=3 * 1024 ** 3, is_dir=False)

        async def read_range(self, key, start, end):
            reads.append((start, end))
            return MP4

        async def read_bytes(self, key):
            pytest.fail("Large media input must never be fully loaded")

    async def sign(agent_id, path, mime):
        signed.append((path, mime))
        return "https://storage.example/signed-video"

    monkeypatch.setattr(media_ai_io, "get_storage_backend", Storage)
    monkeypatch.setattr(media_ai_io, "presign_agent_file", sign)
    items = await media_ai_io.load_media(uuid.uuid4(), ["/custom/reports/long-video.mp4"])
    assert items[0].data_url == "https://storage.example/signed-video"
    assert signed == [("custom/reports/long-video.mp4", "video/mp4")]
    assert reads == [(0, media_ai_io.PROBE_BYTES - 1)]


async def test_local_audio_ticket_is_scoped_to_exact_agent_and_path(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from app.services import agent_file_urls

    class Storage:
        async def presign_download_url(self, *args, **kwargs):
            return None

    async def public_base():
        return "https://platform.example"

    monkeypatch.setattr(agent_file_urls, "get_storage_backend", Storage)
    monkeypatch.setattr(agent_file_urls, "_public_base_url", public_base)
    agent_id = uuid.uuid4()
    url = await presign_agent_file(agent_id, "records/meeting.wav", "audio/wav")
    ticket = parse_qs(urlsplit(url).query)["im_ticket"][0]
    value = verify_agent_file_ticket(agent_id, "records/meeting.wav", ticket)
    assert value["mime_type"] == "audio/wav"
    assert verify_agent_file_ticket(uuid.uuid4(), "records/meeting.wav", ticket) is None
    assert verify_agent_file_ticket(agent_id, "records/other.wav", ticket) is None


async def test_opaque_url_kind_is_detected_from_headers_without_suffix(monkeypatch):
    from app.services import media_url_source

    async def resolve(*args):
        return ["8.8.8.8"]

    def upstream(request):
        assert request.headers["range"].startswith("bytes=0-")
        return httpx.Response(206, content=WAV, headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(media_url_source, "_resolve_host", resolve)
    mock_http(monkeypatch, upstream)
    url = "https://third.example/get?token=original%2Fsignature"
    items = await media_ai_io.load_media(uuid.uuid4(), [url])
    assert items[0].kind == "audio" and items[0].url == url


async def test_video_accepts_first_last_frames_and_multimodal_references():
    inputs = [
        MediaInput("first", "image/png", PNG, role="first_frame"),
        MediaInput("last", "image/png", PNG, role="last_frame"),
        MediaInput("video", "video/mp4", MP4, role="reference_video"),
        MediaInput("audio", "audio/wav", WAV, role="reference_audio"),
    ]
    _, payload = generation_payload(config(), {"output_type": "video", "prompt": "Continue", "duration": -1, "resolution": "480P"}, inputs)
    assert [item["type"] for item in payload["input"]["media"]] == [item.role for item in inputs]
    assert payload["parameters"]["duration"] == -1
