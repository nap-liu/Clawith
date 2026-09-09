"""Compatibility import for the canonical durable trigger adapter."""

from app.services.trigger_daemon_invocation import (
    _invoke_agent_for_triggers as invoke_agent_for_triggers,
)
from app.services.trigger_daemon_delivery import (
    _resolve_trigger_delivery_target as resolve_trigger_delivery_target,
)

__all__ = ["invoke_agent_for_triggers", "resolve_trigger_delivery_target"]
