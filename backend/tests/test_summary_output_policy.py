"""Summary requests use ordinary model output settings without local length gates."""

from types import SimpleNamespace

import pytest

from app.services.llm.compactor import _summarize_via_llm, validate_summary
from tests.test_compactor_unit import _GOOD_SUMMARY


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_summary_and_repair_ignore_legacy_summary_token_cap(monkeypatch, repair):
    calls = []
    output = _GOOD_SUMMARY + "\nEvidence retained. " * 3000

    class Client:
        closed = False

        async def complete(self, messages, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=output, usage={"completion_tokens": 6000})

        async def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr("app.services.llm.create_llm_client", lambda **_: client)
    monkeypatch.setattr("app.services.llm.get_model_api_key", lambda _: "test-key")
    model = SimpleNamespace(
        provider="custom", model="test-model", base_url=None,
        max_output_tokens=65536, compact_summary_max_tokens=1,
    )
    summary, usage = await _summarize_via_llm(
        span_text="history", prior_summary=None, model=model,
        failed_summary="draft" if repair else None,
        failure_reason="missing_goals" if repair else None,
    )
    assert calls[0]["max_tokens"] == 65536
    assert summary == output
    assert usage == {"completion_tokens": 6000}
    assert client.closed


@pytest.mark.parametrize("emphasis", ["**", "__"])
def test_goal_field_emphasis_does_not_reject_valid_summary(emphasis):
    summary = _GOOD_SUMMARY
    for label in ("Active", "Achieved", "Not achieved / blocked"):
        summary = summary.replace(f"- {label}:", f"- {emphasis}{label}{emphasis}:")
    passed, reason, _ = validate_summary(
        summary=summary, original_text="", max_tokens=1,
    )
    assert passed, reason
