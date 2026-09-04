import pytest

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.services.chat_model_selection import _runtime_snapshot
from app.services.llm.client_anthropic import AnthropicClient
from app.services.llm.client_gemini import GeminiClient
from app.services.llm.client_openai_compatible import OpenAICompatibleClient
from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.client_registry import create_llm_client
from app.services.llm.client_shared import LLMMessage
from app.services.llm.reasoning import (
    ReasoningConfigurationError,
    reasoning_effort_display_name,
    resolve_reasoning_capability,
    validate_reasoning_effort,
)

DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MESSAGE = [LLMMessage(role="user", content="ok")]


def _model_with_reasoning(effort: str | None) -> LLMModel:
    return LLMModel(
        provider="qwen",
        model="qwen3.7-plus",
        api_key_encrypted="unused",
        reasoning_effort=effort,
    )


def test_reasoning_precedence_preserves_explicit_none():
    agent = Agent(name="Reasoning", reasoning_effort="high")
    snapshot = _runtime_snapshot(
        _model_with_reasoning("low"),
        agent=agent,
        override_reasoning_effort="none",
    )
    assert snapshot is not None
    assert snapshot.reasoning_effort == "none"


def test_reasoning_inherits_agent_then_model_default():
    model = _model_with_reasoning("low")
    agent_snapshot = _runtime_snapshot(model, agent=Agent(name="Agent", reasoning_effort="high"))
    model_snapshot = _runtime_snapshot(model, agent=Agent(name="Model"))
    assert agent_snapshot is not None and agent_snapshot.reasoning_effort == "high"
    assert model_snapshot is not None and model_snapshot.reasoning_effort == "low"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("NONE", "none"),
        (" minimal ", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "max"),
    ],
)
def test_reasoning_effort_validation(value, expected):
    assert validate_reasoning_effort(value) == expected


def test_reasoning_effort_rejects_unknown_level():
    with pytest.raises(ReasoningConfigurationError):
        validate_reasoning_effort("ultra")


def test_reasoning_effort_uses_product_facing_display_names():
    assert reasoning_effort_display_name(None) == "自动"
    assert reasoning_effort_display_name("minimal") == "极速 (minimal)"
    assert reasoning_effort_display_name("high") == "深入 (high)"
    assert reasoning_effort_display_name("max") == "极致 (max)"


@pytest.mark.parametrize(
    ("model", "profile", "can_disable"),
    [
        ("qwen3.8-max", "native_effort", True),
        ("qwen3.8-plus", "native_effort", True),
        ("qwen3.8-flash", "native_effort", True),
        ("qwen3.8-2.4t-a95b", "always_on_effort", False),
        ("qwen3.7-max", "budget", True),
        ("qwen3.7-max-2026-05-20", "budget", True),
        ("qwen3.7-max-preview", "always_on_budget", False),
        ("qwen3.7-max-2026-05-17", "always_on_budget", False),
        ("qwen3.7-plus", "budget", True),
        ("qwen3.7-flash", "budget", True),
        ("qwen3.6-plus", "budget", True),
        ("qwen3.6-flash", "budget", True),
        ("qwen3.5-plus", "budget", True),
        ("glm-5.2", "native_effort", True),
        ("glm-5.1", "native_effort", True),
        ("ZHIPU/GLM-5.3", "always_on_effort", False),
        ("deepseek-v4-flash", "native_effort", True),
        ("deepseek-v4-pro", "native_effort", True),
        ("kimi-k2.5", "budget", True),
        ("kimi-k2.6", "budget", True),
        ("kimi-k2.7-code", "always_on_budget", False),
        ("kimi-k3", "always_on_effort", False),
        ("mimo-v2.5-pro", "toggle", True),
        ("MiniMax-M2.5", "fixed", False),
        ("deepseek-r1", "fixed", False),
        ("deepseek-v3", "unsupported", False),
        ("qwen-max", "unsupported", False),
        ("qwen-plus", "unsupported", False),
        ("qwen-turbo", "unsupported", False),
        ("qwq-plus", "fixed", False),
    ],
)
def test_dashscope_production_model_capabilities(model, profile, can_disable):
    capability = resolve_reasoning_capability(
        provider="custom",
        model=model,
        base_url=DASHSCOPE,
    )
    assert capability.profile == profile
    assert capability.can_disable is can_disable


