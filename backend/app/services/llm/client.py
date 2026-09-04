"""Unified LLM client for multiple providers."""

import sys
from types import ModuleType

from app.services.llm import client_shared as _client_shared
from app.services.llm.client_shared import *  # noqa: F401,F403
from app.services.llm import (
    client_anthropic as _client_anthropic,
    client_gemini as _client_gemini,
    client_openai_compatible as _client_openai_compatible,
    client_openai_responses as _client_openai_responses,
    client_registry as _client_registry,
)

_MODULE_EXPORTS: tuple[tuple[ModuleType, tuple[str, ...]], ...] = (
    (
        _client_openai_compatible,
        (
            'OpenAICompatibleClient',
        ),
    ),
    (
        _client_openai_responses,
        (
            'OpenAIResponsesClient',
        ),
    ),
    (
        _client_gemini,
        (
            'GeminiClient',
        ),
    ),
    (
        _client_anthropic,
        (
            'AnthropicClient',
        ),
    ),
    (
        _client_registry,
        (
            'ProviderSpec',
            'PROVIDER_ALIASES',
            'PROVIDER_REGISTRY',
            'normalize_provider',
            'get_provider_spec',
            'get_provider_manifest',
            'PROVIDER_CLIENTS',
            'PROVIDER_URLS',
            'TOOL_CHOICE_PROVIDERS',
            'MAX_TOKENS_BY_PROVIDER',
            'MAX_TOKENS_BY_MODEL',
            'get_provider_base_url',
            'get_max_tokens',
            'create_llm_client',
            'chat_complete',
            'chat_stream',
        ),
    ),
)

_SHARED_SYMBOLS = ('LLMClientCloseGuard', 'LLMMessage', 'LLMResponse', 'LLMStreamChunk', 'ChunkCallback', 'ToolCallback', 'ThinkingCallback', 'LLMClient', '_httpx_timeout', '_is_markable_payload_msg', 'select_cache_breakpoints', '_observable_messages_payload', 'LLMError', 'ModelResponseIdleTimeout')

for module, names in _MODULE_EXPORTS:
    for name in names:
        globals()[name] = module.__dict__[name]

def _prepare_client_module(module: ModuleType, symbol_names: tuple[str, ...] | list[str]) -> None:
    for symbol_name in symbol_names:
        value = module.__dict__.get(symbol_name)
        if value is None or not hasattr(value, "__module__"):
            continue
        try:
            value.__module__ = __name__
        except (AttributeError, TypeError):
            continue

for module, names in _MODULE_EXPORTS:
    _prepare_client_module(module, names)
_prepare_client_module(_client_shared, _SHARED_SYMBOLS)

_SYNC_MODULES = (
    _client_shared,
    _client_openai_compatible,
    _client_openai_responses,
    _client_gemini,
    _client_anthropic,
    _client_registry,
)

def _sync_root_exports() -> None:
    module = sys.modules[__name__]
    for name, value in tuple(module.__dict__.items()):
        if name.startswith("__"):
            continue
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value

class _ClientModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value

sys.modules[__name__].__class__ = _ClientModule
_sync_root_exports()
