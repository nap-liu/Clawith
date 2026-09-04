"""Mask MCP header secrets on reads and preserve them on masked writes."""

from __future__ import annotations

import re

MASKED_HEADER_VALUE = "***"
_SENSITIVE_HEADER_KEY = re.compile(r"(?i)(AUTH|TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE)")


def is_sensitive_header(key: str) -> bool:
    return bool(_SENSITIVE_HEADER_KEY.search(str(key)))


def mask_sensitive_headers(headers: dict | None) -> dict:
    """Keep templates editable while hiding literal header credentials."""
    masked: dict = {}
    for key, value in (headers or {}).items():
        text = str(value)
        masked[key] = MASKED_HEADER_VALUE if text and is_sensitive_header(str(key)) and "${" not in text else value
    return masked


def preserve_masked_headers(incoming: dict, current: dict | None) -> dict:
    """Resolve response mask sentinels back to the existing stored values."""
    current_by_key = {str(key).lower(): value for key, value in (current or {}).items()}
    resolved: dict = {}
    for key, value in incoming.items():
        if value == MASKED_HEADER_VALUE:
            current_value = current_by_key.get(str(key).lower())
            if current_value is None or not is_sensitive_header(str(key)):
                raise ValueError("A masked header can only preserve an existing secret")
            resolved[key] = current_value
        else:
            resolved[key] = value
    return resolved
