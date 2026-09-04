"""Session-independent LLM configuration used by long-running executions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.llm import LLMModel


@dataclass(frozen=True, slots=True)
class RuntimeLLMModel:
    """Immutable value snapshot of an ``LLMModel`` ORM row.

    Long-running WebSocket and background turns must not retain SQLAlchemy
    entities after their database session closes.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    provider: str
    model: str
    api_key_encrypted: str
    base_url: str | None
    label: str
    max_tokens_per_day: int | None
    enabled: bool
    supports_vision: bool
    temperature: float | None
    request_timeout: int | None
    max_output_tokens: int | None
    context_window: int
    context_usage_ratio: float
    compact_trigger_ratio: float
    keep_recent_turns: int
    compact_summary_max_tokens: int
    reasoning_effort: str | None = None

    @classmethod
    def from_orm(cls, model: LLMModel) -> "RuntimeLLMModel":
        return cls(
            id=model.id,
            tenant_id=model.tenant_id,
            provider=model.provider,
            model=model.model,
            api_key_encrypted=model.api_key_encrypted,
            base_url=model.base_url,
            label=model.label,
            max_tokens_per_day=model.max_tokens_per_day,
            enabled=model.enabled,
            supports_vision=model.supports_vision,
            temperature=model.temperature,
            reasoning_effort=getattr(model, "reasoning_effort", None),
            request_timeout=model.request_timeout,
            max_output_tokens=model.max_output_tokens,
            context_window=model.context_window,
            context_usage_ratio=float(getattr(model, "context_usage_ratio", None) or 0.7),
            compact_trigger_ratio=model.compact_trigger_ratio,
            keep_recent_turns=model.keep_recent_turns,
            compact_summary_max_tokens=model.compact_summary_max_tokens,
        )
