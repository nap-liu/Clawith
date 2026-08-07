import time
from types import SimpleNamespace

import pytest

from app.api.files import (
    _message_references_media_path,
    _parse_media_range,
    _storage_entry_version_token,
)
from app.services import media_playback
from app.services.storage_runtime.base import StorageEntry
from app.services.storage_runtime.local import LocalStorageBackend


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, ttl):
        if key in self.values:
            self.ttls[key] = ttl
            return True
        return False

    async def delete(self, key):
        self.values.pop(key, None)


def test_parse_media_range_caps_large_requests():
    start, end, partial = _parse_media_range("bytes=100-99999999", 100_000_000)
    assert start == 100
    assert end - start + 1 == media_playback.MAX_RANGE_BYTES
    assert partial is True


def test_parse_media_range_supports_suffix():
    assert _parse_media_range("bytes=-50", 1000) == (950, 999, True)


def test_parse_media_range_without_header_returns_full_object():
    assert _parse_media_range(None, 100_000_000) == (0, 99_999_999, False)


def test_storage_entry_version_prefers_object_version():
    entry = StorageEntry(
        name="demo.mp4",
        key="demo.mp4",
        is_dir=False,
        size=123,
        modified_at="1",
        etag="etag-value",
        version_id="version-value",
    )
    assert _storage_entry_version_token(entry) == "version-value"


def test_message_media_reference_requires_an_exact_structured_path():
    path = "workspace/uploads/demo.mp4"
    structured = SimpleNamespace(
        message_meta={"attachments": [{"path": path}]},
        content="",
    )
    tool_result = SimpleNamespace(
        message_meta={},
        role="tool_call",
        content='{"name":"send_channel_file","status":"done","result":"{\\"type\\":\\"platform_file_delivery\\",\\"path\\":\\"workspace/uploads/demo.mp4\\"}"}',
    )
    unrelated = SimpleNamespace(
        message_meta={},
        role="user",
        content='{"message":"workspace/uploads/demo.mp4.bak"}',
    )

    assert _message_references_media_path(structured, path)
    assert _message_references_media_path(tool_result, path)
    assert not _message_references_media_path(unrelated, path)


def test_message_media_reference_accepts_exact_legacy_file_marker():
    path = "workspace/uploads/demo.mp4"
    legacy = SimpleNamespace(
        message_meta={"source_channel": "dingtalk"},
        role="user",
        content="[file:demo.mp4]\n",
    )
    wrong = SimpleNamespace(
        message_meta={"source_channel": "dingtalk"},
        role="user",
        content="[file:demo.mp4.bak]\n",
    )

    assert _message_references_media_path(legacy, path)
    assert not _message_references_media_path(wrong, path)


def test_message_media_reference_rejects_user_json_and_authoritative_empty_metadata():
    path = "workspace/uploads/demo.mp4"
    forged_json = SimpleNamespace(
        message_meta={},
        role="user",
        content='{"path":"workspace/uploads/demo.mp4"}',
    )
    authoritative_empty = SimpleNamespace(
        message_meta={"source_channel": "web", "attachments": []},
        role="user",
        content="[file:demo.mp4]",
    )

    assert not _message_references_media_path(forged_json, path)
    assert not _message_references_media_path(authoritative_empty, path)


def test_message_media_reference_rejects_assistant_marker_and_tool_arguments():
    path = "workspace/uploads/demo.mp4"
    assistant_marker = SimpleNamespace(
        message_meta={},
        role="assistant",
        content="[file:demo.mp4]",
    )
    unrelated_tool_args = SimpleNamespace(
        message_meta={},
        role="tool_call",
        content='{"name":"read_file","status":"done","args":{"path":"workspace/uploads/demo.mp4"},"result":"ok"}',
    )
    delivery_tool_args_only = SimpleNamespace(
        message_meta={},
        role="tool_call",
        content='{"name":"send_channel_file","status":"done","args":{"file_path":"workspace/uploads/demo.mp4"},"result":"failed"}',
    )

    assert not _message_references_media_path(assistant_marker, path)
    assert not _message_references_media_path(unrelated_tool_args, path)
    assert not _message_references_media_path(delivery_tool_args_only, path)


@pytest.mark.asyncio
async def test_playback_ticket_is_unique_signed_and_cookie_bound(monkeypatch):
    fake = _FakeRedis()

    async def fake_get_redis():
        return fake

    monkeypatch.setattr(media_playback, "get_redis", fake_get_redis)
    first, first_signature, cookie = await media_playback.create_ticket(
        user_id="user-1",
        agent_id="agent-1",
        path="workspace/uploads/demo.mp4",
        mime_type="video/mp4",
        size_bytes=123,
        version_token="version-1",
        message_id="message-1",
    )
    second, second_signature, _ = await media_playback.create_ticket(
        user_id="user-1",
        agent_id="agent-1",
        path="workspace/uploads/demo.mp4",
        mime_type="video/mp4",
        size_bytes=123,
    )

    assert first.ticket_id != second.ticket_id
    assert first_signature != second_signature
    assert media_playback.verify_signature(first.ticket_id, first_signature)
    assert media_playback.decode_cookie_user(cookie) == "user-1"
    loaded = await media_playback.load_ticket(first.ticket_id, touch=True)
    assert loaded == first
    assert loaded.version_token == "version-1"
    assert loaded.message_id == "message-1"
    assert loaded.absolute_expires_at > int(time.time())


@pytest.mark.asyncio
async def test_local_storage_reads_only_requested_range(tmp_path):
    backend = LocalStorageBackend(str(tmp_path))
    await backend.write_bytes("media.bin", b"0123456789")
    assert await backend.read_range("media.bin", 2, 5) == b"2345"
