import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.enterprise import _validate_model_context_budget
from app.models.llm import LLMModel
from app.schemas.schemas import AgentUpdate, LLMModelCreate


def _model_payload(**overrides):
    payload = {
        "provider": "custom",
        "model": "test-model",
        "api_key": "secret",
        "label": "Test",
        "max_output_tokens": 10_000,
        "context_window": 200_000,
        "context_usage_ratio": 0.6,
    }
    payload.update(overrides)
    return payload


def test_model_pool_accepts_independent_context_usage_ratio():
    config = LLMModelCreate(**_model_payload())
    assert config.context_window == 200_000
    assert config.context_usage_ratio == 0.6


def test_model_pool_defaults_new_models_to_seventy_percent():
    payload = _model_payload()
    payload.pop("context_usage_ratio")
    assert LLMModelCreate(**payload).context_usage_ratio == 0.7


def test_model_pool_requires_three_complete_protected_turns():
    assert LLMModelCreate(**_model_payload()).keep_recent_turns == 3
    with pytest.raises(ValidationError):
        LLMModelCreate(**_model_payload(keep_recent_turns=2))


@pytest.mark.parametrize("ratio", [0.0, 1.01])
def test_model_pool_rejects_invalid_context_usage_ratio(ratio):
    with pytest.raises(ValidationError):
        LLMModelCreate(**_model_payload(context_usage_ratio=ratio))


def test_model_pool_rejects_output_reservation_that_consumes_usable_window():
    model = LLMModel(
        provider="custom",
        model="test-model",
        api_key_encrypted="encrypted",
        label="Test",
        context_window=20_000,
        context_usage_ratio=0.5,
        max_output_tokens=10_000,
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_model_context_budget(model)
    assert exc_info.value.status_code == 422


def test_agent_daily_memory_zero_is_a_supported_off_switch():
    assert AgentUpdate(daily_memory_load_days=0).daily_memory_load_days == 0
    with pytest.raises(ValidationError):
        AgentUpdate(daily_memory_load_days=31)