def test_qwen_native_effort_payload_collapses_to_supported_level():
    client = OpenAICompatibleClient("secret", DASHSCOPE, "qwen3.8-plus")
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="max")
    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == "xhigh"
    assert "thinking_budget" not in payload


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("ZHIPU/GLM-5.3", "medium", "high"),
        ("ZHIPU/GLM-5.3", "xhigh", "max"),
        ("kimi-k3", "medium", "high"),
        ("kimi-k3", "xhigh", "max"),
    ],
)
def test_low_high_max_models_use_official_effort_mapping(model, effort, expected):
    client = OpenAICompatibleClient("secret", DASHSCOPE, model)
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort=effort)
    assert payload["reasoning_effort"] == expected


@pytest.mark.parametrize("model", ["qwen3.7-max"])
def test_documented_hybrid_models_support_explicit_none(model):
    client = OpenAICompatibleClient("secret", DASHSCOPE, model)
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="none")
    assert payload["enable_thinking"] is False


def test_qwen_38_24t_rejects_none_matching_live_endpoint_contract():
    client = OpenAICompatibleClient("secret", DASHSCOPE, "qwen3.8-2.4t-a95b")
    with pytest.raises(ReasoningConfigurationError):
        client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="none")


@pytest.mark.parametrize("model", ["deepseek-r1", "qwq-plus"])
def test_legacy_always_thinking_models_reject_explicit_effort(model):
    capability = resolve_reasoning_capability(
        provider="qwen",
        model=model,
        base_url=DASHSCOPE,
    )
    assert capability.profile == "fixed"
    assert capability.supported_efforts == ()


def test_qwen_budget_payload_never_mixes_effort_and_budget():
    client = OpenAICompatibleClient("secret", DASHSCOPE, "qwen3.7-plus")
    payload = client._build_payload(MESSAGE, None, None, 20_000, reasoning_effort="medium")
    assert payload["enable_thinking"] is True
    assert payload["thinking_budget"] == 16_384
    assert "reasoning_effort" not in payload


def test_budget_is_clamped_below_output_ceiling():
    client = OpenAICompatibleClient("secret", DASHSCOPE, "qwen3.7-plus")
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="medium")
    assert payload["thinking_budget"] == 999


def test_explicit_none_disables_hybrid_thinking():
    client = OpenAICompatibleClient("secret", DASHSCOPE, "kimi-k2.6")
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="none")
    assert payload["enable_thinking"] is False
    assert "thinking_budget" not in payload


def test_factory_preserves_provider_for_direct_protocol_fallback():
    client = create_llm_client("deepseek", "secret", "deepseek-chat")
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="none")
    assert payload["reasoning_effort"] == "none"


def test_always_on_model_rejects_explicit_none_before_network():
    client = OpenAICompatibleClient("secret", DASHSCOPE, "kimi-k3")
    with pytest.raises(ReasoningConfigurationError):
        client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="none")


def test_openai_responses_uses_nested_reasoning_effort_and_omits_null_temperature():
    client = OpenAIResponsesClient("secret", "https://api.openai.com/v1", "gpt-5.6")
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="xhigh")
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert "temperature" not in payload


def test_anthropic_uses_adaptive_thinking_and_required_temperature():
    client = AnthropicClient("secret", model="claude-opus-4-6")
    payload = client._build_payload(MESSAGE, None, 0.2, 4000, reasoning_effort="high")
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert payload["temperature"] == 1.0


def test_gemini_none_uses_zero_thinking_budget():
    client = GeminiClient("secret", model="gemini-2.5-flash")
    payload = client._build_payload(MESSAGE, None, None, 1000, reasoning_effort="none")
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
