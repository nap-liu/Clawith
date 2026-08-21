from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.llm import LLMModel
from app.services.llm_model_config import (
    LLMModelConfigError,
    clone_tenant_llm_model,
)


def _result(value):
    result = Mock()
    result.scalar_one_or_none.return_value = value
    return result


def _source(*, tenant_id: uuid.UUID) -> LLMModel:
    return LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="qwen",
        model="qwen3.8-max",
        api_key_encrypted="encrypted-reference",
        base_url="https://dashscope.invalid/compatible-mode/v1",
        label="Qwen3.8 Max",
        max_tokens_per_day=123,
        enabled=True,
        supports_vision=False,
        temperature=0.4,
        request_timeout=180,
        max_output_tokens=4096,
        context_window=1_000_000,
        compact_trigger_ratio=0.85,
        keep_recent_turns=8,
        compact_summary_max_tokens=2000,
    )


@pytest.mark.asyncio
async def test_clone_model_copies_secret_and_runtime_tuning_without_decrypting():
    tenant_id = uuid.uuid4()
    source = _source(tenant_id=tenant_id)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(source), _result(None)]),
        add=Mock(),
        flush=AsyncMock(),
    )

    cloned, created = await clone_tenant_llm_model(
        db,
        source_model_id=source.id,
        tenant_id=tenant_id,
        model_key=" qwen3.5-plus ",
        label=" Qwen3.5 Plus ",
    )

    assert created is True
    assert cloned.model == "qwen3.5-plus"
    assert cloned.label == "Qwen3.5 Plus"
    assert cloned.api_key_encrypted == "encrypted-reference"
    assert cloned.provider == source.provider
    assert cloned.base_url == source.base_url
    assert cloned.request_timeout == source.request_timeout
    assert cloned.context_window == source.context_window
    assert cloned.max_output_tokens == source.max_output_tokens
    db.add.assert_called_once_with(cloned)
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_clone_model_is_idempotent_for_tenant_provider_model():
    tenant_id = uuid.uuid4()
    source = _source(tenant_id=tenant_id)
    existing = _source(tenant_id=tenant_id)
    existing.model = "qwen3.5-plus"
    existing.label = "Existing"
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(source), _result(existing)]),
        add=Mock(),
        flush=AsyncMock(),
    )

    cloned, created = await clone_tenant_llm_model(
        db,
        source_model_id=source.id,
        tenant_id=tenant_id,
        model_key="qwen3.5-plus",
        label="Ignored on retry",
    )

    assert cloned is existing
    assert created is False
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_clone_model_rejects_cross_tenant_source():
    source = _source(tenant_id=uuid.uuid4())
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_result(source)),
        add=Mock(),
        flush=AsyncMock(),
    )

    with pytest.raises(LLMModelConfigError, match="does not belong"):
        await clone_tenant_llm_model(
            db,
            source_model_id=source.id,
            tenant_id=uuid.uuid4(),
            model_key="qwen3.5-plus",
            label="Qwen3.5 Plus",
        )
