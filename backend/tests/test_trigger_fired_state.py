"""Unit tests for the single source of truth of 'a trigger just fired' state.

Regression guard for the legacy-webhook runaway: the lease/execution path
(mark_base_triggers_fired) must clear `_webhook_pending` for legacy webhooks,
or `_evaluate_trigger` keeps re-firing them every cooldown forever.
"""
from datetime import datetime, timezone

from app.models.trigger import AgentTrigger
from app.services.trigger_runtime.executions import apply_base_trigger_fired_state

NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _trig(**kw):
    base = dict(type="cron", config={}, fire_count=0, max_fires=None, is_enabled=True)
    base.update(kw)
    return AgentTrigger(**base)


def test_increments_fire_count_and_sets_last_fired():
    t = _trig(fire_count=5)
    apply_base_trigger_fired_state(t, NOW)
    assert t.fire_count == 6
    assert t.last_fired_at == NOW


def test_fire_count_none_treated_as_zero():
    t = _trig(fire_count=None)
    apply_base_trigger_fired_state(t, NOW)
    assert t.fire_count == 1


def test_once_trigger_auto_disabled():
    t = _trig(type="once")
    apply_base_trigger_fired_state(t, NOW)
    assert t.is_enabled is False


def test_max_fires_reached_auto_disabled():
    t = _trig(type="cron", fire_count=2, max_fires=3)
    apply_base_trigger_fired_state(t, NOW)
    assert t.fire_count == 3
    assert t.is_enabled is False


def test_legacy_webhook_pending_cleared():
    # THE regression: a legacy webhook must have _webhook_pending cleared on fire
    # so the daemon stops re-firing it every cooldown.
    t = _trig(type="webhook", config={"_webhook_pending": True, "_webhook_payload": "hi", "token": "abc"})
    apply_base_trigger_fired_state(t, NOW)
    assert t.config["_webhook_pending"] is False
    assert t.config["_webhook_payload"] is None
    assert t.config["token"] == "abc"  # unrelated keys preserved


def test_legacy_webhook_is_default_mode():
    # webhook with no explicit webhook_mode is "legacy" → still cleared.
    t = _trig(type="webhook", config={"_webhook_pending": True})
    apply_base_trigger_fired_state(t, NOW)
    assert t.config["_webhook_pending"] is False


def test_queue_merge_webhook_pending_untouched():
    # queue/merge webhooks use a different mechanism (_webhook_queue/_webhook_active);
    # this function must NOT touch their pending/payload.
    for mode in ("queue", "merge"):
        t = _trig(type="webhook", config={"webhook_mode": mode, "_webhook_queue": ["a"], "_webhook_active": True})
        apply_base_trigger_fired_state(t, NOW)
        assert t.config.get("_webhook_queue") == ["a"]
        assert t.config.get("_webhook_active") is True
        assert "_webhook_pending" not in t.config


def test_non_webhook_config_untouched():
    t = _trig(type="cron", config={"expr": "0 8 * * *"})
    apply_base_trigger_fired_state(t, NOW)
    assert t.config == {"expr": "0 8 * * *"}
