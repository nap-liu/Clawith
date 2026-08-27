"""Focused unit tests for durable turn-origin delivery semantics."""

from __future__ import annotations

import uuid

import pytest

from app.services import turn_runtime


@pytest.mark.asyncio
async def test_on_message_subagent_origin_uses_history_when_live_mirror_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """A durable child reply needs no external adapter and must not be retried."""
    from app.api.websocket import manager

    agent_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())
    runtime = turn_runtime.TurnRuntime(
        session_found=True,
        source_channel="subagent",
        conversation_id=conversation_id,
        external_conv_id=None,
        is_group=False,
    )
    mirrored: list[tuple[str, str]] = []

    async def fake_load_turn_runtime(**_kwargs):
        return runtime

    async def unavailable_live_mirror(agent_key, session_key, _payload):
        mirrored.append((agent_key, session_key))
        raise RuntimeError("no live websocket viewer")

    def fail_if_transport_adapter_is_used(*_args, **_kwargs):
        raise AssertionError("subagent history has no external transport adapter")

    monkeypatch.setattr(turn_runtime, "load_turn_runtime", fake_load_turn_runtime)
    monkeypatch.setattr(manager, "send_to_session", unavailable_live_mirror)
    monkeypatch.setattr(turn_runtime, "run_channel_send", fail_if_transport_adapter_is_used)

    delivered = await turn_runtime.deliver_reply_to_origin(
        agent_id=agent_id,
        conversation_id=conversation_id,
        reply="already persisted on_message final",
        require_transport=True,
        expected_source_channel="subagent",
    )

    assert delivered is True
    assert mirrored == [(str(agent_id), conversation_id)]
