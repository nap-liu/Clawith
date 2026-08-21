"""Regression tests for the default-off OKR retirement boundary."""

import uuid
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.core.okr_feature import (
    OKR_SYSTEM_TRIGGER_NAMES,
    OKR_TOOL_NAMES,
    configure_retired_okr_agent_ids,
    is_retired_okr_agent,
    is_retired_okr_tool,
    is_retired_okr_trigger,
    okr_feature_enabled,
    partition_retired_okr_triggers,
    refresh_retired_okr_agent_ids,
)
from app.models.trigger import AgentTrigger
from app.services.agent_tools import execute_tool
from app.services.okr_agent_hook import hook_new_agent, hook_new_org_member
from app.services.tool_seeder import (
    BUILTIN_TOOLS,
    builtin_tool_enabled,
    should_sync_builtin_default,
)
from app.services.trigger_runtime.evaluator import (
    evaluate_trigger,
    handle_okr_collection_trigger,
    handle_okr_report_trigger,
)


@pytest.fixture(autouse=True)
def _reset_retired_agent_identity_snapshot():
    configure_retired_okr_agent_ids(())
    yield
    configure_retired_okr_agent_ids(())


def _trigger(name: str, *, agent_id: uuid.UUID | None = None) -> AgentTrigger:
    return AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        name=name,
        type="cron",
        config={"expr": "* * * * *"},
        is_enabled=True,
        created_at=datetime.now(UTC),
        fire_count=0,
    )


def test_feature_is_default_off_with_complete_retirement_sets():
    assert okr_feature_enabled() is False
    assert OKR_TOOL_NAMES == {
        "get_okr",
        "get_my_okr",
        "update_kr_progress",
        "update_kr_content",
        "collect_okr_progress",
        "generate_okr_report",
        "get_okr_settings",
        "create_objective",
        "create_key_result",
        "update_objective",
        "update_any_kr_progress",
        "generate_monthly_okr_report",
        "upsert_member_daily_report",
    }
    assert OKR_SYSTEM_TRIGGER_NAMES == {
        "daily_okr_collection",
        "daily_okr_report",
        "weekly_okr_report",
        "monthly_okr_report",
        "biweekly_okr_checkin",
    }


def test_all_seeded_okr_tools_are_forced_disabled_and_non_default():
    seeds = [tool for tool in BUILTIN_TOOLS if tool["name"] in OKR_TOOL_NAMES]
    assert {tool["name"] for tool in seeds} == OKR_TOOL_NAMES
    assert all(builtin_tool_enabled(tool) is False for tool in seeds)
    assert all(should_sync_builtin_default(tool) is True for tool in seeds)


