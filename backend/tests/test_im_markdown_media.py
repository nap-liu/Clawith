from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.api import feishu_shared
from app.services import im_markdown_media, turn_runtime
from app.services.im_delivery import IMDeliveryResult

pytestmark = pytest.mark.asyncio


class _PresigningStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.presign_calls: list[dict] = []

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        return self.files[key][start : end + 1]

    async def presign_download_url(self, key: str, **kwargs) -> str:
        self.presign_calls.append({"key": key, **kwargs})
        return f"/minio/{key}?existing-signature=yes"


async def test_agent_relative_images_become_existing_absolute_presigned_urls(monkeypatch):
    agent_id = uuid.uuid4()
    image_key = f"{agent_id}/workspace/reports/chart one.png"
    fake_key = f"{agent_id}/workspace/reports/not-image.jpg"
    storage = _PresigningStorage(
        {
            image_key: b"\x89PNG\r\n\x1a\nimage-bytes",
            fake_key: b"plain text",
        }
    )
    monkeypatch.setattr(im_markdown_media, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(
        im_markdown_media,
        "_public_base_url",
        AsyncMock(return_value="https://chat.example.com"),
    )

    original = (
        "报告如下：\n"
        "![趋势图](<workspace/reports/chart%20one.png> \"九月\")\n"
        "![再次引用](<workspace/reports/chart%20one.png>)\n"
        "![外部图](https://cdn.example.com/public.png)\n"
        "![伪图片](workspace/reports/not-image.jpg)\n"
        "![越界](../secret.png)\n"
        "![缺失](workspace/reports/missing.png)"
    )

    projected = await im_markdown_media.project_agent_images_for_im(agent_id, original)

    signed = (
        "https://chat.example.com/minio/"
        f"{agent_id}/workspace/reports/chart one.png?existing-signature=yes"
    )
    assert f'![趋势图](<{signed}> "九月")' in projected
    assert f"![再次引用](<{signed}>)" in projected
    assert "![外部图](https://cdn.example.com/public.png)" in projected
    assert "![伪图片](workspace/reports/not-image.jpg)" in projected
    assert "![越界](../secret.png)" in projected
    assert "![缺失](workspace/reports/missing.png)" in projected
    assert storage.presign_calls == [
        {
            "key": image_key,
            "filename": "chart one.png",
            "inline": True,
            "content_type": "image/png",
        }
    ]


async def test_shared_im_exit_projects_markdown_without_changing_history_channels(monkeypatch):
    agent_id = uuid.uuid4()
    projected_messages: list[str] = []
    delivered_messages: list[str] = []

    async def project(_agent_id, message):
        projected_messages.append(message)
        return message.replace("workspace/chart.png", "https://signed.example/chart.png")

    async def deliver(_agent_id, _runtime, reply, **_kwargs):
        delivered_messages.append(reply)
        return IMDeliveryResult.sent("slack")

    monkeypatch.setattr(turn_runtime, "project_agent_images_for_im", project)
    monkeypatch.setattr(turn_runtime, "_deliver_slack", deliver)
    markdown = "结果：![图](workspace/chart.png)"

    await turn_runtime.deliver_message_with_receipt(
        agent_id=agent_id,
        runtime=turn_runtime.TurnRuntime(True, "slack", "session", "C1", False),
        message=markdown,
    )
    assert projected_messages == [markdown]
    assert delivered_messages == ["结果：![图](https://signed.example/chart.png)"]

    web_deliver = AsyncMock(return_value=IMDeliveryResult.sent("web"))
    monkeypatch.setattr(turn_runtime, "_deliver_web", web_deliver)
    await turn_runtime.deliver_message_with_receipt(
        agent_id=agent_id,
        runtime=turn_runtime.TurnRuntime(True, "web", "session", None, False),
        message=markdown,
    )
    assert projected_messages == [markdown]
    assert web_deliver.await_args.args[2] == markdown


async def test_plain_text_im_delivery_does_not_project_markdown(monkeypatch):
    projector = AsyncMock(side_effect=AssertionError("plain text must not be projected"))
    monkeypatch.setattr(turn_runtime, "project_agent_images_for_im", projector)
    monkeypatch.setattr(
        turn_runtime,
        "_deliver_dingtalk",
        AsyncMock(return_value=IMDeliveryResult.sent("dingtalk")),
    )

    await turn_runtime.deliver_message_with_receipt(
        agent_id=uuid.uuid4(),
        runtime=turn_runtime.TurnRuntime(True, "dingtalk", "session", "staff", False),
        message="命令输出 ![不是图片](workspace/chart.png)",
        content_format="plain_text",
    )
    projector.assert_not_awaited()


async def test_each_projection_failure_isolated_and_original_markdown_preserved(monkeypatch):
    agent_id = uuid.uuid4()
    warning_calls: list[tuple] = []

    async def presign(_agent_id, path):
        if "/broken-" in path:
            raise RuntimeError("secret-signature-must-not-be-logged")
        return f"https://signed.example/{path}"

    class _Logger:
        def warning(self, *args):
            warning_calls.append(args)

    monkeypatch.setattr(im_markdown_media, "_presign_agent_image", presign)
    monkeypatch.setattr(im_markdown_media, "logger", _Logger())
    original = "\n".join(
        ["![异常地址](//[invalid)"]
        + [f"![坏图](workspace/broken-{index}.png)" for index in range(5)]
        + ["![好图](workspace/good.png)"]
    )

    projected = await im_markdown_media.project_agent_images_for_im(agent_id, original)

    assert "![坏图](workspace/broken-0.png)" in projected
    assert "![异常地址](//[invalid)" in projected
    assert "![好图](https://signed.example/workspace/good.png)" in projected
    assert len(warning_calls) == im_markdown_media._MAX_FAILURE_LOGS_PER_MESSAGE
    assert any(call[-1] == "RuntimeError" for call in warning_calls)
    assert "secret-signature" not in repr(warning_calls)


async def test_markdown_images_inside_fenced_and_inline_code_are_not_projected(monkeypatch):
    agent_id = uuid.uuid4()
    presign = AsyncMock(side_effect=lambda _agent_id, path: f"https://signed.example/{path}")
    monkeypatch.setattr(im_markdown_media, "_presign_agent_image", presign)
    original = (
        "示例 `![行内](workspace/inline.png)`\n"
        "```md\n![围栏](workspace/fenced.png)\n```\n"
        "真实：![图](workspace/real.png)"
    )

    projected = await im_markdown_media.project_agent_images_for_im(agent_id, original)

    assert "`![行内](workspace/inline.png)`" in projected
    assert "![围栏](workspace/fenced.png)" in projected
    assert "![图](https://signed.example/workspace/real.png)" in projected
    assert presign.await_args_list[0].args == (agent_id, "workspace/real.png")
    assert presign.await_count == 1


async def test_feishu_intermediate_card_projects_complete_relative_image(monkeypatch):
    agent_id = uuid.uuid4()
    presign = AsyncMock(
        return_value="https://signed.example/workspace/chart.png"
    )
    monkeypatch.setattr(im_markdown_media, "_presign_agent_image", presign)

    partial_card = await feishu_shared._build_projected_stream_card(
        agent_id,
        "文字 ![图](workspace/chart",
        agent_name="测试 Agent",
    )
    complete_card = await feishu_shared._build_projected_stream_card(
        agent_id,
        "文字 ![图](workspace/chart.png)",
        agent_name="测试 Agent",
    )

    assert partial_card["elements"][-1]["content"] == "文字 ![图](workspace/chart▌"
    assert complete_card["elements"][-1]["content"] == (
        "文字 ![图](https://signed.example/workspace/chart.png)▌"
    )
    presign.assert_awaited_once_with(agent_id, "workspace/chart.png")
