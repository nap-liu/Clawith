"""Input capability matching shared by media admission and the async worker."""

import json
import mimetypes
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import get_settings
from app.core.security import encrypt_data
from app.database import async_session
from app.models.llm import LLMModel
from app.services.media_ai_io import MediaAIError
from app.services.model_capabilities import model_modalities, purpose_clause


def source_modalities(files: list) -> set[str]:
    """Cheap hints only; opaque URLs are resolved by the existing worker loader."""
    result = set()
    for value in files:
        item = value if isinstance(value, dict) else {"source": value}
        if item.get("kind"):
            result.add(item["kind"])
            continue
        source = item.get("source", "")
        mime = source[5:].split(";", 1)[0] if source.startswith("data:") else mimetypes.guess_type(urlsplit(source).path)[0]
        kind = (mime or "").split("/", 1)[0]
        if kind in {"image", "audio", "video"}:
            result.add(kind)
    return result


def history_modalities(history: list) -> set[str]:
    result = set()
    for message in history:
        content = message.get("content")
        for item in content if isinstance(content, list) else []:
            kind = {"image_url": "image", "input_image": "image", "video_url": "video",
                    "input_video": "video", "input_audio": "audio"}.get(item.get("type"))
            if kind:
                result.add(kind)
    return result


def supports_inputs(model, required: set[str]) -> bool:
    return required.issubset(set(model_modalities(model)))


async def matching_understanding_model(db, tenant_id, preferred, required: set[str], *, explicit=False):
    if preferred is not None and (explicit or supports_inputs(preferred, required)):
        return preferred
    candidates = (await db.scalars(select(LLMModel).where(
        LLMModel.tenant_id == tenant_id, LLMModel.enabled.is_(True), purpose_clause("media_understanding"),
    ).order_by(LLMModel.model, LLMModel.id))).all()
    model = next((item for item in candidates if supports_inputs(item, required)), preferred or (candidates[0] if candidates else None))
    if model is None:
        raise MediaAIError("notConfigured")
    return model


async def resolve_loaded_inputs(config: dict, tenant_id, required: set[str], *, explicit=False) -> dict:
    """Correct uncertain admission hints before a provider call, never after one."""
    from app.services.media_model_selection import model_connection

    if explicit or not config.get("model_id") or required.issubset(set(config.get("input_modalities") or [])):
        return config
    # Keep the accepted connection if no better metadata match exists. Capability
    # labels can lag provider upgrades and are not an input authorization boundary.
    preferred = SimpleNamespace(input_modalities=config.get("input_modalities"))
    async with async_session() as db:
        model = await matching_understanding_model(db, tenant_id, preferred, required)
        if model is preferred:
            return config
        resolved = model_connection(model)
    fallback = config.get("fallback_connection")
    if fallback and fallback.get("model_id") != resolved["model_id"]:
        resolved["fallback_connection"] = fallback
    resolved["retained_input_modalities"] = config.get("retained_input_modalities", [])
    return resolved


def freeze_request_connection(request: dict, config: dict) -> None:
    request["config"] = {key: value for key, value in config.items()
                         if key not in {"api_key", "extra_headers", "runtime_model", "fallback_connection"}}
    request["connection_ref"] = encrypt_data(json.dumps(config), get_settings().SECRET_KEY)
