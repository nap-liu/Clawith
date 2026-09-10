"""Shared optional provider headers, encrypted in enterprise model storage."""

import json

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.services.model_platform import model_service_platform


def default_model_headers(provider: str, base_url: str | None) -> dict[str, str]:
    if not base_url:
        from app.services.llm.client_registry import get_provider_spec

        spec = get_provider_spec(provider)
        base_url = spec.default_base_url if spec else None
    if model_service_platform(provider, base_url) == "bailian":
        return {"X-DashScope-Wait-Timeout": "120"}
    return {}


def encrypt_model_headers(headers: dict[str, str] | None) -> str | None:
    """NULL inherits defaults; an encrypted empty object explicitly disables them."""
    if headers is None:
        return None
    return encrypt_data(json.dumps(headers), get_settings().SECRET_KEY)


def resolve_model_headers(model) -> dict[str, str]:
    encrypted = getattr(model, "extra_headers_encrypted", None)
    if isinstance(encrypted, str) and encrypted:
        return json.loads(decrypt_data(encrypted, get_settings().SECRET_KEY))
    return default_model_headers(model.provider, model.base_url)
