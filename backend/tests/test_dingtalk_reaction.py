from __future__ import annotations

import pytest

from app.services.dingtalk_reaction import DingTalkReactionController, resolve_tool_reaction


def test_resolve_tool_reaction_maps_common_tool_types():
    assert resolve_tool_reaction("web_search", {}) == "🌐"
    assert resolve_tool_reaction("browser.open", {}) == "🔗"
    assert resolve_tool_reaction("read_file", {}) == "📂"
    assert resolve_tool_reaction("edit_file", {}) == "✍️"
    assert resolve_tool_reaction("execute_code_aio", {}) == "🛠️"
    assert resolve_tool_reaction("svc", {"command": "npm install left-pad"}) == "📦"


@pytest.mark.asyncio
async def test_dingtalk_reaction_controller_switches_from_thinking_to_tool_and_recalls_current():
    calls: list[tuple[str, str]] = []

    async def attach(reaction: str) -> bool:
        calls.append(("attach", reaction))
        return True

    async def recall(reaction: str) -> None:
        calls.append(("recall", reaction))

    controller = DingTalkReactionController(
        attach_reaction=attach,
        recall_reaction=recall,
        min_switch_interval_seconds=0,
        heartbeat_enabled=False,
    )

    await controller.on_consume()
    await controller.on_tool_call({"status": "running", "name": "web_search", "args": {}})
    await controller.on_complete("answer")

    assert calls == [
        ("attach", "🤔思考中"),
        ("recall", "🤔思考中"),
        ("attach", "🌐"),
        ("recall", "🌐"),
    ]


@pytest.mark.asyncio
async def test_dingtalk_reaction_controller_ignores_completed_tool_event():
    calls: list[tuple[str, str]] = []

    async def attach(reaction: str) -> bool:
        calls.append(("attach", reaction))
        return True

    async def recall(reaction: str) -> None:
        calls.append(("recall", reaction))

    controller = DingTalkReactionController(
        attach_reaction=attach,
        recall_reaction=recall,
        min_switch_interval_seconds=0,
        heartbeat_enabled=False,
    )

    await controller.on_consume()
    await controller.on_tool_call({"status": "done", "name": "web_search", "args": {}})
    await controller.on_complete("answer")

    assert calls == [
        ("attach", "🤔思考中"),
        ("recall", "🤔思考中"),
    ]


@pytest.mark.asyncio
async def test_dingtalk_reaction_controller_allows_first_tool_switch_with_min_interval():
    calls: list[tuple[str, str]] = []

    async def attach(reaction: str) -> bool:
        calls.append(("attach", reaction))
        return True

    async def recall(reaction: str) -> None:
        calls.append(("recall", reaction))

    controller = DingTalkReactionController(
        attach_reaction=attach,
        recall_reaction=recall,
        min_switch_interval_seconds=60,
        heartbeat_enabled=False,
    )

    await controller.on_consume()
    await controller.on_tool_call({"status": "running", "name": "read_file", "args": {}})
    await controller.on_complete("answer")

    assert calls == [
        ("attach", "🤔思考中"),
        ("recall", "🤔思考中"),
        ("attach", "📂"),
        ("recall", "📂"),
    ]
