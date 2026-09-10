"""TokenHub generation transports over the existing durable media job contract.

Tencent API contracts: product/1823/135745 (Hy image), 135742 (Kling),
and 135796 (MiniMax speech) at cloud.tencent.com/document.
"""

from __future__ import annotations

from urllib.parse import quote, unquote

import httpx

from app.services.media_ai_bailian import _check_response
from app.services.media_ai_headers import provider_headers
from app.services.media_ai_io import MAX_RESULT_BYTES, MediaAIError, MediaInput
from app.services.model_platform import model_service_platform

IMAGE_PATH = "/wand/hunyuan-image/v3-generation"
SPEECH_PATH = "/wand/minimax-tts/sync_tts"
KLING_PATH = "/wand/kling/"
DEFAULT_VOICE = "minimax_55e81114-ce7e-4ca1"


def supports(config: dict) -> bool:
    platform = model_service_platform(config.get("provider", ""), config.get("base_url")) == "tokenhub"
    return platform and config.get("model") in {
        "hy-image-v3", "kling-video-v3", "minimax-speech-2.8-turbo",
    }


def _object(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MediaAIError("invalidArguments")
    return dict(value)


def _list(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MediaAIError("invalidArguments")
    return list(value)


def generation_payload(config: dict, args: dict, media: list[MediaInput]) -> tuple[str, dict]:
    options = _object(args.get("parameters"))
    model = config["model"]
    if args["output_type"] == "image" and model == "hy-image-v3":
        if any(item.kind != "image" for item in media):
            raise MediaAIError("referenceImagesOnly")
        if any(key in args for key in ("voice", "duration", "resolution")):
            raise MediaAIError("unsupportedOptions")
        payload = {**options, "model": model, "prompt": args["prompt"]}
        images = [*_list(options.get("images")), *[item.data_url for item in media]]
        if images:
            payload["images"] = images
        if "size" in args:
            payload["size"] = args["size"].replace("*", "x")
        elif "ratio" in args and "size" not in payload:
            # Hy documents ratio instructions in the prompt, selecting its own
            # supported dimensions. Do not invent a product ratio allowlist.
            payload["prompt"] += f"\nAspect ratio: {args['ratio']}"
        return IMAGE_PATH, payload
    if args["output_type"] == "audio" and model == "minimax-speech-2.8-turbo":
        if media or any(key in args for key in ("ratio", "duration", "size", "resolution")):
            raise MediaAIError("unsupportedOptions")
        audio_settings = _object(options.get("audio_setting"))
        if audio_settings.get("format") in {"pcm", "pcmu_raw"}:
            # Raw samples cannot be identified by the shared file/player
            # contract. Reject before paying for an unusable result.
            raise MediaAIError("unsupportedOptions")
        if options.get("subtitle_enable") is True:
            # The current result contains one audio artifact, not a subtitle
            # sidecar. Do not accept and then discard a requested output.
            raise MediaAIError("unsupportedOptions")
        voice = _object(options.get("voice_setting"))
        if "voice" in args:
            voice["voice_id"] = args["voice"]
        elif not options.get("timbre_weights"):
            voice.setdefault("voice_id", DEFAULT_VOICE)
        payload = {**options, "model": model, "text": args["prompt"], "voice_setting": voice}
        payload.setdefault("output_format", "hex")
        # This endpoint is explicitly synchronous, even though the platform job
        # itself is asynchronous. Never request a stream that cannot complete.
        if payload.get("stream"):
            raise MediaAIError("unsupportedOptions")
        return SPEECH_PATH, payload
    if args["output_type"] == "video" and model == "kling-video-v3":
        return _video_payload(model, args, media, options)
    raise MediaAIError("unsupportedOptions")


def _video_payload(model: str, args: dict, media: list[MediaInput], options: dict) -> tuple[str, dict]:
    if any(key in args for key in ("voice", "size")):
        raise MediaAIError("unsupportedOptions")
    settings = _object(options.get("settings"))
    for source, target in (("duration", "duration"), ("resolution", "resolution"), ("ratio", "aspect_ratio")):
        if source in args:
            value = args[source]
            settings[target] = value.lower() if source == "resolution" else value
    contents = _list(options.get("contents"))
    for item in media:
        role = item.role or "first_frame"
        if role == "reference_image" and len(media) == 1:
            role = "first_frame"
        if item.kind != "image" or role not in {"first_frame", "last_frame"}:
            raise MediaAIError("inputCombination")
        contents.append({"type": role, "url": item.data_url})
    payload = {**options, "model": model, "settings": settings}
    if contents:
        # Native element definitions and additional prompt segments retain all
        # fields. The normalized prompt is included, never discarded.
        payload.pop("prompt", None)
        payload["contents"] = [{"type": "prompt", "text": args["prompt"]}, *contents]
        if "aspect_ratio" in settings:
            raise MediaAIError("unsupportedOptions")
        return KLING_PATH + "image-to-video", payload
    payload["prompt"] = args["prompt"]
    return KLING_PATH + "text-to-video", payload


def _metadata(data: dict) -> dict:
    return {"usage": data.get("tokenhub_usage") or data.get("usage") or {},
            "request_id": data.get("request_id") or data.get("trace_id") or data.get("id") or ""}


def _video_result(data: dict, expected_id: str | None) -> dict:
    tasks = data.get("data")
    if isinstance(tasks, list):
        tasks = next((item for item in tasks if isinstance(item, dict) and item.get("id") == expected_id), None)
    if not isinstance(tasks, dict) or not tasks.get("id"):
        raise MediaAIError("providerFailed")
    if expected_id is not None and tasks["id"] != expected_id:
        raise MediaAIError("providerFailed")
    status = {"submitted": "PENDING", "processing": "RUNNING", "succeeded": "SUCCEEDED",
              "failed": "FAILED", "canceled": "CANCELED", "cancelled": "CANCELED"}.get(tasks.get("status"))
    if not status:
        raise MediaAIError("providerFailed")
    output = {"task_id": tasks["id"], "task_status": status}
    if status == "SUCCEEDED":
        outputs = tasks.get("outputs") or []
        output["video_url"] = next((item.get("url") for item in outputs
                                    if isinstance(item, dict) and item.get("type") == "video"), None)
        if not output["video_url"]:
            raise MediaAIError("providerFailed")
    if status in {"FAILED", "CANCELED"}:
        output["code"] = str(tasks.get("code") or status)
    return {"output": output, **_metadata(data)}


def _speech_result(data: dict) -> dict:
    base = data.get("base_resp") or {}
    if not isinstance(base, dict) or base.get("status_code", 0) not in {0, "0"}:
        code = base.get("status_code", "") if isinstance(base, dict) else ""
        raise MediaAIError("providerFailed", provider_code=str(code))
    audio = data.get("data") or {}
    if not isinstance(audio, dict) or audio.get("status") != 2 or not isinstance(audio.get("audio"), str):
        raise MediaAIError("providerFailed")
    value = audio["audio"]
    if value.startswith(("http://", "https://")):
        return {"output": {"audio": {"url": value}}, **_metadata(data)}
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise MediaAIError("providerFailed") from exc
    if not raw:
        raise MediaAIError("invalidMedia")
    if MAX_RESULT_BYTES is not None and len(raw) > MAX_RESULT_BYTES:
        raise MediaAIError("fileTooLarge")
    return {"output": {}, "_bytes": raw, **_metadata(data)}


async def request(config: dict, path: str, payload: dict | None = None) -> dict:
    root = config["base_url"].rstrip("/").removesuffix("/responses")
    async with httpx.AsyncClient(timeout=config.get("request_timeout") or 180, follow_redirects=False) as client:
        response = await client.request(
            "POST" if payload is not None else "GET", root + path,
            json=payload, headers=provider_headers(config),
        )
        data = _check_response(response)
    if path == SPEECH_PATH:
        return _speech_result(data)
    if path == IMAGE_PATH:
        images = data.get("data") or []
        image = images[0] if isinstance(images, list) and images else {}
        if not isinstance(image, dict) or not image.get("url"):
            raise MediaAIError("providerFailed")
        return {"output": {"choices": [{"message": {"content": [{"image": image["url"]}]}}]},
                **_metadata(data)}
    if path.startswith(KLING_PATH):
        expected = unquote(path.removeprefix(KLING_PATH + "tasks/")) if payload is None else None
        return _video_result(data, expected)
    raise MediaAIError("providerFailed")


async def poll_generation(config: dict, task_id: str) -> dict:
    return await request(config, KLING_PATH + "tasks/" + quote(str(task_id), safe=""))
