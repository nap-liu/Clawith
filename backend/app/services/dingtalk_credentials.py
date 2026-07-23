"""Stable, non-reversible identities for DingTalk credential pairs."""

from __future__ import annotations

import hashlib
import hmac

from app.config import get_settings


def dingtalk_credential_fingerprint(app_key: str, app_secret: str) -> str:
    """Return a server-keyed fingerprint without exposing either credential."""
    message = f"{len(app_key)}:{app_key}{len(app_secret)}:{app_secret}".encode()
    key = get_settings().SECRET_KEY.encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()
