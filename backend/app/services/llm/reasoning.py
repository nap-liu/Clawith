"""Provider-neutral reasoning effort validation and protocol adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ENABLED_REASONING_EFFORTS = REASONING_EFFORTS[1:]
REASONING_EFFORT_LABELS_ZH = {
    "none": "关闭",
    "minimal": "极速",
    "low": "快速",
    "medium": "均衡",
    "high": "深入",
    "xhigh": "强化",
    "max": "极致",
}


class ReasoningConfigurationError(ValueError):
    """The selected model cannot honor an explicit reasoning choice."""


@dataclass(frozen=True, slots=True)
class ReasoningCapability:
    profile: str
    supported_efforts: tuple[str, ...]
    max_thinking_budget: int | None = None
    effort_map: tuple[tuple[str, str], ...] = ()

    @property
    def can_disable(self) -> bool:
        return "none" in self.supported_efforts

    def mapped_effort(self, effort: str) -> str:
        return dict(self.effort_map).get(effort, effort)


def validate_reasoning_effort(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in REASONING_EFFORTS:
        choices = ", ".join(REASONING_EFFORTS)
        raise ReasoningConfigurationError(f"reasoning_effort must be one of: {choices}")
    return normalized


def reasoning_effort_display_name(value: object | None) -> str:
    normalized = validate_reasoning_effort(value)
    if normalized is None:
        return "自动"
    return f"{REASONING_EFFORT_LABELS_ZH[normalized]} ({normalized})"


def _name(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def is_bailian_endpoint(base_url: str | None) -> bool:
    """Recognize both shared and workspace-scoped Alibaba model endpoints."""
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    return host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    } or host.endswith(".maas.aliyuncs.com")


def is_tokenhub_endpoint(base_url: str | None) -> bool:
    return (urlsplit(str(base_url or "")).hostname or "").lower() in {
        "tokenhub.tencentmaas.com", "tokenhub-intl.tencentmaas.com",
    }


def _budget_cap(model: str) -> int:
    if "qwen3.5" in model or "qwen3-5" in model or "qwen3.6" in model or "qwen3-6" in model:
        return 81_920
    if "qwen3.7" in model or "qwen3-7" in model or "qwen3.8" in model or "qwen3-8" in model:
        return 262_144
    return 65_536


def _collapsed_low_high_max(always_on: bool = False) -> ReasoningCapability:
    efforts = ENABLED_REASONING_EFFORTS if always_on else REASONING_EFFORTS
    return ReasoningCapability(
        "always_on_effort" if always_on else "native_effort",
        efforts,
        effort_map=(
            ("minimal", "low"),
            ("low", "low"),
            ("medium", "high"),
            ("high", "high"),
            ("xhigh", "max"),
            ("max", "max"),
        ),
    )


def resolve_reasoning_capability(
    *, provider: str | None, model: str | None, base_url: str | None
) -> ReasoningCapability:
    """Derive capability from protocol endpoint and exact model family."""
    provider_name = _name(provider)
    model_name = _name(model)
    endpoint = _name(base_url)
    dashscope = is_bailian_endpoint(base_url)

    if is_tokenhub_endpoint(base_url):
        if model_name == "hy3":
            return ReasoningCapability(
                "native_effort", REASONING_EFFORTS,
                effort_map=(("minimal", "low"),
                            ("xhigh", "high"), ("max", "high")),
            )
        if model_name == "deepseek-v4-flash":
            return _collapsed_low_high_max()
        if model_name == "glm-5.3":
            return _collapsed_low_high_max(always_on=True)
        if model_name == "kimi-k3":
            return ReasoningCapability(
                "always_on_effort", ENABLED_REASONING_EFFORTS,
                effort_map=tuple((effort, "max") for effort in ENABLED_REASONING_EFFORTS),
            )

    # Legacy dedicated-reasoning models accept effort-shaped fields but ignore
    # them in live provider responses. Treat them as fixed so the UI never
    # promises a working off switch or artificial strength levels.
    if model_name in {"deepseek-r1", "qwq-plus"}:
        return ReasoningCapability("fixed", ())
    if model_name in {"deepseek-v3", "qwen-max"}:
        return ReasoningCapability("unsupported", ())

    if dashscope:
        if model_name == "minimax/minimax-m3":
            return ReasoningCapability("adaptive_toggle", REASONING_EFFORTS)
        if model_name == "stepfun/step-3.7-flash":
            return ReasoningCapability(
                "toggle_effort",
                REASONING_EFFORTS,
                effort_map=(("minimal", "low"), ("xhigh", "high"), ("max", "high")),
            )
        if "qwen3.8" in model_name or "qwen3-8" in model_name:
            thinking_only = "2.4t" in model_name
            efforts = ENABLED_REASONING_EFFORTS if thinking_only else REASONING_EFFORTS
            return ReasoningCapability(
                "always_on_effort" if thinking_only else "native_effort",
                efforts,
                effort_map=(
                    ("minimal", "low"),
                    ("low", "low"),
                    ("medium", "medium"),
                    ("high", "xhigh"),
                    ("xhigh", "xhigh"),
                    ("max", "xhigh"),
                ),
            )
        if ("qwen3.7" in model_name or "qwen3-7" in model_name) and "max" in model_name:
            thinking_only = "preview" in model_name or "2026-05-17" in model_name
            efforts = ENABLED_REASONING_EFFORTS if thinking_only else REASONING_EFFORTS
            profile = "always_on_budget" if thinking_only else "budget"
            return ReasoningCapability(profile, efforts, _budget_cap(model_name))
        if any(version in model_name for version in ("qwen3.5", "qwen3-5", "qwen3.6", "qwen3-6", "qwen3.7", "qwen3-7")):
            if "plus" in model_name or "flash" in model_name:
                return ReasoningCapability("budget", REASONING_EFFORTS, _budget_cap(model_name))
        if "glm-5.2" in model_name or "glm5.2" in model_name:
            return ReasoningCapability("native_effort", REASONING_EFFORTS)
        if model_name in {"glm-5", "glm5", "glm-5.1", "glm5.1"}:
            return ReasoningCapability(
                "native_effort",
                REASONING_EFFORTS,
                effort_map=(("max", "xhigh"),),
            )
        if "glm-5.3" in model_name or "glm5.3" in model_name:
            return _collapsed_low_high_max(always_on=True)
        if "deepseek-v4" in model_name:
            return ReasoningCapability(
                "native_effort",
                REASONING_EFFORTS,
                effort_map=(
                    ("minimal", "high"),
                    ("low", "high"),
                    ("medium", "high"),
                    ("high", "high"),
                    ("xhigh", "max"),
                    ("max", "max"),
                ),
            )
        if "kimi-k3" in model_name:
            return _collapsed_low_high_max(always_on=True)
        if "kimi-k2.7" in model_name or "kimi-k2-7" in model_name:
            return ReasoningCapability("always_on_budget", ENABLED_REASONING_EFFORTS, _budget_cap(model_name))
        if "kimi-k2.5" in model_name or "kimi-k2.6" in model_name:
            return ReasoningCapability("budget", REASONING_EFFORTS, _budget_cap(model_name))
        if "mimo" in model_name:
            return ReasoningCapability("toggle", REASONING_EFFORTS)
        if "minimax-m1" in model_name or "minimax-m2.5" in model_name:
            return ReasoningCapability("fixed", ())

    if provider_name in {"openai", "azure"} or "api.openai.com" in endpoint:
        return ReasoningCapability("native_effort", REASONING_EFFORTS)
    if provider_name == "anthropic":
        return ReasoningCapability("anthropic", REASONING_EFFORTS)
    if provider_name == "gemini":
        return ReasoningCapability("gemini", REASONING_EFFORTS)
    if provider_name in {"deepseek", "qwen", "zhipu", "kimi"}:
        return ReasoningCapability("native_effort", REASONING_EFFORTS)
    return ReasoningCapability("unsupported", ())


def capability_metadata(*, provider: str | None, model: str | None, base_url: str | None) -> dict[str, Any]:
    capability = resolve_reasoning_capability(provider=provider, model=model, base_url=base_url)
    return {
        "reasoning_profile": capability.profile,
        "reasoning_efforts": list(capability.supported_efforts),
        "reasoning_can_disable": capability.can_disable,
    }


def _assert_supported(capability: ReasoningCapability, effort: str, model: str | None) -> None:
    if effort not in capability.supported_efforts:
        raise ReasoningConfigurationError(
            f"model {model or '<unknown>'} does not support reasoning_effort={effort}"
        )


def _thinking_budget(effort: str, cap: int) -> int:
    budgets = {
        "minimal": 1_024,
        "low": 4_096,
        "medium": 16_384,
        "high": 32_768,
        "xhigh": 65_536,
        "max": cap,
    }
    return min(budgets[effort], cap)


def openai_chat_reasoning_options(
    *,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    effort: object | None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    normalized = validate_reasoning_effort(effort)
    if normalized is None:
        return {}
    capability = resolve_reasoning_capability(provider=provider, model=model, base_url=base_url)
    _assert_supported(capability, normalized, model)
    if is_tokenhub_endpoint(base_url) and _name(model) == "hy3" and normalized == "none":
        return {"reasoning_effort": "no_think"}
    if is_tokenhub_endpoint(base_url) and _name(model) == "deepseek-v4-flash":
        if normalized == "none":
            return {"thinking": {"type": "disabled"}}
        return {
            "thinking": {"type": "enabled"},
            "reasoning_effort": capability.mapped_effort(normalized),
        }
    if capability.profile in {"budget", "always_on_budget"}:
        if normalized == "none":
            return {"enable_thinking": False}
        budget_cap = capability.max_thinking_budget or 65_536
        if max_output_tokens is not None:
            if max_output_tokens <= 1:
                raise ReasoningConfigurationError(
                    "max_output_tokens must exceed one token when reasoning is enabled"
                )
            budget_cap = min(budget_cap, max_output_tokens - 1)
        return {
            "enable_thinking": True,
            "thinking_budget": _thinking_budget(normalized, budget_cap),
        }
    if capability.profile == "toggle":
        return {"enable_thinking": normalized != "none"}
    if capability.profile == "adaptive_toggle":
        return {"thinking": {"type": "disabled" if normalized == "none" else "adaptive"}}
    if capability.profile == "toggle_effort":
        if normalized == "none":
            return {"enable_thinking": False}
        return {"enable_thinking": True, "reasoning_effort": capability.mapped_effort(normalized)}
    if normalized == "none" and is_bailian_endpoint(base_url):
        return {"enable_thinking": False}
    options = {"reasoning_effort": capability.mapped_effort(normalized)}
    if is_bailian_endpoint(base_url) and "qwen" in _name(model):
        options["enable_thinking"] = True
    return options


def openai_responses_reasoning_options(
    *,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    effort: object | None,
) -> dict[str, Any]:
    normalized = validate_reasoning_effort(effort)
    if normalized is None:
        return {}
    capability = resolve_reasoning_capability(provider=provider, model=model, base_url=base_url)
    _assert_supported(capability, normalized, model)
    return {"reasoning": {"effort": capability.mapped_effort(normalized)}}


def anthropic_reasoning_options(*, model: str | None, effort: object | None) -> dict[str, Any]:
    normalized = validate_reasoning_effort(effort)
    if normalized is None:
        return {}
    if normalized == "none":
        return {}
    mapped = {
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "max",
    }[normalized]
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": mapped}}


def gemini_reasoning_options(*, model: str | None, effort: object | None) -> dict[str, Any]:
    normalized = validate_reasoning_effort(effort)
    if normalized is None:
        return {}
    if normalized == "none":
        return {"thinkingConfig": {"thinkingBudget": 0}}
    levels = {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "high",
    }
    return {"thinkingConfig": {"thinkingLevel": levels[normalized]}}
