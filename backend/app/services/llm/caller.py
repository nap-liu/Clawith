"""Unified LLM calling service with failover support for all execution paths."""

import sys
from types import ModuleType

from app.services.llm import caller_agent as _caller_agent
from app.services.llm import caller_context as _caller_context
from app.services.llm import caller_failover as _caller_failover
from app.services.llm import caller_shared as _caller_shared
from app.services.llm import caller_streaming as _caller_streaming
from app.services.llm import caller_streaming_rounds as _caller_streaming_rounds
from app.services.llm import caller_streaming_support as _caller_streaming_support
from app.services.llm import caller_tooling as _caller_tooling

_EXPORT_MODULES = (
    _caller_shared,
    _caller_tooling,
    _caller_context,
    _caller_streaming,
    _caller_failover,
    _caller_agent,
)

_SYNC_MODULES = (
    _caller_shared,
    _caller_tooling,
    _caller_context,
    _caller_streaming_support,
    _caller_streaming_rounds,
    _caller_streaming,
    _caller_failover,
    _caller_agent,
)

_MOVED_SYMBOLS = (
    "get_agent_tools_for_llm",
    "execute_tool",
    "_cache_hit_ratio",
    "_join_visible_response_segments",
    "_invoke_before_round",
    "_response_was_truncated_by_length",
    "ProviderThrottleExhausted",
    "_is_provider_throttle_error",
    "_sleep_before_throttle_retry",
    "_close_cancelled_provider_client",
    "_provider_slot",
    "_stream_with_throttle_retry",
    "_complete_with_throttle_retry",
    "DispatchBudget",
    "measure_dispatch",
    "_guard_provider_dispatch",
    "_tool_call_signature",
    "_update_repeat_streaks",
    "FailoverGuard",
    "is_error_result",
    "is_retryable_error",
    "_same_model_record",
    "_get_model_timeout",
    "_usage_from_response",
    "_authoritative_usage_details",
    "_is_provider_context_overflow",
    "_coerce_uuid",
    "_observable_tool_args",
    "_observable_tool_result",
    "_send_media_result_is_durable_in_current_session",
    "_durable_tool_result_row_id",
    "_RoundDoneToolCall",
    "_persist_tool_call_events_strict",
    "_reconcile_round_tool_outputs",
    "_emit_round_done_events",
    "_get_agent_config",
    "_get_user_name",
    "_convert_messages_for_vision",
    "_attach_turn_context",
    "_build_turn_context",
    "_check_tool_requires_args",
    "_sanitize_tool_calls_for_context",
    "_allowed_tool_names",
    "_tool_not_enabled_message",
    "_canonicalize_tc_arguments",
    "_process_tool_call",
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
)


def _load_module_exports() -> None:
    module = sys.modules[__name__]
    for source in _EXPORT_MODULES:
        for name, value in source.__dict__.items():
            if name.startswith("__"):
                continue
            module.__dict__[name] = value


def _prepare_caller_module(symbol_names: tuple[str, ...]) -> None:
    module = sys.modules[__name__]
    for symbol_name in symbol_names:
        value = module.__dict__.get(symbol_name)
        if value is None or not hasattr(value, "__module__"):
            continue
        try:
            value.__module__ = __name__
        except (AttributeError, TypeError):
            continue


def _sync_root_exports() -> None:
    module = sys.modules[__name__]
    for name, value in tuple(module.__dict__.items()):
        if name.startswith("__"):
            continue
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value


class _CallerModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value


_load_module_exports()
_prepare_caller_module(_MOVED_SYMBOLS)
sys.modules[__name__].__class__ = _CallerModule
_sync_root_exports()

__all__ = [
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "FailoverGuard",
    "is_retryable_error",
]
