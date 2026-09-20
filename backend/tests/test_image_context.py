"""Behavior tests for model-aware image attachment preparation."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

from app.services import image_context
from app.services.chat_attachments import attachment_from_workspace_path

_LEGACY_PNG_BYTES = b"\x89PNG\r\n\x1a\nlegacy"
_LEGACY_PNG_DATA_URL = (
    "data:image/png;base64," + base64.b64encode(_LEGACY_PNG_BYTES).decode("ascii")
)


class _MemoryStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def stat(self, key: str):
        return SimpleNamespace(size=len(self.files[key]))

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        return self.files[key][start : end + 1]


async def test_non_vision_model_receives_accessible_path_without_image_payload(monkeypatch):
    agent_id = uuid.uuid4()
    path = "workspace/uploads/store.jpg"
    storage = _MemoryStorage({f"{agent_id}/{path}": b"image-bytes"})
    monkeypatch.setattr(image_context, "get_storage_backend", lambda: storage)

    prepared = await image_context.prepare_messages_for_model(
        [
            {
                "role": "user",
                "content": "请分析图片",
                "attachments": [attachment_from_workspace_path(path)],
            }
        ],
        agent_id=agent_id,
        supports_vision=False,
    )

    assert prepared[0]["content"] == (
        "[图片附件]\n"
        "文件名：store.jpg\n"
        "路径：workspace/uploads/store.jpg\n\n"
        "请分析图片"
    )
    assert "base64" not in prepared[0]["content"]
    assert "image_url" not in prepared[0]["content"]


async def test_vision_model_receives_image_and_keeps_accessible_path(monkeypatch):
    agent_id = uuid.uuid4()
    path = "workspace/uploads/store.jpg"
    image_bytes = b"\xff\xd8\xffimage-bytes"
    storage = _MemoryStorage({f"{agent_id}/{path}": image_bytes})
    monkeypatch.setattr(image_context, "get_storage_backend", lambda: storage)

    prepared = await image_context.prepare_messages_for_model(
        [
            {
                "role": "user",
                "content": "请分析图片",
                "attachments": [attachment_from_workspace_path(path)],
            }
        ],
        agent_id=agent_id,
        supports_vision=True,
    )

    assert prepared[0]["content"][0] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        },
    }
    assert prepared[0]["content"][1]["type"] == "text"
    assert "路径：workspace/uploads/store.jpg" in prepared[0]["content"][1]["text"]
    assert "请分析图片" in prepared[0]["content"][1]["text"]


async def test_non_vision_model_strips_legacy_image_data_but_keeps_exact_file_path(monkeypatch):
    agent_id = uuid.uuid4()
    path = "workspace/uploads/legacy.png"
    storage = _MemoryStorage({f"{agent_id}/{path}": b"legacy"})
    monkeypatch.setattr(image_context, "get_storage_backend", lambda: storage)

    prepared = await image_context.prepare_messages_for_model(
        [
            {
                "role": "user",
                "content": (
                    "[file:legacy.png]\n"
                    "[image_data:data:image/png;base64,bGVnYWN5]\n"
                    "看看旧图片"
                ),
            }
        ],
        agent_id=agent_id,
        supports_vision=False,
    )

    assert "路径：workspace/uploads/legacy.png" in prepared[0]["content"]
    assert "看看旧图片" in prepared[0]["content"]
    assert "image_data" not in prepared[0]["content"]
    assert "base64" not in prepared[0]["content"]


async def test_non_vision_model_does_not_invent_path_for_pathless_legacy_image():
    prepared = await image_context.prepare_messages_for_model(
        [
            {
                "role": "user",
                "content": "[image_data:data:image/png;base64,bGVnYWN5]\n看看旧图片",
            }
        ],
        agent_id=uuid.uuid4(),
        supports_vision=False,
    )

    assert prepared[0]["content"] == "[历史图片附件未记录可访问路径]\n\n看看旧图片"
    assert "workspace/uploads" not in prepared[0]["content"]


async def test_vision_model_keeps_pathless_legacy_image_compatible():
    data_url = _LEGACY_PNG_DATA_URL
    prepared = await image_context.prepare_messages_for_model(
        [
            {
                "role": "user",
                "content": f"[image_data:{data_url}]\n看看旧图片",
            }
        ],
        agent_id=uuid.uuid4(),
        supports_vision=True,
    )

    assert prepared[0]["content"] == [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": "看看旧图片"},
    ]


async def test_missing_attachment_path_is_reported_as_unavailable(monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(image_context, "get_storage_backend", lambda: _MemoryStorage({}))

    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": "请检查",
            "attachments": [attachment_from_workspace_path("workspace/uploads/missing.png")],
        }],
        agent_id=agent_id,
        supports_vision=False,
    )

    assert "路径：workspace/uploads/missing.png" in prepared[0]["content"]
    assert "状态：当前不可访问" in prepared[0]["content"]


async def test_vision_rejects_non_image_bytes_even_when_metadata_claims_image(monkeypatch):
    agent_id = uuid.uuid4()
    path = "workspace/uploads/not-really.png"
    monkeypatch.setattr(
        image_context,
        "get_storage_backend",
        lambda: _MemoryStorage({f"{agent_id}/{path}": b"plain secret document"}),
    )

    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": "请看",
            "attachments": [{
                "display_name": "not-really.png",
                "path": path,
                "kind": "image",
                "mime_type": "image/png",
            }],
        }],
        agent_id=agent_id,
        supports_vision=True,
    )

    assert isinstance(prepared[0]["content"], str)
    assert "base64" not in prepared[0]["content"]


async def test_all_legacy_historical_images_reach_the_vision_model():
    messages = [
        {
            "role": "user",
            "content": f"[image_data:{_LEGACY_PNG_DATA_URL}]\nturn-{index}",
        }
        for index in range(5)
    ]

    prepared = await image_context.prepare_messages_for_model(
        messages,
        agent_id=uuid.uuid4(),
        supports_vision=True,
    )

    assert [isinstance(message["content"], list) for message in prepared] == [
        True,
        True,
        True,
        True,
        True,
    ]


async def test_structured_and_legacy_images_are_all_retained(monkeypatch):
    agent_id = uuid.uuid4()
    files: dict[str, bytes] = {}
    messages: list[dict] = []
    for index in range(4):
        if index % 2:
            path = f"workspace/uploads/{index}.png"
            files[f"{agent_id}/{path}"] = b"\x89PNG\r\n\x1a\nimage"
            messages.append({
                "role": "user",
                "content": f"turn-{index}",
                "attachments": [attachment_from_workspace_path(path)],
            })
        else:
            messages.append({
                "role": "user",
                "content": f"[image_data:{_LEGACY_PNG_DATA_URL}]\nturn-{index}",
            })
    messages.append({"role": "user", "content": "current text only"})
    monkeypatch.setattr(image_context, "get_storage_backend", lambda: _MemoryStorage(files))

    prepared = await image_context.prepare_messages_for_model(
        messages,
        agent_id=agent_id,
        supports_vision=True,
    )

    assert [isinstance(message["content"], list) for message in prepared] == [
        True,
        True,
        True,
        True,
        False,
    ]


async def test_multiple_current_images_are_not_aggregate_byte_truncated(monkeypatch):
    agent_id = uuid.uuid4()
    first = b"\x89PNG\r\n\x1a\nfirst"
    second = b"\x89PNG\r\n\x1a\nsecond"
    paths = ["workspace/uploads/first.png", "workspace/uploads/second.png"]
    monkeypatch.setattr(
        image_context,
        "get_storage_backend",
        lambda: _MemoryStorage({
            f"{agent_id}/{paths[0]}": first,
            f"{agent_id}/{paths[1]}": second,
        }),
    )

    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": "比较",
            "attachments": [attachment_from_workspace_path(path) for path in paths],
        }],
        agent_id=agent_id,
        supports_vision=True,
    )

    image_blocks = [part for part in prepared[0]["content"] if part["type"] == "image_url"]
    assert len(image_blocks) == 2
    assert all(path in prepared[0]["content"][-1]["text"] for path in paths)


async def test_invalid_legacy_base64_is_not_sent_to_vision_provider():
    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": "[image_data:data:image/png;base64,bm90LWltYWdl]\n请看",
        }],
        agent_id=uuid.uuid4(),
        supports_vision=True,
    )

    assert isinstance(prepared[0]["content"], str)
    assert "base64" not in prepared[0]["content"]
    assert "未记录可访问路径" in prepared[0]["content"]


async def test_authoritative_empty_attachments_never_reparse_literal_file_marker(monkeypatch):
    agent_id = uuid.uuid4()
    path = "workspace/uploads/private.png"
    monkeypatch.setattr(
        image_context,
        "get_storage_backend",
        lambda: _MemoryStorage({f"{agent_id}/{path}": b"\x89PNG\r\n\x1a\nprivate"}),
    )

    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": "literal [file:private.png]",
            "attachments": [],
        }],
        agent_id=agent_id,
        supports_vision=True,
    )

    assert prepared == [{"role": "user", "content": "literal [file:private.png]"}]


async def test_oversized_legacy_image_is_rejected_before_provider_payload(monkeypatch):
    monkeypatch.setattr(image_context, "MAX_IMAGE_BYTES", 8)
    oversized = b"\x89PNG\r\n\x1a\nmore-than-eight"
    data_url = "data:image/png;base64," + base64.b64encode(oversized).decode("ascii")

    prepared = await image_context.prepare_messages_for_model(
        [{"role": "user", "content": f"[image_data:{data_url}]\n请看"}],
        agent_id=uuid.uuid4(),
        supports_vision=True,
    )

    assert isinstance(prepared[0]["content"], str)
    assert "base64" not in prepared[0]["content"]
    assert "未记录可访问路径" in prepared[0]["content"]


async def test_responses_input_image_is_normalized_without_shape_error():
    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": _LEGACY_PNG_DATA_URL},
                {"type": "text", "text": "inspect"},
            ],
        }],
        agent_id=uuid.uuid4(),
        supports_vision=True,
    )

    assert prepared[0]["content"][0] == {
        "type": "image_url",
        "image_url": {"url": _LEGACY_PNG_DATA_URL},
    }


async def test_anthropic_base64_image_is_normalized_without_aggregate_budget():
    prepared = await image_context.prepare_messages_for_model(
        [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(_LEGACY_PNG_BYTES).decode("ascii"),
                },
            }],
        }],
        agent_id=uuid.uuid4(),
        supports_vision=True,
    )

    assert prepared[0]["content"][0] == {
        "type": "image_url",
        "image_url": {"url": _LEGACY_PNG_DATA_URL},
    }
