"""Read-only field discovery for standards-based identity providers."""

import json
from typing import Any

import httpx

from app.core.token_cache import get_cached_token, set_cached_token

OAUTH_FIELD_SAMPLE_TTL_SECONDS = 60 * 60
SENSITIVE_FIELD_MARKERS = (
    "authorization",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)


async def discover_oidc_field_samples(
    config: dict[str, Any],
    *,
    provider_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Return fields from a recent login or a real client-credentials userinfo."""
    if provider_id:
        cached = await _get_cached_oidc_field_samples(provider_id)
        if cached:
            return cached
    async with httpx.AsyncClient(
        timeout=10,
        follow_redirects=True,
        transport=transport,
    ) as client:
        fields = await _discover_userinfo_fields(client, config)
        if fields:
            return {
                "source": "userinfo",
                "paths": [field["path"] for field in fields],
                "fields": fields,
            }
    return {
        "source": "authorization_required",
        "paths": [],
        "fields": [],
    }


async def cache_oidc_field_samples(provider_id: str, payload: Any) -> None:
    """Cache only bounded, non-sensitive samples from a successful user login."""
    fields = _sample_fields(payload)
    if not fields:
        return
    result = {
        "source": "recent_login",
        "paths": [field["path"] for field in fields],
        "fields": fields,
    }
    await set_cached_token(
        _field_sample_cache_key(provider_id),
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        OAUTH_FIELD_SAMPLE_TTL_SECONDS,
    )


async def discover_oidc_claim_paths(
    config: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ...]:
    """Compatibility wrapper returning only the discovered field paths."""
    result = await discover_oidc_field_samples(config, transport=transport)
    return tuple(result["paths"])


async def _discover_userinfo_fields(
    client: httpx.AsyncClient,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    token_url = str(config.get("token_url") or "").strip()
    user_info_url = str(config.get("user_info_url") or "").strip()
    client_id = str(config.get("client_id") or config.get("app_id") or "").strip()
    client_secret = str(
        config.get("client_secret") or config.get("app_secret") or ""
    )
    if not all((token_url, user_info_url, client_id, client_secret)):
        return []
    try:
        token_response = await client.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=httpx.BasicAuth(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        access_token = (
            token_payload.get("access_token")
            if isinstance(token_payload, dict)
            else None
        )
        if not isinstance(access_token, str) or not access_token.strip():
            return []
        user_response = await client.get(
            user_info_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
        user_response.raise_for_status()
        payload = user_response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return []
    if not isinstance(payload, (dict, list)):
        return []
    samples: dict[str, Any] = {}
    _collect_sample_fields(payload, "", samples)
    return [
        {"path": path, "sample_value": samples[path]}
        for path in sorted(samples)
    ]


async def _get_cached_oidc_field_samples(provider_id: str) -> dict[str, Any] | None:
    cached = await get_cached_token(_field_sample_cache_key(provider_id))
    if not cached:
        return None
    try:
        result = json.loads(cached)
    except (TypeError, ValueError):
        return None
    fields = result.get("fields") if isinstance(result, dict) else None
    if not isinstance(fields, list) or not fields:
        return None
    return result


def _field_sample_cache_key(provider_id: str) -> str:
    return f"clawith:field-sample:oauth:{provider_id}"


def _sample_fields(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, (dict, list)):
        return []
    samples: dict[str, Any] = {}
    _collect_sample_fields(payload, "", samples)
    return [
        {"path": path, "sample_value": samples[path]}
        for path in sorted(samples)
    ]


def _collect_sample_fields(
    value: Any,
    prefix: str,
    samples: dict[str, Any],
) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).strip()
            if not key or _is_sensitive_field(key):
                continue
            path = f"{prefix}.{key}" if prefix else key
            _collect_sample_fields(item, path, samples)
        return
    if isinstance(value, list):
        for item in value:
            _collect_sample_fields(item, prefix, samples)
        return
    if prefix and prefix not in samples:
        samples[prefix] = _bounded_sample(value)


def _is_sensitive_field(value: str) -> bool:
    normalized = "".join(character for character in value.lower() if character.isalnum())
    return any(marker in normalized for marker in SENSITIVE_FIELD_MARKERS)


def _bounded_sample(value: Any) -> Any:
    if isinstance(value, str):
        return value[:256]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:256]
