"""Media transport facade: shared LLM clients and small generation adapters."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx

from app.services import media_ai_bailian as bailian, media_ai_openai as standard
from app.services.llm.client import LLMError, LLMMessage, LLMResponse, create_llm_client
from app.services.llm.client_registry import resolve_api_protocol
from app.services.media_ai_io import MediaAIError, MediaInput, media_content


def connection(config: dict) -> dict:
    if not config.get("model_id"):
        return bailian.connection(config)
    resolved = dict(config)
    parsed = urlsplit(str(resolved.get("base_url") or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise MediaAIError("invalidEndpoint")
    if not resolved.get("api_key"):
        raise MediaAIError("notConfigured")
    resolved["base_url"] = resolved["base_url"].rstrip("/")
    return resolved


def _native(config: dict) -> bool:
    return not config.get("model_id") or config.get("provider") in {"qwen", "bailian", "dashscope"}


def _bailian_config(config: dict) -> dict:
    if not config.get("model_id"):
        return config
    result = dict(config)
    for suffix in ("/compatible-mode/v1", "/api/v1"):
        if result["base_url"].endswith(suffix):
            result["base_url"] = result["base_url"][:-len(suffix)]
    return result


def generation_payload(config: dict, args: dict, media: list[MediaInput]) -> tuple[str, dict]:
    _validate_inputs(config, media)
    adapter = bailian if _native(config) else standard
    return adapter.generation_payload(config, args, media)


async def request(config: dict, path: str, payload: dict | None = None, *, asynchronous=False) -> dict:
    if _native(config):
        return await bailian.request(_bailian_config(config), path, payload, asynchronous=asynchronous)
    return await standard.request(config, path, payload)


async def poll_generation(config: dict, task_id: str) -> dict:
    path = "/api/v1/tasks/" if _native(config) else "/videos/"
    return await request(config, path + quote(str(task_id), safe=""))


def result_url(result: dict, kind: str) -> str:
    url = (result.get("output") or {}).get("video_url")
    if kind == "video" and isinstance(url, str) and url.startswith("provider:/videos/"):
        return url
    return bailian.result_url(result, kind)


async def download_result(config: dict, reference: str) -> bytes:
    return await standard.download_result(config, reference)


def _understanding_transport(config: dict, messages: list[LLMMessage]) -> tuple[str, str]:
    protocol = resolve_api_protocol(config["provider"], config.get("api_protocol"))
    base_url = config["base_url"]
    host = (urlsplit(base_url).hostname or "").lower()
    bailian_endpoint = host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
    } or host.endswith(".maas.aliyuncs.com")
    # Bailian documents Responses audio/video input as unsupported. Route known
    # capability differences before submission; never retry a failed model job.
    # https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses
    audio_video = any(
        part.get("type") in {"video_url", "input_video", "input_audio", "audio_url"}
        for message in messages if isinstance(message.content, list)
        for part in message.content if isinstance(part, dict)
    )
    if protocol == "openai_responses" and bailian_endpoint and audio_video:
        return "openai_compatible", base_url.rstrip("/").removesuffix("/responses")
    return protocol, base_url


async def understand_response(config: dict, prompt: str, media: list[MediaInput], *, history: list[dict] | None = None) -> LLMResponse:
    if not media and not history:
        raise MediaAIError("inputCombination")
    _validate_inputs(config, media)
    content = media_content(prompt, media)
    messages = [LLMMessage(**message) for message in (history or [])]
    messages.append(LLMMessage(role="user", content=content))
    return await _understanding_attempt(config, media, messages)


async def _understanding_attempt(config, media, messages, *, attempts=None):
    from app.services.llm.provider_retry import _is_provider_recovery_error, _is_provider_throttle_error

    attempts = list(attempts or [])
    protocol, base_url = _understanding_transport(config, messages)
    client = create_llm_client(
        provider=config["provider"], api_key=config["api_key"], model=config["model"],
        base_url=base_url, timeout=config.get("request_timeout") or 180, api_protocol=protocol,
    )
    emitted = False

    async def observed(*values):
        nonlocal emitted
        emitted = emitted or any(values)

    fallback = None
    try:
        extra = {"modalities": ["text"]} if config.get("provider") in {"qwen", "bailian", "dashscope"} else {}
        response = await client.stream(
            messages=messages, max_tokens=config.get("max_output_tokens"),
            temperature=config.get("temperature"), reasoning_effort=config.get("reasoning_effort"),
            on_chunk=observed, on_thinking=observed, on_tool_delta=observed, **extra,
        )
        if response.finish_reason in {"length", "max_tokens"}:
            raise MediaAIError("analysisTooLong")
        if response.finish_reason not in {None, "stop", "end_turn"} or not response.content:
            raise MediaAIError("providerFailed")
        response.model = response.model or config["model"]
        if attempts:
            response.usage = {**(response.usage or {}), "provider_attempts": [
                *attempts, {"model_id": config.get("model_id"), "status": "completed"},
            ]}
        return response
    except LLMError as exc:
        attempts.append({"model_id": config.get("model_id"), "status": "failed",
                         "http_status": exc.status_code, "provider_code": exc.error_code})
        if (not emitted and not attempts[:-1] and exc.status_code is not None
                and (_is_provider_throttle_error(exc) or _is_provider_recovery_error(exc))):
            fallback = config.get("fallback_connection")
        if fallback is None:
            error = MediaAIError("providerFailed", provider_code=exc.error_code or "")
            error.attempts = attempts
            raise error from exc
    except (httpx.HTTPError, MediaAIError) as exc:
        error = exc if isinstance(exc, MediaAIError) else MediaAIError("connectionFailed")
        error.attempts = [*attempts, {"model_id": config.get("model_id"), "status": "failed",
                                    "code": error.code}]
        raise error from exc
    finally:
        await client.close()
    _validate_inputs(fallback, media)
    return await _understanding_attempt(fallback, media, messages, attempts=attempts)


def _validate_inputs(config: dict, media: list[MediaInput]) -> None:
    modalities = config.get("input_modalities")
    if modalities is not None and any(item.kind not in modalities for item in media):
        raise MediaAIError("inputCombination")


async def understand(config: dict, prompt: str, media: list[MediaInput], *, history: list[dict] | None = None) -> tuple[str, dict]:
    if not config.get("model_id"):
        return await bailian.understand(config, prompt, media, history=history)
    response = await understand_response(config, prompt, media, history=history)
    return response.content, response.usage or {}
