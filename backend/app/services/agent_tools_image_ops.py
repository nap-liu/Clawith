from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.agent_tools import _get_tool_config


async def _upload_image(agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Upload an image to ImageKit CDN and return the public URL.

    Credential resolution order:
    1. Global tool config (admin-set, shared by all agents)
    2. Per-agent tool config override (agent-specific)
    """
    import httpx
    import base64

    file_path = arguments.get("file_path")
    url = arguments.get("url")
    file_name = arguments.get("file_name")
    folder = arguments.get("folder", "/clawith")

    if not file_path and not url:
        return "❌ Please provide either 'file_path' (workspace path) or 'url' (public image URL)"

    # ── Load ImageKit credentials (Agent > Company priority) ──
    private_key = ""
    url_endpoint = ""
    try:
        # Use standard _get_tool_config (Agent > Company, cached, schema-aware decryption)
        config = await _get_tool_config(agent_id, "upload_image") or {}
        private_key = config.get("private_key", "")
        url_endpoint = config.get("url_endpoint", "")
    except Exception as e:
        logger.error(f"[UploadImage] Config load error: {e}")

    if not private_key:
        return "❌ ImageKit Private Key not configured. Ask your admin to configure it in Enterprise Settings → Tools → Upload Image, or set it in your agent's tool config."

    # ── Prepare the file ──
    form_data = {}
    file_content = None

    if file_path:
        # Read from workspace
        full_path = (ws / file_path).resolve()
        if not str(full_path).startswith(str(ws)):
            return "❌ Access denied: path is outside the workspace"
        if not full_path.exists():
            return f"❌ File not found: {file_path}"
        if not full_path.is_file():
            return f"❌ Not a file: {file_path}"

        # Check file size (max 25MB for free plan)
        size_mb = full_path.stat().st_size / (1024 * 1024)
        if size_mb > 25:
            return f"❌ File too large ({size_mb:.1f}MB). Maximum is 25MB."

        file_content = full_path.read_bytes()
        if not file_name:
            file_name = full_path.name
    elif url:
        # Pass URL directly to ImageKit
        form_data["file"] = url
        if not file_name:
            from urllib.parse import urlparse

            file_name = urlparse(url).path.split("/")[-1] or "image.jpg"

    if not file_name:
        file_name = "image.png"

    form_data["fileName"] = file_name
    form_data["folder"] = folder
    form_data["useUniqueFileName"] = "true"

    # ── Upload to ImageKit V2 ──
    auth_string = base64.b64encode(f"{private_key}:".encode()).decode()

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if file_content:
                # Binary upload via multipart
                files = {"file": (file_name, file_content)}
                resp = await client.post(
                    "https://upload.imagekit.io/api/v2/files/upload",
                    headers={"Authorization": f"Basic {auth_string}"},
                    data=form_data,
                    files=files,
                )
            else:
                # URL upload via form data
                resp = await client.post(
                    "https://upload.imagekit.io/api/v2/files/upload",
                    headers={"Authorization": f"Basic {auth_string}"},
                    data=form_data,
                )

        if resp.status_code in (200, 201):
            result = resp.json()
            cdn_url = result.get("url", "")
            file_id = result.get("fileId", "")
            size = result.get("size", 0)
            size_str = f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f}MB"
            return (
                f"✅ Image uploaded successfully!\n\n"
                f"**CDN URL**: {cdn_url}\n"
                f"**File ID**: {file_id}\n"
                f"**Size**: {size_str}\n"
                f"**Name**: {result.get('name', file_name)}"
            )
        else:
            error_detail = resp.text[:300]
            return f"❌ Upload failed (HTTP {resp.status_code}): {error_detail}"

    except httpx.TimeoutException:
        return "❌ Upload timed out after 60s. The file may be too large or the network is slow."
    except Exception as e:
        return f"❌ Upload error: {type(e).__name__}: {str(e)[:300]}"


async def _generate_image(agent_id: uuid.UUID, ws: Path, arguments: dict, provider: str) -> str:
    """Generate an image using the configured provider and save to workspace.

    Supported providers:
    - siliconflow: OpenAI-compatible API (FLUX models, China-friendly)
    - openai: Native OpenAI API (GPT Image)
    - google: Google Gemini Native Image API (Nano Banana)
    - custom: Configurable HTTP API for gateways such as TokenRouter/OpenRouter

    The tool config is resolved via the standard _get_tool_config() hierarchy:
    global tool config (admin-set) -> per-agent tool config override.
    """
    import httpx
    from datetime import datetime

    prompt = arguments.get("prompt")
    if not prompt:
        return "❌ Missing required argument 'prompt' for generate_image"

    size = arguments.get("size", "1024x1024")
    save_path = arguments.get("save_path", "")

    # Load tool config (global -> per-agent override)
    tool_key = f"generate_image_{provider}"
    config = await _get_tool_config(agent_id, tool_key) or {}
    model = config.get("model", "")
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "")

    if not api_key:
        return (
            "❌ Image generation API key not configured. "
            "Ask your admin to configure it in Enterprise Settings → Tools → Generate Image."
        )

    # Generate the save path if not provided
    if not save_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Derive a short slug from the prompt for a more descriptive filename
        slug = "_".join(prompt.split()[:4]).lower()
        slug = "".join(c for c in slug if c.isalnum() or c == "_")[:40]
        save_path = f"workspace/images/{slug}_{ts}.png"

    # Ensure the target directory exists and path is within workspace
    full_save_path = (ws / save_path).resolve()
    if not str(full_save_path).startswith(str(ws.resolve())):
        return "❌ Access denied: save path is outside the workspace"
    full_save_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if provider == "siliconflow":
            image_bytes = await _generate_image_siliconflow(
                api_key,
                model or "black-forest-labs/FLUX.1-schnell",
                base_url or "https://api.siliconflow.cn/v1",
                prompt,
                size,
            )
        elif provider == "openai":
            image_bytes = await _generate_image_openai(
                api_key,
                model or "gpt-image-1",
                base_url or "https://api.openai.com/v1",
                prompt,
                size,
            )
        elif provider == "google":
            image_bytes = await _generate_image_google(
                api_key,
                model or "gemini-2.5-flash-image",
                base_url or "https://generativelanguage.googleapis.com/v1beta",
                prompt,
                size,
            )
        elif provider == "custom":
            image_bytes = await _generate_image_custom_api(
                api_key=api_key,
                model=model,
                base_url=base_url,
                endpoint_path=config.get("endpoint_path") or "/chat/completions",
                request_body_template_json=config.get("request_body_template_json") or "",
                response_image_path=config.get("response_image_path") or "choices.0.message.images.0.image_url.url",
                extra_headers_json=config.get("extra_headers_json") or "",
                timeout_seconds=config.get("timeout_seconds") or 120,
                prompt=prompt,
                size=size,
            )
        else:
            return f"❌ Unknown image generation provider: {provider}. Supported: siliconflow, openai, google, custom"

        if not image_bytes:
            return "❌ Image generation returned empty result. Please try a different prompt."

        # Save the generated image to workspace
        full_save_path.write_bytes(image_bytes)
        size_kb = len(image_bytes) / 1024

        # Build the API path for inline display in chat
        # The MarkdownRenderer will auto-inject the auth token for /api/agents/ paths
        api_image_path = f"/api/agents/{agent_id}/files/download?path={save_path}"

        return (
            f"✅ Image generated and saved to: {save_path}\n"
            f"Size: {size_kb:.1f} KB | Provider: {provider} | Model: {model or '(default)'}\n\n"
            f"Display this image to the user using this exact markdown:\n"
            f"![generated image]({api_image_path})"
        )
    except httpx.TimeoutException:
        logger.error(f"[GenerateImage] Timeout ({provider}): took longer than 120 seconds or network unreachable.")
        return (
            f"❌ Image generation failed ({provider}): API request timed out after 120 seconds. "
            f"This is usually caused by network issues or the model taking too long to generate."
        )
    except Exception as e:
        err_msg = str(e) or type(e).__name__
        logger.error(f"[GenerateImage] Error ({provider}): {err_msg}")
        return f"❌ Image generation failed ({provider}): {err_msg[:400]}"


async def _generate_image_siliconflow(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via SiliconFlow (OpenAI-compatible images.generate API).

    SiliconFlow returns a temporary URL (expires in ~1 hour), so we download
    the image bytes immediately after generation.
    """
    import httpx
    import base64

    url = f"{base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": size,  # SiliconFlow uses 'image_size' instead of 'size'
        "n": 1,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            # Extract API error message for better diagnostics
            try:
                err_body = resp.json()
                err_msg = err_body.get("message") or err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"SiliconFlow API error ({resp.status_code}): {err_msg}")
        data = resp.json()

        # SiliconFlow may return url or b64_json
        image_data = data.get("data", [{}])[0]
        image_url = image_data.get("url")
        if image_url:
            # Download the temporary URL immediately
            img_resp = await client.get(image_url, timeout=60)
            img_resp.raise_for_status()
            return img_resp.content

        b64 = image_data.get("b64_json")
        if b64:
            return base64.b64decode(b64)

        raise ValueError(f"No image URL or b64_json in SiliconFlow response: {data}")


