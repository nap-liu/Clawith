"""Small DashScope adapters behind the two normalized media tools."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from app.services.media_ai_contract import MEDIA_AI_DEFAULTS
from app.services.media_ai_bailian_generation import IMAGE_PATH, MULTIMODAL_PATH, VIDEO_PATH, image_payload
from app.services.media_ai_bailian_video import video_payload
from app.services.media_ai_headers import provider_headers
from app.services.media_ai_io import MediaAIError, MediaInput, media_content

def connection(config: dict) -> dict:
    resolved = {**MEDIA_AI_DEFAULTS, **{k: v for k, v in config.items() if v is not None and v != ""}}
    root = str(resolved["base_url"]).rstrip("/")
    for suffix in ("/compatible-mode/v1", "/api/v1"):
        if root.endswith(suffix):
            root = root[:-len(suffix)]
    parts = urlsplit(root)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment or parts.path:
        raise MediaAIError("invalidEndpoint")
    if not resolved.get("api_key"):
        raise MediaAIError("notConfigured")
    try:
        resolved["context_window"] = int(resolved["context_window"])
        resolved["context_usage_ratio"] = float(resolved["context_usage_ratio"])
        resolved["max_output_tokens"] = int(resolved["max_output_tokens"])
        if not (0.1 <= resolved["context_usage_ratio"] <= 1
                and 0 < resolved["max_output_tokens"] < resolved["context_window"]):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise MediaAIError("invalidArguments") from exc
    resolved["base_url"] = root
    return resolved


def _check_response(response: httpx.Response) -> dict:
    retryable = response.status_code in {408, 429} or response.status_code >= 500
    try:
        value = response.json()
    except ValueError as exc:
        raise MediaAIError("providerFailed", provider_code=str(response.status_code),
                           retryable=retryable) from exc
    if not isinstance(value, dict):
        raise MediaAIError("providerFailed", provider_code=str(response.status_code),
                           retryable=retryable)
    error = value.get("error")
    if not response.is_success or error or value.get("code"):
        code = error.get("code", "") if isinstance(error, dict) else value.get("code", "")
        raise MediaAIError("providerFailed", provider_code=str(code or response.status_code),
                           retryable=retryable)
    return value


async def request(config: dict, path: str, payload: dict | None = None, *, asynchronous=False) -> dict:
    headers = provider_headers(config)
    if asynchronous or (payload is not None and path == IMAGE_PATH):
        headers["x-dashscope-async"] = "enable"
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        response = await client.request(
            "POST" if payload is not None else "GET", config["base_url"] + path,
            json=payload, headers=headers,
        )
        return _check_response(response)


async def understand(config: dict, prompt: str, media: list[MediaInput], *, history: list[dict] | None = None) -> tuple[str, dict]:
    if not media and not history:
        raise MediaAIError("inputCombination")
    content = media_content(prompt, media)
    payload = {
        "model": config["understanding_model"],
        "messages": [*(history or []), {"role": "user", "content": content}],
        "modalities": ["text"], "stream": True,
        "stream_options": {"include_usage": True}, "max_tokens": config.get("max_output_tokens", 4096),
    }
    chunks = []
    usage = {}
    finished = False
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST", config["base_url"] + "/compatible-mode/v1/chat/completions",
            json=payload, headers=provider_headers(config),
        ) as response:
            if not response.is_success:
                await response.aread()
                _check_response(response)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if event.get("error"):
                    raise MediaAIError("providerFailed")
                usage = event.get("usage") or usage
                for choice in event.get("choices", []):
                    if choice.get("finish_reason") == "length":
                        raise MediaAIError("analysisTooLong")
                    if choice.get("finish_reason") not in {None, "", "stop"}:
                        raise MediaAIError("providerFailed")
                    if choice.get("finish_reason") == "stop":
                        finished = True
                    text = (choice.get("delta") or {}).get("content")
                    if isinstance(text, str):
                        chunks.append(text)
    if not finished or not "".join(chunks).strip():
        raise MediaAIError("providerFailed")
    return "".join(chunks), usage


def generation_payload(config: dict, args: dict, media: list[MediaInput]) -> tuple[str, dict]:
    kind = args["output_type"]
    options = dict(args.get("parameters") or {})
    if kind == "image" and any(item.kind != "image" for item in media):
        raise MediaAIError("referenceImagesOnly")
    if kind == "audio":
        if media or any(key in args for key in ("ratio", "duration", "size", "resolution")):
            raise MediaAIError("unsupportedOptions")
        return MULTIMODAL_PATH, {
            "model": config["audio_model"],
            "input": {"language_type": "Auto", **options, "text": args["prompt"],
                      "voice": args.get("voice", options.get("voice", "Cherry"))},
        }
    if "voice" in args or (kind == "image" and "duration" in args):
        raise MediaAIError("unsupportedOptions")
    if kind == "image":
        return image_payload(config["image_model"], args, media)
    return VIDEO_PATH, video_payload(config["video_model"], args, media)

def result_url(result: dict, kind: str) -> str:
    output = result.get("output") or {}
    if kind == "audio":
        url = (output.get("audio") or {}).get("url")
    elif kind == "video":
        url = output.get("video_url")
    else:
        choices = output.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") or []) if choices else []
        url = next((item.get("image") for item in content if item.get("image")), None)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise MediaAIError("providerFailed")
    return url
