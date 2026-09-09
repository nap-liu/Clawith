"""Small DashScope adapters behind the two normalized media tools."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from app.services.media_ai_contract import MEDIA_AI_DEFAULTS
from app.services.media_ai_io import MediaAIError, MediaInput

MULTIMODAL_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
VIDEO_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
IMAGE_SIZES = {"1:1": "1024*1024", "16:9": "1536*864", "9:16": "864*1536",
               "4:3": "1152*864", "3:4": "864*1152"}


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
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    if asynchronous:
        headers["X-DashScope-Async"] = "enable"
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        response = await client.request(
            "POST" if payload is not None else "GET", config["base_url"] + path,
            json=payload, headers=headers,
        )
        return _check_response(response)


async def understand(config: dict, prompt: str, media: list[MediaInput], *, history: list[dict] | None = None) -> tuple[str, dict]:
    if not media and not history:
        raise MediaAIError("inputCombination")
    content = [{"type": "text", "text": prompt}]
    for item in media:
        if item.kind == "audio":
            content.append({"type": "input_audio", "input_audio": {"data": item.data_url}})
        else:
            kind = "image_url" if item.kind == "image" else "video_url"
            content.append({"type": kind, kind: {"url": item.data_url}})
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
            json=payload, headers={"Authorization": f"Bearer {config['api_key']}"},
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
    if kind == "image" and any(item.kind != "image" for item in media):
        raise MediaAIError("referenceImagesOnly")
    if kind == "audio":
        if media or any(key in args for key in ("ratio", "duration", "size", "resolution")):
            raise MediaAIError("unsupportedOptions")
        return MULTIMODAL_PATH, {
            "model": config["audio_model"],
            "input": {"text": args["prompt"], "voice": args.get("voice", "Cherry"), "language_type": "Auto"},
        }
    if "voice" in args or (kind == "image" and "duration" in args):
        raise MediaAIError("unsupportedOptions")
    if kind == "image":
        size = args.get("size") or IMAGE_SIZES.get(args.get("ratio", "1:1"))
        if not size:
            raise MediaAIError("unsupportedOptions")
        return MULTIMODAL_PATH, {
            "model": config["image_model"],
            "input": {"messages": [{"role": "user", "content": [
                *[{"image": item.data_url} for item in media], {"text": args["prompt"]},
            ]}]},
            "parameters": {"n": 1, "size": size},
        }
    references = []
    for item in media:
        role = item.role or ("first_frame" if len(media) == 1 and item.kind == "image" else f"reference_{item.kind}")
        expected = "image" if role in {"first_frame", "last_frame"} else role.removeprefix("reference_")
        if expected != item.kind:
            raise MediaAIError("inputCombination")
        references.append({"type": role, "url": item.data_url})
    return VIDEO_PATH, {
        "model": config["video_model"],
        "input": {"prompt": args["prompt"], **({"media": references} if media else {})},
        "parameters": {"resolution": args.get("resolution", "720P"), "ratio": args.get("ratio", "adaptive" if media else "16:9"),
                       "duration": args.get("duration", 5)},
    }


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
