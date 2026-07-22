"""Behavior tests for historical image rehydration."""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

from app.services import image_context
from app.services.llm.caller import _convert_messages_for_vision
from app.services.llm.client import LLMMessage
from app.services.llm.compactor import estimate_prompt_tokens


def test_rehydrated_history_uses_structured_multimodal_content(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    upload_dir = tmp_path / str(agent_id) / "workspace" / "uploads"
    upload_dir.mkdir(parents=True)
    image_bytes = b"historical-image-bytes"
    (upload_dir / "store.jpg").write_bytes(image_bytes)
    monkeypatch.setattr(
        image_context,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    messages = [{"role": "user", "content": "[file:store.jpg]\n请分析图片"}]
    result = image_context.rehydrate_image_messages(messages, agent_id, max_images=1)

    assert messages[0]["content"] == "[file:store.jpg]\n请分析图片"
    assert result[0]["content"] == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
            },
        },
        {"type": "text", "text": "[file:store.jpg]\n请分析图片"},
    ]
    assert estimate_prompt_tokens(result) < 400

    non_vision = _convert_messages_for_vision(
        [LLMMessage(role="user", content=result[0]["content"])],
        supports_vision=False,
    )
    assert non_vision[0].content == "[file:store.jpg]\n请分析图片"
    assert "base64" not in non_vision[0].content
