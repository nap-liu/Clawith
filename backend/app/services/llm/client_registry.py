from app.services.llm.client_shared import *  # noqa: F401,F403
from app.services.llm.client_anthropic import AnthropicClient
from app.services.llm.client_gemini import GeminiClient
from app.services.llm.client_openai_compatible import OpenAICompatibleClient
from app.services.llm.client_openai_responses import OpenAIResponsesClient


@dataclass(frozen=True)
class ProviderSpec:
    """Provider registry entry."""

    provider: str
    display_name: str
    protocol: Literal["openai_compatible", "anthropic", "openai_responses", "gemini"]
    default_base_url: str | None
    supports_tool_choice: bool = True
    default_max_tokens: int = 4096
    model_max_tokens: dict[str, int] = field(default_factory=dict)


# Provider aliases accepted for compatibility
PROVIDER_ALIASES: dict[str, str] = {
    "openai_response": "openai-response",
    "openairesponses": "openai-response",
}


# Canonical provider registry (single source of truth)
PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        provider="anthropic",
        display_name="Anthropic",
        protocol="anthropic",
        default_base_url="https://api.anthropic.com",
        supports_tool_choice=False,
        default_max_tokens=8192,
    ),
    "openai": ProviderSpec(
        provider="openai",
        display_name="OpenAI",
        protocol="openai_compatible",
        default_base_url="https://api.openai.com/v1",
        default_max_tokens=16384,
    ),
    "openai-response": ProviderSpec(
        provider="openai-response",
        display_name="OpenAI Responses",
        protocol="openai_responses",
        default_base_url="https://api.openai.com/v1",
        default_max_tokens=16384,
    ),
    "azure": ProviderSpec(
        provider="azure",
        display_name="Azure OpenAI",
        protocol="openai_compatible",
        default_base_url=None,
        default_max_tokens=16384,
    ),
    "deepseek": ProviderSpec(
        provider="deepseek",
        display_name="DeepSeek",
        protocol="openai_compatible",
        default_base_url="https://api.deepseek.com/v1",
        default_max_tokens=8192,
    ),
    "qwen": ProviderSpec(
        provider="qwen",
        display_name="Qwen (DashScope)",
        protocol="openai_compatible",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_max_tokens=8192,
        model_max_tokens={
            "qwen-plus": 16384,
            "qwen-long": 16384,
            "qwen-turbo": 8192,
            "qwen-max": 8192,
            # Qwen3.x "plus" lines support 32k output; default 8192 truncates
            # long write_file tool_calls mid-JSON and bricks the round.
            "qwen3.5-plus": 32768,
            "qwen3.6-plus": 32768,
        },
    ),
    "minimax": ProviderSpec(
        provider="minimax",
        display_name="MiniMax",
        protocol="openai_compatible",
        default_base_url="https://api.minimaxi.com/v1",
        default_max_tokens=16384,
    ),
    "openrouter": ProviderSpec(
        provider="openrouter",
        display_name="OpenRouter",
        protocol="openai_compatible",
        default_base_url="https://openrouter.ai/api/v1",
        default_max_tokens=4096,
    ),
    "zhipu": ProviderSpec(
        provider="zhipu",
        display_name="Zhipu",
        protocol="openai_compatible",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_max_tokens=8192,
    ),
    "baidu": ProviderSpec(
        provider="baidu",
        display_name="Baidu (Qianfan)",
        protocol="openai_compatible",
        default_base_url="https://qianfan.baidubce.com/v2",
        supports_tool_choice=False,
        default_max_tokens=4096,
    ),
    "gemini": ProviderSpec(
        provider="gemini",
        display_name="Gemini",
        protocol="gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        default_max_tokens=8192,
    ),
    "kimi": ProviderSpec(
        provider="kimi",
        display_name="Kimi (Moonshot)",
        protocol="openai_compatible",
        default_base_url="https://api.moonshot.cn/v1",
        default_max_tokens=8192,
    ),
    "vllm": ProviderSpec(
        provider="vllm",
        display_name="vLLM",
        protocol="openai_compatible",
        default_base_url="http://localhost:8000/v1",
        default_max_tokens=4096,
    ),
    "ollama": ProviderSpec(
        provider="ollama",
        display_name="Ollama",
        protocol="openai_compatible",
        default_base_url="http://localhost:11434/v1",
        default_max_tokens=4096,
    ),
    "sglang": ProviderSpec(
        provider="sglang",
        display_name="SGLang",
        protocol="openai_compatible",
        default_base_url="http://localhost:30000/v1",
        default_max_tokens=4096,
    ),
    "custom": ProviderSpec(
        provider="custom",
        display_name="Custom",
        protocol="openai_compatible",
        default_base_url=None,
        default_max_tokens=4096,
    ),
}


