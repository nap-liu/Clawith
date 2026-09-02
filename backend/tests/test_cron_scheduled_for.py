"""Regression coverage for timezone-aware cron execution identity."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.chat_session import ChatSession  # noqa: F401 - register FK target
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import Identity, User
from app.services.trigger_runtime.cron_schedule import (
    compute_next_cron_occurrence,
    compute_next_trigger_cron_occurrence,
    format_cron_timing_context,
)
from app.services.trigger_runtime.dispatch import (
    claim_ready_trigger_invocations,
    enqueue_due_trigger,
)
from app.services.trigger_runtime.executions import build_execution_runtime_trigger
from app.services.trigger_runtime.keys import build_scheduled_execution_key
from app.services.trigger_runtime.queue import enqueue_trigger_execution


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


def _cron_trigger(
    *,
    trigger_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    config: dict | None = None,
    created_at: datetime | None = None,
    last_fired_at: datetime | None = None,
) -> AgentTrigger:
    return AgentTrigger(
        id=trigger_id or uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        name=f"cron-{uuid.uuid4().hex[:8]}",
        type="cron",
        config=config or {"expr": "0 18 * * *", "timezone": "Asia/Shanghai"},
        reason="scheduled work",
        is_enabled=True,
        fire_count=0,
        cooldown_seconds=60,
        created_at=created_at or datetime(2026, 7, 22, 1, 39, 44, tzinfo=timezone.utc),
        last_fired_at=last_fired_at,
    )


def test_consecutive_shanghai_occurrences_have_distinct_canonical_keys():
    trigger = _cron_trigger()
    first = compute_next_cron_occurrence(
        expr="0 18 * * *",
        base=trigger.created_at,
        timezone_name="Asia/Shanghai",
    )
    first_key = build_scheduled_execution_key(
        trigger,
        first.scheduled_for,
        scheduled_for=first.scheduled_for,
    )

    trigger.last_fired_at = datetime(2026, 7, 22, 10, 0, 12, tzinfo=timezone.utc)
    second = compute_next_cron_occurrence(
        expr="0 18 * * *",
        base=trigger.last_fired_at,
        timezone_name="Asia/Shanghai",
    )
    second_key = build_scheduled_execution_key(
        trigger,
        second.scheduled_for,
        scheduled_for=second.scheduled_for,
    )

    assert first.scheduled_for == datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    assert second.scheduled_for == datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    assert first_key.endswith("2026-07-22T10:00:00+00:00")
    assert second_key.endswith("2026-07-23T10:00:00+00:00")
    assert first_key != second_key


def test_same_occurrence_has_stable_key_and_cron_requires_it():
    trigger = _cron_trigger()
    scheduled_for = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)

    first = build_scheduled_execution_key(
        trigger,
        scheduled_for,
        scheduled_for=scheduled_for,
    )
    second = build_scheduled_execution_key(
        trigger,
        scheduled_for + timedelta(seconds=15),
        scheduled_for=scheduled_for,
    )

    assert first == second
    with pytest.raises(ValueError, match="require scheduled_for"):
        build_scheduled_execution_key(trigger, scheduled_for)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_scheduled_execution_key(
            trigger,
            scheduled_for,
            scheduled_for=scheduled_for.replace(tzinfo=None),
        )


async def test_explicit_trigger_timezone_wins_without_agent_lookup():
    trigger = _cron_trigger(
        config={"expr": "0 18 * * *", "timezone": "Asia/Tokyo"},
    )
    with patch(
        "app.services.trigger_runtime.cron_schedule.get_agent_timezone",
        new_callable=AsyncMock,
    ) as timezone_lookup:
        occurrence = await compute_next_trigger_cron_occurrence(trigger)

    timezone_lookup.assert_not_awaited()
    assert occurrence.timezone_name == "Asia/Tokyo"
    assert occurrence.scheduled_for == datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


async def test_trigger_occurrence_applies_configured_rollout_boundary():
    trigger = _cron_trigger(
        last_fired_at=datetime(2026, 7, 22, 10, 0, 12, tzinfo=timezone.utc),
    )
    with patch(
        "app.services.trigger_runtime.cron_schedule.get_settings",
        return_value=SimpleNamespace(
            CRON_OCCURRENCE_NOT_BEFORE=datetime(
                2026,
                7,
                24,
                3,
                0,
                tzinfo=timezone.utc,
            )
        ),
    ):
        occurrence = await compute_next_trigger_cron_occurrence(trigger)

    assert occurrence.scheduled_for == datetime(
        2026,
        7,
        24,
        10,
        0,
        tzinfo=timezone.utc,
    )


def test_invalid_timezone_falls_back_to_utc():
    occurrence = compute_next_cron_occurrence(
        expr="0 18 * * *",
        base=datetime(2026, 7, 22, 1, 39, tzinfo=timezone.utc),
        timezone_name="Invalid/Timezone",
    )

    assert occurrence.timezone_name == "UTC"
    assert occurrence.scheduled_for == datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)
    assert occurrence.local_scheduled_for.utcoffset() == timedelta(0)


def test_rollout_boundary_skips_historical_occurrences_without_mutating_trigger():
    trigger = _cron_trigger(
        last_fired_at=datetime(2026, 7, 22, 10, 0, 12, tzinfo=timezone.utc),
    )
    original_last_fired_at = trigger.last_fired_at

    occurrence = compute_next_cron_occurrence(
        expr="0 18 * * *",
        base=trigger.last_fired_at,
        timezone_name="Asia/Shanghai",
        not_before=datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc),
    )

    assert occurrence.scheduled_for == datetime(
        2026,
        7,
        24,
        10,
        0,
        tzinfo=timezone.utc,
    )
    assert trigger.last_fired_at == original_last_fired_at


def test_rollout_boundary_includes_occurrence_exactly_at_boundary():
    occurrence = compute_next_cron_occurrence(
        expr="0 18 * * *",
        base=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
        not_before=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
    )

    assert occurrence.scheduled_for == datetime(
        2026,
        7,
        24,
        10,
        0,
        tzinfo=timezone.utc,
    )


def test_newer_last_fired_base_wins_over_rollout_boundary():
    occurrence = compute_next_cron_occurrence(
        expr="0 18 * * *",
        base=datetime(2026, 7, 25, 10, 0, 12, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
        not_before=datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc),
    )

    assert occurrence.scheduled_for == datetime(
        2026,
        7,
        26,
        10,
        0,
        tzinfo=timezone.utc,
    )


def test_rollout_boundary_must_be_timezone_aware():
    with pytest.raises(ValueError, match="boundary must be timezone-aware"):
        compute_next_cron_occurrence(
            expr="0 18 * * *",
            base=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
            timezone_name="Asia/Shanghai",
            not_before=datetime(2026, 7, 24, 3, 0),
        )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("", None),
        ("2026-07-24T03:00:00Z", datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc)),
        (
            "2026-07-24T11:00:00+08:00",
            datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_settings_normalizes_cron_rollout_boundary(raw_value, expected):
    settings = Settings(
        _env_file=None,
        CRON_OCCURRENCE_NOT_BEFORE=raw_value,
    )

    assert settings.CRON_OCCURRENCE_NOT_BEFORE == expected


def test_settings_rejects_naive_cron_rollout_boundary():
    with pytest.raises(ValidationError, match="must include a timezone"):
        Settings(
            _env_file=None,
            CRON_OCCURRENCE_NOT_BEFORE="2026-07-24T03:00:00",
        )


def test_dst_occurrence_is_aware_and_stable():
    occurrence = compute_next_cron_occurrence(
        expr="30 2 * * *",
        base=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
    )

    assert occurrence.scheduled_for.tzinfo is timezone.utc
    assert occurrence.local_scheduled_for.tzinfo is not None
    repeated = compute_next_cron_occurrence(
        expr="30 2 * * *",
        base=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
    )
    assert repeated == occurrence


def test_timing_context_is_automatic_and_business_neutral():
    context = format_cron_timing_context(
        {
            "_scheduled_for": "2026-07-23T10:00:00+00:00",
            "_scheduled_timezone": "Asia/Shanghai",
        },
        datetime(2026, 7, 23, 10, 15, tzinfo=timezone.utc),
    )

    assert "2026-07-23T18:00:00+08:00" in context
    assert "Current execution time" in context
    assert "Delay seconds: 900" in context
    assert "anchor relative calendar terms" in context
    assert "scheduled occurrence" in context
    assert "current execution time for live state" in context
    assert "Explicit time ranges in the task take precedence" in context
    assert "complaint" not in context.lower()
    assert "24 hours" not in context.lower()


async def test_non_workday_uses_scheduled_date_for_delayed_run():
    from app.services.trigger_daemon import _evaluate_trigger

    trigger = _cron_trigger(
        last_fired_at=datetime(2026, 7, 22, 10, 0, 12, tzinfo=timezone.utc),
    )
    occurrence = await compute_next_trigger_cron_occurrence(trigger)
    delayed_now = datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc)

    with (
        patch(
            "app.services.trigger_daemon._should_skip_non_workday",
            new_callable=AsyncMock,
            return_value=True,
        ) as should_skip,
        patch(
            "app.services.trigger_daemon._mark_trigger_skipped",
            new_callable=AsyncMock,
        ) as mark_skipped,
    ):
        assert not await _evaluate_trigger(
            trigger,
            delayed_now,
            cron_occurrence=occurrence,
        )

    scheduled_local = should_skip.await_args.args[1]
    assert scheduled_local.isoformat() == "2026-07-23T18:00:00+08:00"
    mark_skipped.assert_awaited_once_with(trigger.id, delayed_now)


async def _seed_persisted_cron() -> tuple[AgentTrigger, dict[str, uuid.UUID]]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(
            name=f"Cron Tenant {suffix}",
            slug=f"cron-{suffix}",
            timezone="Asia/Shanghai",
        )
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"cron_{suffix}",
            email=f"cron_{suffix}@example.com",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name=f"Cron User {suffix}",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Cron Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            agent_type="native",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = _cron_trigger(
            agent_id=agent.id,
            config={"expr": "0 18 * * *"},
            created_at=datetime(2026, 7, 22, 1, 39, 44, tzinfo=timezone.utc),
        )
        trigger.created_by_user_id = user.id
        trigger.execution_user_id = user.id
        db.add(trigger)
        await db.commit()
        await db.refresh(trigger)
        db.expunge(trigger)
        return trigger, {
            "tenant": tenant.id,
            "identity": identity.id,
            "user": user.id,
            "agent": agent.id,
        }


async def _cleanup_seeded_cron(ids: dict[str, uuid.UUID]) -> None:
    async with async_session() as db:
        await db.execute(delete(Agent).where(Agent.id == ids["agent"]))
        await db.execute(delete(User).where(User.id == ids["user"]))
        await db.execute(delete(Identity).where(Identity.id == ids["identity"]))
        await db.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))
        await db.commit()


async def test_concurrent_enqueue_uses_schedule_for_record_key_and_context():
    trigger, ids = await _seed_persisted_cron()
    occurrence = await compute_next_trigger_cron_occurrence(trigger)
    scheduled_for = occurrence.scheduled_for
    observed_at = scheduled_for + timedelta(hours=15)
    try:
        assert occurrence.timezone_name == "Asia/Shanghai"
        assert scheduled_for == datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
        await asyncio.gather(
            enqueue_due_trigger(
                trigger,
                observed_at,
                scheduled_for=scheduled_for,
                scheduled_timezone=occurrence.timezone_name,
            ),
            enqueue_due_trigger(
                trigger,
                observed_at + timedelta(seconds=15),
                scheduled_for=scheduled_for,
                scheduled_timezone=occurrence.timezone_name,
            ),
        )

        async with async_session() as db:
            executions = list(
                (
                    await db.execute(
                        select(TriggerExecution).where(
                            TriggerExecution.trigger_id == trigger.id
                        )
                    )
                ).scalars()
            )

        assert len(executions) == 1
        execution = executions[0]
        assert execution.scheduled_at == scheduled_for
        assert execution.idempotency_key.endswith("2026-07-22T10:00:00+00:00")
        assert execution.payload["_scheduled_for"] == "2026-07-22T10:00:00+00:00"
        assert execution.payload["_scheduled_timezone"] == "Asia/Shanghai"
        assert execution.execution_user_id == ids["user"]
        runtime_trigger = build_execution_runtime_trigger(trigger, execution)
        assert runtime_trigger.execution_user_id == ids["user"]
        assert runtime_trigger.config["_execution_user_id"] == str(ids["user"])
        assert runtime_trigger.config["_scheduled_for"] == "2026-07-22T10:00:00+00:00"
        assert "Schedule timing" in format_cron_timing_context(
            runtime_trigger.config,
            observed_at,
        )
    finally:
        await _cleanup_seeded_cron(ids)


async def test_recurring_trigger_enqueues_next_occurrence_while_prior_run_is_active():
    trigger, ids = await _seed_persisted_cron()
    try:
        async with async_session() as db:
            stored_trigger = await db.get(AgentTrigger, trigger.id)
            stored_trigger.type = "interval"
            stored_trigger.config = {"minutes": 5}
            await db.commit()
            await db.refresh(stored_trigger)
            db.expunge(stored_trigger)

        await enqueue_due_trigger(stored_trigger, stored_trigger.created_at)
        await claim_ready_trigger_invocations(stored_trigger.created_at)

        async with async_session() as db:
            stored_trigger = await db.get(AgentTrigger, trigger.id)
            await db.refresh(stored_trigger)
            db.expunge(stored_trigger)

        await enqueue_due_trigger(stored_trigger, stored_trigger.last_fired_at + timedelta(minutes=5))
        async with async_session() as db:
            executions = list(
                (
                    await db.execute(
                        select(TriggerExecution).where(
                            TriggerExecution.trigger_id == trigger.id
                        )
                    )
                ).scalars()
            )
        assert len(executions) == 2
        assert {execution.status for execution in executions} == {"pending", "processing"}
    finally:
        await _cleanup_seeded_cron(ids)


async def test_different_recurring_triggers_for_one_agent_dispatch_independently():
    trigger, ids = await _seed_persisted_cron()
    sibling = _cron_trigger(
        agent_id=ids["agent"],
        config={"expr": "5 18 * * *", "timezone": "Asia/Shanghai"},
    )
    sibling.created_by_user_id = ids["user"]
    sibling.execution_user_id = ids["user"]
    async with async_session() as db:
        db.add(sibling)
        await db.commit()
        await db.refresh(sibling)
        db.expunge(sibling)

    scheduled_for = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    try:
        await asyncio.gather(
            enqueue_due_trigger(
                trigger,
                scheduled_for,
                scheduled_for=scheduled_for,
                scheduled_timezone="Asia/Shanghai",
            ),
            enqueue_due_trigger(
                sibling,
                scheduled_for + timedelta(minutes=5),
                scheduled_for=scheduled_for + timedelta(minutes=5),
                scheduled_timezone="Asia/Shanghai",
            ),
        )
        async with async_session() as db:
            executions = list(
                (
                    await db.execute(
                        select(TriggerExecution).where(
                            TriggerExecution.agent_id == ids["agent"]
                        )
                    )
                ).scalars()
            )
        assert len(executions) == 2
        assert {execution.trigger_id for execution in executions} == {
            trigger.id,
            sibling.id,
        }

        invocations, _force_invoke = await claim_ready_trigger_invocations(
            scheduled_for + timedelta(minutes=5)
        )
        agent_invocations = [
            claimed
            for key, claimed in invocations.items()
            if key[0] == ids["agent"]
        ]
        assert len(agent_invocations) == 2
        assert all(len(claimed) == 1 for claimed in agent_invocations)
        assert {claimed[0].id for claimed in agent_invocations} == {
            trigger.id,
            sibling.id,
        }
    finally:
        await _cleanup_seeded_cron(ids)


async def test_poll_enqueue_is_not_dropped_by_an_active_scheduled_run():
    trigger, ids = await _seed_persisted_cron()
    scheduled_for = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    poll_trigger = AgentTrigger(
        agent_id=ids["agent"],
        created_by_user_id=ids["user"],
        execution_user_id=ids["user"],
        name=f"poll-{uuid.uuid4().hex[:8]}",
        type="poll",
        config={"_last_value": "changed-value"},
        reason="changed remote value",
        is_enabled=True,
    )
    try:
        async with async_session() as db:
            db.add(poll_trigger)
            await db.commit()
            await db.refresh(poll_trigger)
            db.expunge(poll_trigger)
        await enqueue_due_trigger(
            trigger,
            scheduled_for,
            scheduled_for=scheduled_for,
            scheduled_timezone="Asia/Shanghai",
        )
        async with async_session() as db:
            await db.execute(
                update(TriggerExecution)
                .where(TriggerExecution.trigger_id == trigger.id)
                .values(status="processing")
            )
            await db.commit()

        await enqueue_due_trigger(poll_trigger, scheduled_for)
        async with async_session() as db:
            poll_execution = (
                await db.execute(
                    select(TriggerExecution).where(
                        TriggerExecution.trigger_id == poll_trigger.id
                    )
                )
            ).scalar_one_or_none()
        assert poll_execution is not None
        assert poll_execution.payload == {}
    finally:
        await _cleanup_seeded_cron(ids)


async def test_unrelated_integrity_error_is_not_treated_as_deduplication():
    trigger, ids = await _seed_persisted_cron()
    invalid_agent_trigger = _cron_trigger(
        trigger_id=trigger.id,
        agent_id=uuid.uuid4(),
    )
    try:
        async with async_session() as db:
            with pytest.raises(IntegrityError):
                await enqueue_trigger_execution(
                    db,
                    trigger=invalid_agent_trigger,
                    source="cron",
                    idempotency_key=f"bad-fk-{uuid.uuid4()}",
                    scheduled_at=datetime.now(timezone.utc),
                )
    finally:
        await _cleanup_seeded_cron(ids)
