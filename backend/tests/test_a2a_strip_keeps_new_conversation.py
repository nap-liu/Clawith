"""Guardrail: when async A2A is disabled, _strip_a2a_msg_type must drop msg_type
but KEEP the new_conversation self-reset param AND keep a RESET hint in the
description — otherwise agents (on a2a_async_enabled=false tenants) can never
discover the reset escape hatch. This is the regression that shipped once."""

from app.services.agent_tools import _strip_a2a_msg_type


def _tool():
    return {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": "Send a message to a digital employee colleague. DECISION GUIDE ...",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "message": {"type": "string"},
                    "msg_type": {"type": "string", "enum": ["notify", "consult", "task_delegate"]},
                    "new_conversation": {"type": "boolean", "description": "reset hint"},
                },
                "required": ["agent_name", "message", "msg_type"],
            },
        },
    }


def test_strip_drops_msg_type_keeps_new_conversation_and_reset_hint():
    out = _strip_a2a_msg_type([_tool()])
    fn = out[0]["function"]
    props = fn["parameters"]["properties"]
    # msg_type removed (param + required)
    assert "msg_type" not in props
    assert "msg_type" not in fn["parameters"]["required"]
    # the self-reset escape hatch survives
    assert "new_conversation" in props
    # the simplified description still surfaces the reset hint
    assert "RESET" in fn["description"]
    assert "new_conversation" in fn["description"]


def test_strip_does_not_mutate_input():
    original = _tool()
    _strip_a2a_msg_type([original])
    # deep-copied internally; original keeps msg_type
    assert "msg_type" in original["function"]["parameters"]["properties"]
