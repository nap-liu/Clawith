"""Mask MCP mapping secrets on reads and preserve them on masked writes."""

from __future__ import annotations

import re

MASKED_HEADER_VALUE = "***"
_SENSITIVE_HEADER_KEY = re.compile(r"(?i)(AUTH|TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE)")
_SENSITIVE_ENV_KEY = re.compile(r"(?i)(TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL)")


def is_sensitive_header(key: str) -> bool:
    return bool(_SENSITIVE_HEADER_KEY.search(str(key)))


def is_sensitive_env(key: str) -> bool:
    return bool(_SENSITIVE_ENV_KEY.search(str(key)))


def _mask_sensitive_values(values: dict | None, is_sensitive) -> dict:
    masked: dict = {}
    for key, value in (values or {}).items():
        text = str(value)
        masked[key] = MASKED_HEADER_VALUE if text and is_sensitive(str(key)) and "${" not in text else value
    return masked


def mask_sensitive_headers(headers: dict | None) -> dict:
    """Keep templates editable while hiding literal header credentials."""
    return _mask_sensitive_values(headers, is_sensitive_header)


def mask_sensitive_env(env: dict | None) -> dict | None:
    """Keep templates editable while hiding literal environment credentials."""
    if env is None:
        return None
    return _mask_sensitive_values(env, is_sensitive_env)


def _preserve_masked_values(incoming: dict, current: dict | None, is_sensitive, field_name: str) -> dict:
    current_by_key = {str(key).lower(): value for key, value in (current or {}).items()}
    resolved: dict = {}
    for key, value in incoming.items():
        if value == MASKED_HEADER_VALUE:
            current_value = current_by_key.get(str(key).lower())
            if current_value is None or not is_sensitive(str(key)):
                raise ValueError(f"A masked {field_name} can only preserve an existing secret")
            resolved[key] = current_value
        else:
            resolved[key] = value
    return resolved


def preserve_masked_headers(incoming: dict, current: dict | None) -> dict:
    """Resolve response mask sentinels back to the existing stored values."""
    return _preserve_masked_values(incoming, current, is_sensitive_header, "header")


def preserve_masked_env(incoming: dict, current: dict | None) -> dict:
    """Resolve response mask sentinels back to existing stored env values."""
    return _preserve_masked_values(incoming, current, is_sensitive_env, "environment value")
