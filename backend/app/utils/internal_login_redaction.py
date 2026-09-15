"""Redact internal login credentials only at user-visible/log boundaries."""

import base64
import json
import re

_FRAGMENT = re.compile(r"agent_code=[A-Za-z0-9._%=-]+")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
INCOMPLETE_CREDENTIAL = re.compile(r"(?:agent_code=|eyJ)[A-Za-z0-9._%=-]*$")


def redact_internal_login_values(value):
    """Apply credential redaction to a structured user-facing projection."""
    if isinstance(value, dict):
        return {key: redact_internal_login_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_internal_login_values(item) for item in value]
    return redact_internal_login(value) if isinstance(value, str) else value


def redact_internal_login(text: str) -> str:
    def replace_token(match):
        try:
            encoded = match.group().split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if isinstance(payload, dict) and (
                payload.get("aud") == "agent-temporary-login" or payload.get("login_code_hash")
            ):
                return "******"
        except (ValueError, TypeError, UnicodeError):
            pass
        return match.group()

    return _JWT.sub(replace_token, _FRAGMENT.sub("agent_code=******", text))
