"""String-compatible failures returned by the shared LLM boundary."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

MODEL_RESPONSE_IDLE_TIMEOUT_CODE = "model_response_idle_timeout"
MODEL_RESPONSE_IDLE_TIMEOUT_MESSAGE_KEY = "errors.modelResponseIdleTimeout"
_RESOURCE_DIR = Path(__file__).resolve().parents[2] / "i18n"


class LLMFailure(str):
    """A user-facing string that retains machine-readable failure policy."""

    code: str
    message_key: str
    retryable: bool
    allow_failover: bool
    details: Mapping[str, str | int | float | bool | None]

    def __new__(
        cls,
        content: str,
        *,
        code: str,
        message_key: str,
        retryable: bool,
        allow_failover: bool,
        details: Mapping[str, Any] | None = None,
    ) -> "LLMFailure":
        value = super().__new__(cls, content)
        value.code = code
        value.message_key = message_key
        value.retryable = retryable
        value.allow_failover = allow_failover
        value.details = MappingProxyType(_bounded_details(details))
        return value


def _bounded_details(details: Mapping[str, Any] | None) -> dict[str, str | int | float | bool | None]:
    bounded: dict[str, str | int | float | bool | None] = {}
    for key, raw_value in list((details or {}).items())[:24]:
        safe_key = str(key)[:64]
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            bounded[safe_key] = raw_value
        else:
            bounded[safe_key] = str(raw_value)[:256]
    return bounded


@lru_cache(maxsize=4)
def _messages(locale: str) -> dict[str, str]:
    language = "en" if str(locale or "").lower().startswith("en") else "zh"
    with (_RESOURCE_DIR / f"{language}.json").open(encoding="utf-8") as handle:
        return dict(json.load(handle))


def render_message(message_key: str, locale: str | None = None) -> str:
    """Render one product message from the backend locale resources."""
    messages = _messages(locale or "zh")
    return messages.get(message_key) or _messages("zh").get(message_key, message_key)


def model_response_idle_timeout_failure(locale: str | None = None) -> LLMFailure:
    return LLMFailure(
        render_message(MODEL_RESPONSE_IDLE_TIMEOUT_MESSAGE_KEY, locale),
        code=MODEL_RESPONSE_IDLE_TIMEOUT_CODE,
        message_key=MODEL_RESPONSE_IDLE_TIMEOUT_MESSAGE_KEY,
        retryable=False,
        allow_failover=False,
    )


def make_llm_failure(
    *,
    code: str,
    message_key: str,
    retryable: bool = False,
    allow_failover: bool = False,
    details: Mapping[str, Any] | None = None,
    locale: str | None = None,
) -> LLMFailure:
    return LLMFailure(
        render_message(message_key, locale),
        code=code,
        message_key=message_key,
        retryable=retryable,
        allow_failover=allow_failover,
        details=details,
    )


def llm_failure_code(value: object) -> str | None:
    code = getattr(value, "code", None)
    return str(code) if code else None


def llm_failure_meta(value: object) -> dict[str, Any]:
    code = llm_failure_code(value)
    if code is None:
        return {}
    details = dict(getattr(value, "details", {}) or {})
    return {
        "error_code": code,
        "llm_failure": {"code": code, **details},
    }


def localize_llm_failure(value: LLMFailure, locale: str | None) -> LLMFailure:
    return LLMFailure(
        render_message(value.message_key, locale),
        code=value.code,
        message_key=value.message_key,
        retryable=value.retryable,
        allow_failover=value.allow_failover,
        details=value.details,
    )


__all__ = [
    "LLMFailure",
    "MODEL_RESPONSE_IDLE_TIMEOUT_CODE",
    "MODEL_RESPONSE_IDLE_TIMEOUT_MESSAGE_KEY",
    "llm_failure_code",
    "llm_failure_meta",
    "localize_llm_failure",
    "make_llm_failure",
    "model_response_idle_timeout_failure",
    "render_message",
]
