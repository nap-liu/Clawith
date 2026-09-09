"""OpenAI media endpoints, shared by providers implementing the same protocol."""

from __future__ import annotations

import base64
import binascii
from urllib.parse import quote

import httpx

from app.services.media_ai_bailian import _check_response
from app.services.media_ai_io import MAX_RESULT_BYTES, MediaAIError, MediaInput, download_media


def generation_payload(config: dict, args: dict, media: list[MediaInput]) -> tuple[str, dict]:
    kind = args["output_type"]
    payload = {"model": config["model"], "prompt": args["prompt"]}
    options = dict(args.get("parameters") or {})
    if kind == "audio":
        if media or any(key in args for key in ("ratio", "duration", "size", "resolution")):
            raise MediaAIError("unsupportedOptions")
        return "/audio/speech", {
            **options, "model": config["model"], "input": args["prompt"],
            "voice": args.get("voice", options.get("voice", "alloy")),
            "response_format": "mp3",
        }
    if "voice" in args or (kind == "image" and any(key in args for key in ("duration", "resolution"))):
        raise MediaAIError("unsupportedOptions")
    if kind == "video" and "resolution" in args and "size" not in args:
        # Standard video selects exact dimensions, not a provider resolution label.
        if args["resolution"] != "720P":
            raise MediaAIError("unsupportedOptions")
        if "ratio" not in args:
            payload["size"] = "1280x720"
    if "size" in args:
        payload["size"] = args["size"].replace("*", "x")
    elif "ratio" in args:
        sizes = {"1:1": "1024x1024", "16:9": "1536x864", "9:16": "864x1536",
                 "3:2": "1536x1024", "2:3": "1024x1536"}
        if kind == "video":
            sizes = {"16:9": "1280x720", "9:16": "720x1280"}
        if args["ratio"] not in sizes:
            raise MediaAIError("unsupportedOptions")
        payload["size"] = sizes[args["ratio"]]
    if kind == "image":
        if any(item.kind != "image" for item in media):
            raise MediaAIError("referenceImagesOnly")
        if media:
            # The current standard accepts URL/data-URL references directly.
            payload["images"] = [{"image_url": item.data_url} for item in media]
        return "/images/edits" if media else "/images/generations", {**options, **payload, "n": 1}
    if len(media) > 1 or any(item.kind != "image" or item.role not in {None, "", "first_frame", "reference_image"} for item in media):
        raise MediaAIError("unsupportedOptions")
    if media:
        payload["input_reference"] = {"image_url": media[0].data_url}
    if "duration" in args:
        payload["seconds"] = str(args["duration"])
    return "/videos", {**options, **payload}


def _normalize_video(data: dict) -> dict:
    task_id = data.get("id")
    status = {"queued": "PENDING", "in_progress": "RUNNING", "completed": "SUCCEEDED",
              "failed": "FAILED", "cancelled": "CANCELED"}.get(data.get("status"))
    if not task_id or not status:
        raise MediaAIError("providerFailed")
    output = {"task_id": task_id, "task_status": status}
    if status == "SUCCEEDED":
        output["video_url"] = "provider:/videos/" + quote(str(task_id), safe="") + "/content"
    if data.get("error"):
        output["code"] = (data["error"] or {}).get("code", "FAILED")
    return {"output": output, "usage": data.get("usage") or {}}


async def request(config: dict, path: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    async with httpx.AsyncClient(timeout=config.get("request_timeout") or 180, follow_redirects=False) as client:
        response = await client.request(
            "POST" if payload is not None else "GET", config["base_url"].rstrip("/") + path,
            json=payload, headers=headers,
        )
        if path == "/audio/speech" and response.is_success:
            if not response.content or (MAX_RESULT_BYTES is not None and len(response.content) > MAX_RESULT_BYTES):
                raise MediaAIError("invalidMedia")
            return {"output": {}, "_bytes": response.content,
                    "request_id": response.headers.get("x-request-id", "")}
        # Video terminal failures are valid task query responses, not transport errors.
        if path.startswith("/videos") and response.is_success:
            try:
                data = response.json()
            except ValueError as exc:
                raise MediaAIError("providerFailed") from exc
            if isinstance(data, dict) and data.get("id"):
                return _normalize_video(data)
        data = _check_response(response)
    if path.startswith("/images/"):
        images = data.get("data") or []
        first = images[0] if images else {}
        if first.get("b64_json"):
            try:
                raw = base64.b64decode(first["b64_json"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise MediaAIError("providerFailed") from exc
            if MAX_RESULT_BYTES is not None and len(raw) > MAX_RESULT_BYTES:
                raise MediaAIError("fileTooLarge")
            return {"output": {}, "_bytes": raw, "usage": data.get("usage") or {}}
        if first.get("url"):
            return {"output": {"choices": [{"message": {"content": [{"image": first["url"]}]}}]},
                    "usage": data.get("usage") or {}}
        raise MediaAIError("providerFailed")
    raise MediaAIError("providerFailed")


async def download_result(config: dict, reference: str) -> bytes:
    if not reference.startswith("provider:"):
        return await download_media(reference, max_bytes=MAX_RESULT_BYTES)
    path = reference.removeprefix("provider:")
    # Provider authentication is only attached to the exact configured endpoint.
    if not path.startswith("/videos/") or not path.endswith("/content") or "?" in path or "#" in path:
        raise MediaAIError("invalidArguments")
    async with httpx.AsyncClient(timeout=config.get("request_timeout") or 180, follow_redirects=False) as client:
        async with client.stream("GET", config["base_url"].rstrip("/") + path,
                                 headers={"Authorization": f"Bearer {config['api_key']}"}) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if MAX_RESULT_BYTES is not None and size > MAX_RESULT_BYTES:
                    raise MediaAIError("fileTooLarge")
                chunks.append(chunk)
            return b"".join(chunks)