@pytest.mark.asyncio
async def test_stale_enabled_tool_cannot_reach_dispatch():
    result = await execute_tool(
        "update_objective",
        {"objective_id": str(uuid.uuid4()), "title": "must not run"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert result == "This tool is unavailable."


@pytest.mark.asyncio
async def test_enabled_system_triggers_do_not_evaluate_or_execute():
    now = datetime.now(UTC)
    retired_agent_id = uuid.uuid4()
    configure_retired_okr_agent_ids((retired_agent_id,))
    for name in OKR_SYSTEM_TRIGGER_NAMES:
        trigger = _trigger(name, agent_id=retired_agent_id)
        assert is_retired_okr_trigger(name, retired_agent_id) is True
        assert await evaluate_trigger(trigger, now) is False

    assert await handle_okr_collection_trigger(
        _trigger("daily_okr_collection", agent_id=retired_agent_id), now
    ) is True
    assert await handle_okr_report_trigger(
        _trigger("daily_okr_report", agent_id=retired_agent_id), now
    ) is True


@pytest.mark.asyncio
async def test_same_named_trigger_on_ordinary_agent_remains_active():
    trigger = _trigger("daily_okr_report")
    trigger.type = "interval"
    trigger.config = {"minutes": 0}
    assert is_retired_okr_trigger(trigger.name, trigger.agent_id) is False
    assert await evaluate_trigger(trigger, datetime.now(UTC)) is True


@pytest.mark.asyncio
async def test_relationship_hooks_fail_fast_without_touching_database():
    class NoDatabaseCalls:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("retired hook touched the database")

    db = NoDatabaseCalls()
    await hook_new_org_member(db, uuid.uuid4(), uuid.uuid4())
    await hook_new_agent(db, uuid.uuid4(), uuid.uuid4())


def test_explicit_enable_restores_gate_semantics(monkeypatch):
    monkeypatch.setattr(get_settings(), "OKR_FEATURE_ENABLED", True)
    assert okr_feature_enabled() is True
    assert is_retired_okr_tool("update_objective") is False
    assert is_retired_okr_trigger("daily_okr_report") is False


@pytest.mark.asyncio
async def test_retired_agent_identity_is_fail_closed_without_taxing_ordinary_agents():
    class NoDatabaseCalls:
        async def scalar(self, *_args, **_kwargs):
            raise AssertionError("ordinary or named Agent unexpectedly queried settings")

    ordinary = type(
        "AgentDouble",
        (),
        {"id": uuid.uuid4(), "name": "General Agent", "is_system": False},
    )()
    named_system = type(
        "AgentDouble",
        (),
        {"id": uuid.uuid4(), "name": "OKR Agent", "is_system": True},
    )()
    ordinary_same_name = type(
        "AgentDouble",
        (),
        {"id": uuid.uuid4(), "name": "OKR Agent", "is_system": False},
    )()

    assert await is_retired_okr_agent(NoDatabaseCalls(), ordinary) is False
    assert await is_retired_okr_agent(NoDatabaseCalls(), ordinary_same_name) is False
    assert await is_retired_okr_agent(NoDatabaseCalls(), named_system) is True


@pytest.mark.asyncio
async def test_renamed_retired_system_agent_uses_canonical_settings_link():
    linked_tenant_id = uuid.uuid4()

    class CanonicalLinkDatabase:
        async def scalar(self, *_args, **_kwargs):
            return linked_tenant_id

    renamed_system = type(
        "AgentDouble",
        (),
        {"id": uuid.uuid4(), "name": "Renamed System Agent", "is_system": True},
    )()

    assert await is_retired_okr_agent(CanonicalLinkDatabase(), renamed_system) is True


@pytest.mark.asyncio
async def test_historical_non_system_flag_still_uses_startup_identity_snapshot():
    retired_id = uuid.uuid4()
    configure_retired_okr_agent_ids((retired_id,))

    class NoDatabaseCalls:
        async def scalar(self, *_args, **_kwargs):
            raise AssertionError("identity snapshot unexpectedly queried settings")

    historical_agent = type(
        "AgentDouble",
        (),
        {"id": retired_id, "name": "Renamed Agent", "is_system": False},
    )()

    assert await is_retired_okr_agent(NoDatabaseCalls(), historical_agent) is True


@pytest.mark.asyncio
async def test_startup_identity_snapshot_unions_settings_and_legacy_ids():
    settings_id = uuid.uuid4()
    legacy_id = uuid.uuid4()

    class ScalarRows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def scalars(self):
            return self

    class SnapshotDatabase:
        def __init__(self):
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return ScalarRows([settings_id] if self.calls == 1 else [legacy_id])

    snapshot = await refresh_retired_okr_agent_ids(SnapshotDatabase())

    assert snapshot == {settings_id, legacy_id}
    assert is_retired_okr_trigger("daily_okr_report", settings_id) is True
    assert is_retired_okr_trigger("daily_okr_report", legacy_id) is True


def test_mixed_trigger_batch_keeps_unrelated_trigger_active():
    retired_agent_id = uuid.uuid4()
    configure_retired_okr_agent_ids((retired_agent_id,))
    retired = _trigger("daily_okr_report", agent_id=retired_agent_id)
    active = _trigger("ordinary_schedule", agent_id=retired_agent_id)

    retired_batch, active_batch = partition_retired_okr_triggers([retired, active])

    assert retired_batch == [retired]
    assert active_batch == [active]
