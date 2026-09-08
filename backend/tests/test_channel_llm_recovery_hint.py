"""`_call_agent_llm` recovery-hint decoupling.

The IM-only "/new" recovery hint is appended inside `_call_agent_llm`. The MCP
channel has no `/new`, so the hint must be opt-out via a parameter — default
behaviour for IM channels is unchanged. We unit-test the pure helper so the test
needs no LLM/DB.
"""

from __future__ import annotations

from app.services.channel_llm import _apply_recovery_hint


def test_error_reply_with_none_hint_unchanged():
    reply = "[Error] something blew up"
    assert _apply_recovery_hint(reply, None) == reply
