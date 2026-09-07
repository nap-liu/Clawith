from types import SimpleNamespace

from app.services.llm.context_budget import resolve_context_budget


def test_context_budget_uses_physical_window_times_configured_ratio():
    model = SimpleNamespace(
        context_window=200_000,
        context_usage_ratio=0.6,
        compact_trigger_ratio=0.85,
    )

    budget = resolve_context_budget(model, max_output_tokens=20_000)

    assert budget.physical_context_window == 200_000
    assert budget.effective_context_window == 120_000
    assert budget.input_capacity == 100_000
    assert budget.compaction_trigger_limit == 100_000
    assert budget.hard_input_limit == 100_000


def test_invalid_persisted_ratio_fails_closed():
    model = SimpleNamespace(
        context_window=200_000,
        context_usage_ratio=1.2,
        compact_trigger_ratio=0.85,
    )

    budget = resolve_context_budget(model, max_output_tokens=20_000)

    assert budget.configured is True
    assert budget.input_capacity == 0
