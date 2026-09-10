"""Serving platform is metadata, independent of model branding and protocol."""

import uuid
from datetime import datetime, timezone

import pytest

from app.api.enterprise_routes_models import _llm_model_out
from app.models.llm import LLMModel
from app.services.llm.client_registry import get_provider_manifest, resolve_api_protocol


@pytest.mark.parametrize(("provider", "endpoint", "platform"), [
    ("deepseek", "https://dashscope.aliyuncs.com/compatible-mode/v1", "bailian"),
    ("minimax", "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "bailian"),
    ("hunyuan", "https://tokenhub.tencentmaas.com/v1", "tokenhub"),
    ("tokenhub", "https://tokenhub.tencentmaas.com/v1", "tokenhub"),
    ("deepseek", "https://api.deepseek.com/v1", "deepseek"),
    ("custom", "https://dashscope.aliyuncs.com.example.org/v1", "custom"),
])
def test_api_projection_preserves_brand_and_connection(provider, endpoint, platform):
    model = LLMModel(
        id=uuid.uuid4(), provider=provider, model="deepseek-v4-flash", label="Shared name",
        base_url=endpoint, api_key_encrypted="private", api_protocol="openai_compatible",
        enabled=True, supports_vision=False, context_window=32000, context_usage_ratio=.7,
        purposes=["conversation"], input_modalities=["text"],
        keep_recent_turns=3, created_at=datetime.now(timezone.utc),
    )
    result = _llm_model_out(model).model_dump(mode="json")
    assert result["service_platform"] == platform
    assert result["provider"] == provider
    assert result["base_url"] == endpoint
    assert result["effective_api_protocol"] == "openai_compatible"
    assert "api_key_encrypted" not in result


def test_tokenhub_registry_prefers_responses_and_allows_explicit_chat():
    provider = next(row for row in get_provider_manifest() if row["provider"] == "tokenhub")
    assert provider["default_base_url"] == "https://tokenhub.tencentmaas.com/v1"
    assert resolve_api_protocol("tokenhub") == "openai_responses"
    assert resolve_api_protocol("tokenhub", "openai_compatible") == "openai_compatible"
