"""Symmetric encryption for Tool.config env_inject values.

Reuses the project-wide `encrypt_data` / `decrypt_data` primitives that
already back LLMModel.api_key_encrypted. Values are tagged with an
"enc:v1:" prefix so a mixed plaintext/ciphertext dict (older records,
partial updates) can be decoded without a schema bump.
"""

from __future__ import annotations

from typing import Mapping

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data

_PREFIX = "enc:v1:"


def _key() -> str:
    return get_settings().SECRET_KEY


def encrypt_env(values: Mapping[str, str]) -> dict[str, str]:
    """Encrypt every value; keys are left as-is."""
    key = _key()
    return {k: _PREFIX + encrypt_data(v, key) for k, v in values.items()}


def decrypt_env(values: Mapping[str, str]) -> dict[str, str]:
    """Decrypt prefixed values; passthrough for legacy plaintext."""
    key = _key()
    out: dict[str, str] = {}
    for k, v in values.items():
        if isinstance(v, str) and v.startswith(_PREFIX):
            out[k] = decrypt_data(v[len(_PREFIX):], key)
        else:
            out[k] = v
    return out


def mask_env(values: Mapping[str, str]) -> dict[str, str]:
    """Return a redacted view for API responses."""
    return {k: "***" for k in values}
