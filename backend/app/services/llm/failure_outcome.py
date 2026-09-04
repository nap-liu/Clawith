"""String-compatible failures returned by the shared LLM boundary."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

MODEL_RESPONSE_IDLE_TIMEOUT_CODE = "model_response_idle_timeout"
MODEL_RESPONSE_IDLE_TIMEOUT_MESSAGE_KEY = "errors.modelResponseIdleTimeout"
_RESOURCE_DIR = Path(__file__).resolve().parents[2] / "i18n"


class LLMFailure(str):
    """A user-facing string that retains machine-readable failure policy."""

    code: str
    message_key: str
    retryable: bool
    allow_failover: bool

    def __new__(
        cls,
        content: str,
        *,
        code: str,
        message_key: str,
        retryable: bool,
        allow_failover: bool,
    ) -> "LLMFailure":
        value = super().__new__(cls, content)
        value.code = code
        value.message_key = message_key
        value.retryable = retryable
        value.allow_failover = allow_failover
        return value


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


def llm_failure_code(value: object) -> str | None:
    code = getattr(value, "code", None)
    return str(code) if code else None


def localize_llm_failure(value: LLMFailure, locale: str | None) -> LLMFailure:
    return LLMFailure(
        render_message(value.message_key, locale),
        code=value.code,
        message_key=value.message_key,
        retryable=value.retryable,
        allow_failover=value.allow_failover,
    )


__all__ = [
    "LLMFailure",
    "MODEL_RESPONSE_IDLE_TIMEOUT_CODE",
    "MODEL_RESPONSE_IDLE_TIMEOUT_MESSAGE_KEY",
    "llm_failure_code",
    "localize_llm_failure",
    "model_response_idle_timeout_failure",
    "render_message",
]
