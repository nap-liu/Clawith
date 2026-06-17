"""`_call_agent_llm` recovery-hint decoupling.

The IM-only "/new" recovery hint is appended inside `_call_agent_llm`. The MCP
channel has no `/new`, so the hint must be opt-out via a parameter — default
behaviour for IM channels is unchanged. We unit-test the pure helper so the test
needs no LLM/DB.
"""

from __future__ import annotations

from app.services import channel_llm
from app.services.channel_llm import _IM_LLM_RECOVERY_HINT, _apply_recovery_hint


def test_error_reply_with_hint_gets_appended():
    reply = "[Error] something blew up"
    assert _apply_recovery_hint(reply, _IM_LLM_RECOVERY_HINT) == reply + _IM_LLM_RECOVERY_HINT


def test_error_reply_with_none_hint_unchanged():
    reply = "[Error] something blew up"
    assert _apply_recovery_hint(reply, None) == reply


def test_normal_reply_never_gets_hint():
    reply = "hello, here is your answer"
    assert _apply_recovery_hint(reply, _IM_LLM_RECOVERY_HINT) == reply


def test_call_agent_llm_accepts_recovery_hint_param():
    """Signature must expose recovery_hint defaulting to the IM hint (back-compat)."""
    import inspect

    sig = inspect.signature(channel_llm._call_agent_llm)
    assert "recovery_hint" in sig.parameters
    assert sig.parameters["recovery_hint"].default == _IM_LLM_RECOVERY_HINT
