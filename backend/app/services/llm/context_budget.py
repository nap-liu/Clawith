"""Single source of truth for model context budgeting."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelContextBudget:
    physical_context_window: int
    context_usage_ratio: float
    effective_context_window: int
    max_output_tokens: int
    input_capacity: int
    compaction_trigger_limit: int
    hard_input_limit: int
    configured: bool


def resolve_context_budget(model, *, max_output_tokens: int) -> ModelContextBudget:
    """Resolve the single effective context budget used everywhere.

    ``context_window * context_usage_ratio`` is the final allowed total of
    input plus reserved output tokens.  The configured ratio is itself the
    model-specific safety control, so dispatch must not apply another hidden
    coefficient on top of it.

    Invalid persisted ratios fail closed. API and database validation normally
    prevent them, but a malformed row must never silently expand the window.
    """
    physical = int(getattr(model, "context_window", 0) or 0)
    ratio = float(getattr(model, "context_usage_ratio", 0.7) or 0.0)
    output = max(0, int(max_output_tokens or 0))
    configured = physical > 0
    valid_ratio = 0.1 <= ratio <= 1.0

    effective = math.floor(physical * ratio) if configured and valid_ratio else 0
    input_capacity = max(0, effective - output) if configured and valid_ratio else 0
    hard_limit = input_capacity
    return ModelContextBudget(
        physical_context_window=physical,
        context_usage_ratio=ratio,
        effective_context_window=effective,
        max_output_tokens=output,
        input_capacity=input_capacity,
        # The model-level usage ratio is the one normalized threshold.  Do not
        # silently multiply it by a second compaction coefficient: for a 1M
        # model at 70%, input + reserved output may actually use 700K.
        compaction_trigger_limit=max(0, hard_limit),
        hard_input_limit=max(0, hard_limit),
        configured=configured,
    )