def normalize_provider(provider: str) -> str:
    """Normalize provider id with aliases and lowercase."""
    p = (provider or "").strip().lower()
    return PROVIDER_ALIASES.get(p, p)


def get_provider_spec(provider: str) -> ProviderSpec | None:
    """Get provider spec from registry."""
    return PROVIDER_REGISTRY.get(normalize_provider(provider))


def get_provider_manifest() -> list[dict[str, Any]]:
    """List supported providers and capabilities for UI/config discovery."""
    out: list[dict[str, Any]] = []
    for spec in PROVIDER_REGISTRY.values():
        out.append({
            "provider": spec.provider,
            "display_name": spec.display_name,
            "protocol": spec.protocol,
            "default_base_url": spec.default_base_url,
            "supports_tool_choice": spec.supports_tool_choice,
            "default_max_tokens": spec.default_max_tokens,
            "model_max_tokens": spec.model_max_tokens,
            "aliases": [k for k, v in PROVIDER_ALIASES.items() if v == spec.provider],
        })
    return out


# Backward-compatible constants derived from registry
PROVIDER_CLIENTS: dict[str, type[LLMClient]] = {
    spec.provider: (
        AnthropicClient
        if spec.protocol == "anthropic"
        else OpenAIResponsesClient
        if spec.protocol == "openai_responses"
        else GeminiClient
        if spec.protocol == "gemini"
        else OpenAICompatibleClient
    )
    for spec in PROVIDER_REGISTRY.values()
}

PROVIDER_URLS: dict[str, str | None] = {
    spec.provider: spec.default_base_url for spec in PROVIDER_REGISTRY.values()
}

TOOL_CHOICE_PROVIDERS = {
    spec.provider for spec in PROVIDER_REGISTRY.values() if spec.supports_tool_choice
}

MAX_TOKENS_BY_PROVIDER: dict[str, int] = {
    spec.provider: spec.default_max_tokens for spec in PROVIDER_REGISTRY.values()
}

MAX_TOKENS_BY_MODEL: dict[str, int] = {
    prefix: limit
    for spec in PROVIDER_REGISTRY.values()
    for prefix, limit in spec.model_max_tokens.items()
}

def get_provider_base_url(provider: str, custom_base_url: str | None = None) -> str | None:
    """Return the API base URL for a provider.

    If a custom base_url is provided, it takes precedence.
    Otherwise falls back to the default URL for the provider.
    """
    if custom_base_url:
        return custom_base_url
    spec = get_provider_spec(provider)
    if spec:
        return spec.default_base_url
    return PROVIDER_URLS.get(normalize_provider(provider))


def get_max_tokens(provider: str, model: str | None = None, max_output_tokens: int | None = None) -> int:
    """Return a safe max_tokens value for the given provider/model pair.

    Priority: max_output_tokens (DB override) > model prefix > provider default > 4096
    """
    spec = get_provider_spec(provider)
    model_limits = spec.model_max_tokens if spec else MAX_TOKENS_BY_MODEL

    # Highest priority: per-model DB override
    if max_output_tokens and max_output_tokens > 0:
        return max_output_tokens

    # Check model-specific limits
    if model:
        for prefix, limit in model_limits.items():
            if model.lower().startswith(prefix):
                return limit

    if spec:
        return spec.default_max_tokens

    # Provider default, falling back to safe 4096
    return MAX_TOKENS_BY_PROVIDER.get(normalize_provider(provider), 4096)


