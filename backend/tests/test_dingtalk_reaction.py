from __future__ import annotations

import uuid

import pytest

from app.services import dingtalk_reaction, dingtalk_stream, turn_inbox
from app.services.dingtalk_reaction import DingTalkReactionController, resolve_tool_reaction


def test_resolve_tool_reaction_maps_common_tool_types():
    assert resolve_tool_reaction("web_search", {}) == "🌐"
    assert resolve_tool_reaction("browser.open", {}) == "🔗"
    assert resolve_tool_reaction("read_file", {}) == "📂"
    assert resolve_tool_reaction("edit_file", {}) == "✍️"
    assert resolve_tool_reaction("execute_code_aio", {}) == "🛠️"
    assert resolve_tool_reaction("svc", {"command": "npm install left-pad"}) == "📦"


@pytest.mark.asyncio
async def test_durable_cleanup_does_not_accept_only_wrong_reaction_success(
    monkeypatch,
):
    async def fake_post_reaction(**kwargs) -> bool:
        return kwargs["reaction_name"] != dingtalk_reaction.DEFAULT_THINKING_REACTION

    monkeypatch.setattr(dingtalk_reaction, "_post_reaction", fake_post_reaction)
    assert await dingtalk_reaction.cleanup_durable_progress_reactions(
        "robot",
        "secret",
        "provider-message",
        "provider-conversation",
    ) is False


@pytest.mark.asyncio
async def test_healthy_dingtalk_terminal_uses_one_live_recall_then_acks_marker(
    monkeypatch,
):
    recalls: list[str] = []
    acknowledgements: list[dict] = []

    async def attach(*_args) -> bool:
        return True

    async def recall(*args) -> bool:
        recalls.append(args[-1])
        return True

    async def acknowledge(**kwargs) -> None:
        acknowledgements.append(kwargs)

    async def fail_if_durable_sweep_runs(**_kwargs) -> bool:
        raise AssertionError("healthy terminal must not run the nine-reaction sweep")

    monkeypatch.setattr(dingtalk_reaction, "add_reaction", attach)
    monkeypatch.setattr(dingtalk_reaction, "recall_reaction", recall)
    monkeypatch.setattr(
        turn_inbox,
        "acknowledge_durable_channel_receipt_cleanup",
        acknowledge,
    )
    monkeypatch.setattr(
        turn_inbox,
        "cleanup_durable_channel_receipt_anchor",
        fail_if_durable_sweep_runs,
    )
    reactions = dingtalk_stream._make_dingtalk_reactions(
        "robot",
        "secret",
        "provider-message",
        "provider-conversation",
    )
    agent_id = uuid.uuid4()
    local_message_id = uuid.uuid4()
    assert reactions.bind_receipt_context is not None
    reactions.bind_receipt_context(agent_id, str(uuid.uuid4()), local_message_id)

    await reactions.on_consume()
    assert await reactions.on_complete("done") is True

    assert recalls == [dingtalk_reaction.DEFAULT_THINKING_REACTION]
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["agent_id"] == agent_id
    assert acknowledgements[0]["message_id"] == local_message_id


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
