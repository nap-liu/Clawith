"""Per-call inference controls over the existing shared provider adapters."""

from app.services.media_ai_io import MediaAIError


def understanding_parameters(config: dict, parameters: dict | None = None) -> dict:
    options = dict(parameters or {})
    # Connection, content and execution lifecycle have authoritative tool fields.
    # Everything else remains a provider parameter, without a model allowlist.
    reserved = {"model", "model_id", "messages", "input", "prompt", "files",
                "stream", "stream_options", "tools", "tool_choice", "api_key",
                "base_url", "extra_headers", "on_chunk", "on_thinking", "on_tool_delta"}
    if reserved.intersection(options):
        raise MediaAIError("invalidArguments")
    max_tokens = options.pop("max_output_tokens", options.get("max_tokens", config.get("max_output_tokens")))
    options.pop("max_tokens", None)
    native_reasoning = any(key in options for key in ("reasoning", "enable_thinking", "thinking_budget"))
    defaults = {
        "max_tokens": max_tokens,
        "temperature": config.get("temperature"),
        "reasoning_effort": None if native_reasoning else config.get("reasoning_effort"),
    }
    return {**defaults, **options}