def create_llm_client(
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: float = 120.0,
    *,
    provider_managed_timeout: bool = False,
) -> LLMClient:
    """Create an LLM client for the given provider.

    Args:
        provider: Provider name (openai, anthropic, deepseek, etc.)
        api_key: API key for authentication
        model: Model name
        base_url: Optional custom base URL
        timeout: Connection and auxiliary-call timeout in seconds
        provider_managed_timeout: Deprecated compatibility flag. Provider
            response reads are always unbounded.

    Returns:
        An instance of the appropriate LLMClient subclass

    Raises:
        ValueError: If provider is not supported
    """
    normalized_provider = normalize_provider(provider)
    spec = get_provider_spec(normalized_provider)

    # Get base URL
    final_base_url = get_provider_base_url(normalized_provider, base_url)

    # Create appropriate client
    if spec and spec.protocol == "anthropic":
        return AnthropicClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            provider_managed_timeout=provider_managed_timeout,
        )
    elif spec and spec.protocol == "openai_responses":
        return OpenAIResponsesClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            supports_tool_choice=spec.supports_tool_choice,
            provider_managed_timeout=provider_managed_timeout,
        )
    elif spec and spec.protocol == "gemini":
        return GeminiClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            supports_tool_choice=spec.supports_tool_choice,
            provider_managed_timeout=provider_managed_timeout,
        )
    elif normalized_provider in PROVIDER_CLIENTS:
        supports_tool_choice = normalized_provider in TOOL_CHOICE_PROVIDERS
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            supports_tool_choice=supports_tool_choice,
            supports_cache_control=normalized_provider == "qwen",
            provider_managed_timeout=provider_managed_timeout,
            provider=normalized_provider,
        )
    else:
        # Default to OpenAI-compatible for unknown providers
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=final_base_url or PROVIDER_URLS["openai"],
            model=model,
            timeout=timeout,
            supports_tool_choice=True,
            supports_cache_control=False,
            provider_managed_timeout=provider_managed_timeout,
            provider=normalized_provider,
        )


# ============================================================================
# High-level Convenience Functions
# ============================================================================

async def chat_complete(
    provider: str,
    api_key: str,
    model: str,
    messages: list[dict],
    base_url: str | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 120.0,
) -> dict:
    """High-level function for non-streaming chat completion.

    Returns response in OpenAI-compatible format for backward compatibility.
    """
    client = create_llm_client(provider, api_key, model, base_url, timeout)

    try:
        llm_messages = [LLMMessage(**m) for m in messages]
        response = await client.complete(
            messages=llm_messages,
            tools=tools,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens or get_max_tokens(provider, model),
        )

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": response.tool_calls or None,
                },
                "finish_reason": response.finish_reason or "stop",
            }],
            "model": response.model or model,
            "usage": response.usage or {},
        }
    finally:
        await client.close()


async def chat_stream(
    provider: str,
    api_key: str,
    model: str,
    messages: list[dict],
    base_url: str | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 120.0,
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
) -> dict:
    """High-level function for streaming chat completion.

    Returns aggregated response in OpenAI-compatible format.
    """
    client = create_llm_client(provider, api_key, model, base_url, timeout)

    try:
        llm_messages = [LLMMessage(**m) for m in messages]
        response = await client.stream(
            messages=llm_messages,
            tools=tools,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens or get_max_tokens(provider, model),
            on_chunk=on_chunk,
            on_thinking=on_thinking,
        )

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": response.tool_calls or None,
                },
                "finish_reason": response.finish_reason or "stop",
            }],
            "model": response.model or model,
            "usage": response.usage or {},
        }
    finally:
        await client.close()

__all__ = (
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
)
