"""Behavior tests for model-aware image attachment preparation."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

from app.services import image_context
from app.services.chat_attachments import attachment_from_workspace_path


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
    image_bytes = b"image-bytes"
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
    data_url = "data:image/png;base64,bGVnYWN5"
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
