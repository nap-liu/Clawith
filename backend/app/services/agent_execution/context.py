"""Explicit context crossing the execution boundary; no ORM sessions or locks."""

from contextlib import contextmanager
from importlib import import_module

_CONTEXT_FIELDS = (
    ("app.services.conversation_execution_lock", "_inherited_resources"),
    ("app.core.logging_config", "trace_id_var"),
    ("app.services.agent_runtime_workspace", "_active_workspace"),
    ("app.services.turn_tool_settings", "_current"),
    ("app.services.subagent_runtime_shared", "_current_subagent_lease_owner"),
    ("app.services.agent_tools", "channel_file_sender"),
    ("app.services.agent_tools", "channel_file_part_recorder"),
    ("app.services.agent_tools", "channel_audio_sender"),
    ("app.services.agent_tools", "channel_video_sender"),
    ("app.services.agent_tools", "channel_web_agent_id"),
    ("app.services.agent_tools", "channel_feishu_sender_open_id"),
)


def export_context():
    from app.services.conversation_execution_lock import supervised_conversation_resources

    values = {
        (module, name): getattr(import_module(module), name).get()
        for module, name in _CONTEXT_FIELDS
    }
    values[("app.services.conversation_execution_lock", "_inherited_resources")] = (
        supervised_conversation_resources()
    )
    return values


@contextmanager
def bind_context(values):
    tokens = []
    try:
        for field, value in values.items():
            if field not in _CONTEXT_FIELDS:
                raise ValueError("Unsupported execution context")
            module, name = field
            variable = getattr(import_module(module), name)
            tokens.append((variable, variable.set(value)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
