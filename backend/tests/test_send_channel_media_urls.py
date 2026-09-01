"""URL-source media delivery tests."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import agent_tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        ({}, "INVALID_MEDIA_SOURCE"),
        ({"file_path": "workspace/a.mp3", "url": "https://example.com/a.mp3"}, "INVALID_MEDIA_SOURCE"),
        ({"url": "https://example.com/a.mp3"}, "INVALID_URL_MODE"),
        ({"url": "https://example.com/a.mp3", "url_mode": "copy"}, "INVALID_URL_MODE"),
        ({"file_path": "workspace/a.mp3", "headers": {}}, "INVALID_MEDIA_HEADERS"),
        ({
            "url": "https://example.com/a.mp3",
            "url_mode": "external",
            "headers": {"Authorization": "Bearer demo"},
        }, "INVALID_MEDIA_HEADERS"),
    ],
)
async def test_send_media_requires_one_explicit_source(tmp_path, arguments, expected_code):
    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {**arguments, "session_id": str(uuid.uuid4())},
        media_kind="audio",
        tool_call_id="call-source",
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == expected_code


@pytest.mark.asyncio
async def test_external_url_publishes_without_downloading(tmp_path, monkeypatch):
    captured = {}

    async def fake_validate(url, *, external):
        assert external is True
        return url

    async def fake_publish(**kwargs):
        captured.update(kwargs)
        return json.dumps({"type": "platform_media_delivery", "status": "sent"})

    async def fail_import(*args, **kwargs):
        raise AssertionError("external URL must not be downloaded")

    monkeypatch.setattr(agent_tools, "validate_media_url", fake_validate)
    monkeypatch.setattr(agent_tools, "_publish_external_media_to_session", fake_publish)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", fail_import)
    monkeypatch.setattr(agent_tools, "_get_tool_config", lambda *_args: _async_value({"allow_download": True}))

    target_session = str(uuid.uuid4())
    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "media_type": "video",
            "url": "https://media.example/demo.mp4",
            "url_mode": "external",
            "session_id": target_session,
        },
        media_kind="video",
        tool_call_id="call-external",
    ))

    assert payload["status"] == "sent"
    assert captured["media_url"] == "https://media.example/demo.mp4"
    assert captured["session_id"] == target_session
    assert captured["allow_download"] is True


@pytest.mark.asyncio
async def test_managed_url_replay_returns_before_preflight_or_download(tmp_path, monkeypatch):
    async def fake_replay(**_kwargs):
        return {
            "type": "platform_media_delivery",
            "status": "already_sent",
            "code": "MEDIA_ALREADY_SENT",
        }

    async def should_not_run(**_kwargs):
        raise AssertionError("terminal replay must not preflight or download")

    monkeypatch.setattr(agent_tools, "_replay_terminal_media_delivery", fake_replay)
    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", should_not_run)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", should_not_run)

    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "url": "https://expired.example/audio.mp3",
            "url_mode": "managed",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="audio",
        tool_call_id="call-replay",
        origin_session_id=str(uuid.uuid4()),
    ))

    assert payload["status"] == "already_sent"


@pytest.mark.asyncio
async def test_managed_url_invalid_target_is_rejected_before_download(tmp_path, monkeypatch):
    async def no_replay(**_kwargs):
        return None

    async def fake_preflight(**_kwargs):
        return {
            "type": "media_delivery_result",
            "version": 1,
            "status": "failed",
            "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
            "media_kind": "audio",
        }

    async def should_not_download(*_args, **_kwargs):
        raise AssertionError("invalid target must not download")

    monkeypatch.setattr(agent_tools, "_replay_terminal_media_delivery", no_replay)
    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", fake_preflight)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", should_not_download)

    payload = json.loads(await agent_tools._send_channel_media(
        uuid.uuid4(),
        tmp_path,
        {
            "url": "https://media.example/audio.mp3",
            "url_mode": "managed",
            "session_id": str(uuid.uuid4()),
        },
        media_kind="audio",
        tool_call_id="call-invalid-target",
    ))

    assert payload["status"] == "failed"
    assert payload["code"] == "SESSION_NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.asyncio
async def test_managed_url_uses_origin_session_result_scope_and_agent_media_storage(
    tmp_path,
    monkeypatch,
):
    captured = {}
    agent_id = uuid.uuid4()
    origin_session_id = str(uuid.uuid4())
    target_session_id = str(uuid.uuid4())
    managed_file = tmp_path / "media" / "imported" / "managed-demo.mp4"
    managed_file.parent.mkdir(parents=True)
    managed_file.write_bytes(
        b"\x00\x00\x00\x18ftypmp42hdlr\x00\x00\x00\x00\x00\x00\x00\x00vide"
    )

    async def no_replay(**_kwargs):
        return None

    async def valid_target(**_kwargs):
        return None

    async def fake_import(_url, **kwargs):
        captured["import"] = kwargs
        def close_import():
            captured["import_closed"] = True

        return SimpleNamespace(
            file_path=managed_file,
            workspace_path="media/imported/managed-demo.mp4",
            mime_type="video/mp4",
            close=close_import,
        )

    class Storage:
        async def write_local_file(self, key, path, *, content_type=None):
            captured["storage"] = (key, path, content_type)

    async def fake_send(**kwargs):
        captured["send"] = kwargs
        return json.dumps({"type": "platform_media_delivery", "status": "sent"})

    monkeypatch.setattr(agent_tools, "_replay_terminal_media_delivery", no_replay)
    monkeypatch.setattr(agent_tools, "_preflight_managed_media_target", valid_target)
    monkeypatch.setattr(agent_tools, "import_managed_media_url", fake_import)
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(agent_tools, "_get_tool_config", lambda *_args: _async_value({}))
    monkeypatch.setattr(agent_tools, "_send_media_to_session", fake_send)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)

    payload = json.loads(await agent_tools._send_channel_media(
        agent_id,
        tmp_path,
        {
            "url": "https://media.example/demo.mp4",
            "url_mode": "managed",
            "headers": {
                "Authorization": "Bearer media-token",
                "X-Media-Tenant": "tenant-a",
            },
            "session_id": target_session_id,
        },
        media_kind="video",
        tool_call_id="call-managed-layout",
        origin_session_id=origin_session_id,
    ))

    assert payload["status"] == "sent"
    assert captured["import"]["session_id"] == origin_session_id
    assert captured["import"]["operation_scope"] == (
        f"outbound:{agent_id}:{origin_session_id}:unanchored:call-managed-layout"
    )
    assert captured["import"]["request_headers"] == {
        "Authorization": "Bearer media-token",
        "X-Media-Tenant": "tenant-a",
    }
    assert captured["storage"] == (
        f"{agent_id}/media/imported/managed-demo.mp4",
        managed_file,
        "video/mp4",
    )
    assert captured["send"]["workspace_path"] == "media/imported/managed-demo.mp4"
    assert captured["import_closed"] is True


async def _async_value(value):
    return value
