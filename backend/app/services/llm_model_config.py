"""Safe, idempotent operations for tenant LLM model configurations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm import LLMModel


class LLMModelConfigError(ValueError):
    """Raised when a source or target model configuration is invalid."""


async def clone_tenant_llm_model(
    db: AsyncSession,
    *,
    source_model_id: uuid.UUID,
    tenant_id: uuid.UUID,
    model_key: str,
    label: str,
) -> tuple[LLMModel, bool]:
    """Clone one tenant model without decrypting or returning its credential.

    The source row is locked so concurrent idempotent requests using the same
    source cannot create duplicate ``provider/model`` entries.  Every runtime
    tuning field is inherited; only the provider model key and display label
    are replaced.
    """
    normalized_model = model_key.strip()
    normalized_label = label.strip()
    if not normalized_model:
        raise LLMModelConfigError("model key is required")
    if not normalized_label:
        raise LLMModelConfigError("model label is required")

    source = (
        await db.execute(
            select(LLMModel)
            .where(LLMModel.id == source_model_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        raise LLMModelConfigError("source model not found")
    if source.tenant_id != tenant_id:
        raise LLMModelConfigError("source model does not belong to tenant")

    existing = (
        await db.execute(
            select(LLMModel).where(
                LLMModel.tenant_id == tenant_id,
                LLMModel.provider == source.provider,
                LLMModel.model == normalized_model,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    cloned = LLMModel(
        tenant_id=tenant_id,
        provider=source.provider,
        api_protocol=source.api_protocol,
        purposes=list(source.purposes or ["conversation"]),
        input_modalities=list(source.input_modalities or (["text", "image"] if source.supports_vision else ["text"])),
        model=normalized_model,
        # Copy the encrypted-at-rest value verbatim.  The clone path never
        # decrypts the credential and no response schema exposes this column.
        api_key_encrypted=source.api_key_encrypted,
        extra_headers_encrypted=source.extra_headers_encrypted,
        base_url=source.base_url,
        label=normalized_label,
        max_tokens_per_day=source.max_tokens_per_day,
        enabled=source.enabled,
        supports_vision=source.supports_vision,
        temperature=source.temperature,
        reasoning_effort=source.reasoning_effort,
        request_timeout=source.request_timeout,
        max_output_tokens=source.max_output_tokens,
        context_window=source.context_window,
        context_usage_ratio=float(source.context_usage_ratio or 0.7),
        compact_trigger_ratio=source.compact_trigger_ratio,
        keep_recent_turns=source.keep_recent_turns,
        compact_summary_max_tokens=source.compact_summary_max_tokens,
    )
    db.add(cloned)
    await db.flush()
    return cloned, True