async def _generate_image_openai(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via OpenAI GPT Image API.

    Requests b64_json format to avoid dealing with URL expiry.
    """
    import httpx
    import base64

    url = f"{base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"OpenAI API error ({resp.status_code}): {err_msg}")
        data = resp.json()

        image_data = data.get("data", [{}])[0]
        b64 = image_data.get("b64_json")
        if b64:
            return base64.b64decode(b64)

        # Fallback: try URL
        image_url = image_data.get("url")
        if image_url:
            img_resp = await client.get(image_url, timeout=60)
            img_resp.raise_for_status()
            return img_resp.content

        raise ValueError(f"No b64_json or URL in OpenAI response: {data}")


def _json_path_get(data: Any, path: str) -> Any:
    """Read a simple dotted JSON path, with numeric list indexes."""
    if not path:
        return None

    current: Any = data
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        if isinstance(current, list):
            if not part.isdigit():
                return None
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _render_json_template(template_json: str, variables: dict[str, str]) -> dict:
    """Parse JSON first, then replace placeholders inside string values.

    This avoids corrupting JSON when a prompt contains quotes, newlines, or
    other characters that need escaping.
    """
    template_text = template_json.strip()
    parse_errors: list[str] = []

    candidates = [template_text]
    normalized_quotes = (
        template_text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    )
    if normalized_quotes != template_text:
        candidates.append(normalized_quotes)

    # Users often paste a JSON example copied from a string literal, leaving
    # escaped quotes like { \"model\": \"{model}\" }. Treat that as JSON too.
    for text in list(candidates):
        if '\\"' in text:
            candidates.append(text.replace('\\"', '"'))

    template = None
    for text in candidates:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            template = parsed
            break
        except Exception as e:
            parse_errors.append(str(e))

    if template is None:
        detail = parse_errors[-1] if parse_errors else "unknown parse error"
        raise ValueError(detail)

    def render(value: Any) -> Any:
        if isinstance(value, str):
            rendered = value
            for key, replacement in variables.items():
                rendered = rendered.replace("{" + key + "}", replacement)
            return rendered
        if isinstance(value, list):
            return [render(item) for item in value]
        if isinstance(value, dict):
            return {key: render(item) for key, item in value.items()}
        return value

    rendered = render(template)
    if not isinstance(rendered, dict):
        raise ValueError("Request body template must be a JSON object.")
    return rendered


def _json_structure_preview(data: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(data, dict):
        return {k: _json_structure_preview(v, depth + 1) for k, v in list(data.items())[:12]}
    if isinstance(data, list):
        preview = [_json_structure_preview(item, depth + 1) for item in data[:2]]
        if len(data) > 2:
            preview.append(f"... {len(data)} items total")
        return preview
    if isinstance(data, str):
        if data.startswith("data:image"):
            return f"data:image... len={len(data)}"
        if len(data) > 160:
            return data[:160] + "..."
    return data


def _find_first_image_reference(data: Any) -> Any:
    common_paths = [
        "choices.0.message.images.0.image_url.url",
        "choices.0.message.images.0.image_url",
        "data.0.b64_json",
        "data.0.url",
        "output.0.content.0.image_url",
        "output.0.content.0.image_base64",
    ]
    for path in common_paths:
        value = _json_path_get(data, path)
        if value:
            return value

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("url", "b64_json", "image_url", "image_base64"):
                nested = value.get(key)
                if isinstance(nested, str) and nested:
                    return nested
                if isinstance(nested, dict):
                    found = walk(nested)
                    if found:
                        return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, str) and (
            value.startswith("data:image") or value.startswith("http://") or value.startswith("https://")
        ):
            return value
        return None

    return walk(data)


async def _custom_image_reference_to_bytes(image_ref: Any, client: Any) -> bytes:
    import base64

    if isinstance(image_ref, dict):
        image_ref = image_ref.get("url") or image_ref.get("b64_json") or image_ref.get("image_base64")

    if not isinstance(image_ref, str) or not image_ref:
        raise ValueError("Response image path did not resolve to a URL, data URL, or base64 string.")

    if image_ref.startswith("data:image"):
        _, _, encoded = image_ref.partition(",")
        if not encoded:
            raise ValueError("Image data URL did not contain base64 payload.")
        return base64.b64decode(encoded)

    if image_ref.startswith("http://") or image_ref.startswith("https://"):
        img_resp = await client.get(image_ref, timeout=60)
        img_resp.raise_for_status()
        return img_resp.content

    return base64.b64decode(image_ref)


async def _generate_image_custom_api(
    api_key: str,
    model: str,
    base_url: str,
    endpoint_path: str,
    request_body_template_json: str,
    response_image_path: str,
    extra_headers_json: str,
    timeout_seconds: int | str,
    prompt: str,
    size: str,
) -> bytes:
    """Generate image via a configurable gateway API.

    The default request/response shape supports TokenRouter and OpenRouter:
    POST /chat/completions with image/text modalities, image returned in
    choices.0.message.images.0.image_url.url as a data URL.
    """
    import httpx

    if not base_url:
        raise ValueError("Custom image API base_url is not configured.")
    if not model:
        raise ValueError("Custom image API model is not configured.")

    timeout = int(timeout_seconds or 120)
    endpoint = endpoint_path or "/chat/completions"
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        url = endpoint
    else:
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    variables = {"prompt": prompt, "size": size, "model": model}
    if request_body_template_json.strip():
        try:
            payload = _render_json_template(request_body_template_json, variables)
        except Exception as e:
            raise ValueError(f"Invalid request_body_template_json: {e}")
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
            "stream": False,
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers_json.strip():
        try:
            extra_headers = json.loads(extra_headers_json)
        except Exception as e:
            raise ValueError(f"Invalid extra_headers_json: {e}")
        if not isinstance(extra_headers, dict):
            raise ValueError("extra_headers_json must be a JSON object.")
        headers.update({str(k): str(v) for k, v in extra_headers.items() if v is not None})

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code < 200 or resp.status_code >= 300:
            try:
                err_body = resp.json()
                err_msg = (
                    err_body.get("error", {}).get("message")
                    if isinstance(err_body.get("error"), dict)
                    else err_body.get("message")
                ) or resp.text[:300]
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"Custom image API error ({resp.status_code}): {err_msg}")

        try:
            data = resp.json()
        except Exception:
            raise ValueError("Custom image API returned non-JSON response.")

        image_ref = _json_path_get(data, response_image_path) if response_image_path else None
        if not image_ref:
            image_ref = _find_first_image_reference(data)
        if not image_ref:
            preview = json.dumps(_json_structure_preview(data), ensure_ascii=False)
            raise ValueError(
                "No image found in custom image API response. "
                f"Check response_image_path. Response structure: {preview[:800]}"
            )

        return await _custom_image_reference_to_bytes(image_ref, client)


async def _generate_image_google(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via Google Gemini Native Image API (Nano Banana) or Vertex AI.

    Uses the Gemini generateContent endpoint with responseModalities=["IMAGE"].
    Converts WxH size to aspect ratio format (e.g. 1024x1024 -> 1:1).
    Extracts the generated image from inlineData in the response parts.
    """
    import httpx
    import base64

    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"

    # Convert WxH size to aspect ratio for Gemini API
    # Supported: 1:1, 3:4, 4:3, 9:16, 16:9
    size_to_ratio = {
        "1024x1024": "1:1",
        "768x1024": "3:4",
        "1024x768": "4:3",
        "768x1366": "9:16",
        "1366x768": "16:9",
        "1024x1536": "3:4",
        "1536x1024": "4:3",
    }
    aspect_ratio = size_to_ratio.get(size, "1:1")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
            },
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"Google Gemini API error ({resp.status_code}): {err_msg}")
        data = resp.json()

        # Extract image from response candidates -> content -> parts
        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError(f"No candidates in Gemini response: {data}")

        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            if "inlineData" in part:
                b64 = part["inlineData"]["data"]
                return base64.b64decode(b64)

        raise ValueError(
            f"No image (inlineData) found in Gemini response parts. "
            f"Parts: {[p.get('text', '(image)') if 'text' in p else '(inline)' for p in parts]}"
        )


__all__ = [
    "_upload_image",
    "_generate_image",
    "_generate_image_siliconflow",
    "_generate_image_openai",
    "_json_path_get",
    "_render_json_template",
    "_json_structure_preview",
    "_find_first_image_reference",
    "_custom_image_reference_to_bytes",
    "_generate_image_custom_api",
    "_generate_image_google",
]
