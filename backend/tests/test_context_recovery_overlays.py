"""Ephemeral loop instructions survive a durable context reload."""

from __future__ import annotations

import uuid

import pytest

from app.services.llm.caller import call_llm
from app.services.llm.client import LLMError
from tests.test_llm_throttle_retry import (
    _FakeModel,
    _ThrottleScriptClient,
    _patch_call_llm_collaborators,
    _stop_response,
    _tool_response,
)


@pytest.mark.asyncio
async def test_invalid_tool_retry_instruction_survives_context_recovery(monkeypatch):
    malformed = {
        "id": "call-invalid",
        "type": "function",
        "function": {"name": "", "arguments": "{}"},
    }
    client = _ThrottleScriptClient([
        _tool_response(malformed),
        LLMError("maximum context length exceeded"),
        _stop_response("recovered"),
    ])
    _patch_call_llm_collaborators(monkeypatch, client)

    async def recover(_model, _budget):
        return [{"role": "user", "content": "durable root"}]

    direct_call_llm = call_llm.__wrapped__.__wrapped__
    result = await direct_call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "run tool"}],
        agent_name="test",
        role_description="",
        agent_id=None,
        user_id=None,
        session_id="",
        turn_anchor_id=uuid.uuid4(),
        context_recovery=recover,
    )

    assert result == "recovered"
    assert len(client.stream_calls) == 3
    before_recovery = client.stream_calls[1]["messages"]
    after_recovery = client.stream_calls[2]["messages"]
    before_prompts = [message.content for message in before_recovery if message.role == "user"]
    after_prompts = [message.content for message in after_recovery if message.role == "user"]
    retry_prompt = next(content for content in before_prompts if "Retry once" in content)
    assert after_prompts.count(retry_prompt) == 1
