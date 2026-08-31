"""Background work follows its last user mutation and freezes active-run snapshots."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.models.chat_compaction  # noqa: F401 - registers ChatMessage FK target
import app.models.participant  # noqa: F401 - registers ChatSession FK target
from app.api.agents import get_agent_permission_members
from app.api.schedules import ScheduleUpdate, trigger_schedule, update_schedule
from app.api.tasks import update_task
from app.api.triggers import TriggerUpdate, list_agent_triggers, update_trigger
from app.database import async_session, engine
from app.models.agent import Agent, AgentPermission
from app.models.schedule import AgentSchedule
from app.models.audit import AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import Identity, User
from app.schemas.schemas import TaskUpdate

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


async def _user(db, tenant_id, role, suffix):
    identity = Identity(
        username=f"executor_{suffix}",
        email=f"executor_{suffix}@test.local",
        password_hash="test",
    )
    db.add(identity)
    await db.flush()
    user = User(
        identity_id=identity.id,
        tenant_id=tenant_id,
        display_name=suffix,
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def test_task_mutation_aligns_execution_user_to_current_user():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Executor {suffix}", slug=f"executor-{suffix}")
        db.add(tenant)
        await db.flush()
        admin = await _user(db, tenant.id, "platform_admin", f"admin_{suffix}")
        member = await _user(db, tenant.id, "member", f"member_{suffix}")
        candidate = await _user(db, tenant.id, "member", f"candidate_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=admin.id,
            name=f"Executor Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        task = Task(
            agent_id=agent.id,
            title="Frozen principal",
            created_by=candidate.id,
            execution_user_id=None,
            status="doing",
        )
        db.add(task)
        await db.flush()
        active_run = TaskLog(
            task_id=task.id,
            content="🤖 开始执行任务...",
            execution_user_id=None,
        )
        db.add(active_run)
        await db.commit()

    async with async_session() as db:
        member = await db.get(User, member.id)
        changed = await update_task(
            agent.id,
            task.id,
            TaskUpdate(description="changed by member"),
            member,
            db,
        )
        assert changed.execution_user_id == member.id
        assert (await db.get(TaskLog, active_run.id)).execution_user_id == candidate.id
        await db.commit()

    async with async_session() as db:
        admin = await db.get(User, admin.id)
        changed = await update_task(
            agent.id,
            task.id,
            TaskUpdate(
                execution_user_id=candidate.id,
                expected_execution_user_id=member.id,
            ),
            admin,
            db,
        )
        assert changed.execution_user_id == candidate.id


async def test_schedule_fields_remain_creator_only_and_identity_matches_actor():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Schedule {suffix}", slug=f"schedule-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"schedule_creator_{suffix}")
        manager = await _user(db, tenant.id, "member", f"schedule_manager_{suffix}")
        org_admin = await _user(db, tenant.id, "org_admin", f"schedule_admin_{suffix}")
        candidate = await _user(db, tenant.id, "member", f"schedule_candidate_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Schedule Agent {suffix}",
            access_mode="custom",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        db.add_all(
            [AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=manager.id,
                access_level="manage",
            ), AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=candidate.id,
                access_level="use",
            )]
        )
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Creator-owned schedule",
            instruction="original",
            cron_expr="0 9 * * *",
            created_by=creator.id,
            execution_user_id=creator.id,
        )
        db.add(schedule)
        await db.commit()

    async with async_session() as db:
        manager = await db.get(User, manager.id)
        with pytest.raises(HTTPException) as denied:
            await update_schedule(
                agent.id,
                schedule.id,
                ScheduleUpdate(name="manager edit"),
                manager,
                db,
            )
        assert denied.value.status_code == 403

    async with async_session() as db:
        candidate = await db.get(User, candidate.id)
        with pytest.raises(HTTPException) as identity_only_denied:
            await update_schedule(
                agent.id,
                schedule.id,
                ScheduleUpdate(
                    execution_user_id=candidate.id,
                    expected_execution_user_id=creator.id,
                ),
                candidate,
                db,
            )
        assert identity_only_denied.value.status_code == 403

    async with async_session() as db:
        org_admin = await db.get(User, org_admin.id)
        changed = await update_schedule(
            agent.id,
            schedule.id,
            ScheduleUpdate(
                execution_user_id=candidate.id,
                expected_execution_user_id=creator.id,
            ),
            org_admin,
            db,
        )
        assert changed.execution_user_id == candidate.id


async def test_org_admin_can_reassign_private_agent_schedule_identity():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Private Schedule {suffix}", slug=f"private-schedule-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"private_creator_{suffix}")
        org_admin = await _user(db, tenant.id, "org_admin", f"private_admin_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Private Schedule Agent {suffix}",
            access_mode="private",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        db.add(
            AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=org_admin.id,
                access_level="use",
            )
        )
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Private schedule",
            instruction="original",
            cron_expr="0 9 * * *",
            created_by=creator.id,
            execution_user_id=creator.id,
        )
        db.add(schedule)
        await db.commit()

    async with async_session() as db:
        actor = await db.get(User, org_admin.id)
        changed = await update_schedule(
            agent.id,
            schedule.id,
            ScheduleUpdate(
                execution_user_id=org_admin.id,
                expected_execution_user_id=creator.id,
            ),
            actor,
            db,
        )
        assert changed.execution_user_id == org_admin.id


async def test_regular_trigger_update_freezes_queued_legacy_identity():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Trigger Update {suffix}", slug=f"trigger-update-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"trigger_creator_{suffix}")
        org_admin = await _user(db, tenant.id, "org_admin", f"trigger_admin_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Trigger Update Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"queued-{suffix}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="before",
        )
        db.add(trigger)
        await db.flush()
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            execution_user_id=None,
            status="pending",
            source="cron",
            idempotency_key=f"queued-update-{suffix}",
            payload={},
            payload_text="",
        )
        db.add(execution)
        await db.commit()

    async with async_session() as db:
        actor = await db.get(User, org_admin.id)
        await db.refresh(actor, ["identity"])
        await update_trigger(
            agent.id,
            trigger.id,
            TriggerUpdate(reason="changed by admin"),
            actor,
        )

    async with async_session() as db:
        refreshed_trigger = await db.get(AgentTrigger, trigger.id)
        refreshed_execution = await db.get(TriggerExecution, execution.id)
        assert refreshed_trigger.execution_user_id == org_admin.id
        assert refreshed_execution.execution_user_id == creator.id
        same_identity_execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            execution_user_id=None,
            status="pending",
            source="cron",
            idempotency_key=f"queued-same-identity-{suffix}",
            payload={},
            payload_text="",
        )
        db.add(same_identity_execution)
        await db.commit()

    async with async_session() as db:
        actor = await db.get(User, org_admin.id)
        await db.refresh(actor, ["identity"])
        await update_trigger(
            agent.id,
            trigger.id,
            TriggerUpdate(reason="changed again by same admin"),
            actor,
        )

    async with async_session() as db:
        assert (
            await db.get(TriggerExecution, same_identity_execution.id)
        ).execution_user_id == org_admin.id


async def test_manual_schedule_run_aligns_execution_user_to_current_user(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Schedule Run {suffix}", slug=f"schedule-run-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"run_creator_{suffix}")
        operator = await _user(db, tenant.id, "member", f"run_operator_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Schedule Run Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Manual run",
            instruction="run now",
            cron_expr="0 9 * * *",
            created_by=creator.id,
            execution_user_id=creator.id,
        )
        db.add(schedule)
        await db.commit()

    captured: dict[str, object] = {}

    async def _capture(*args):
        captured["args"] = args

    monkeypatch.setattr("app.services.scheduler._execute_schedule", _capture)
    async with async_session() as db:
        operator = await db.get(User, operator.id)
        result = await trigger_schedule(agent.id, schedule.id, operator, db)
        assert result["status"] == "triggered"
        await asyncio.sleep(0)
        assert captured["args"][3] == operator.id
        assert (await db.get(AgentSchedule, schedule.id)).execution_user_id == operator.id


async def test_execution_picker_only_lists_users_with_agent_access_without_org_profile():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Picker {suffix}", slug=f"picker-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"picker_creator_{suffix}")
        org_admin = await _user(db, tenant.id, "org_admin", f"picker_admin_{suffix}")
        allowed = await _user(db, tenant.id, "member", f"picker_allowed_{suffix}")
        denied = await _user(db, tenant.id, "member", f"picker_denied_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Picker Agent {suffix}",
            access_mode="custom",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        db.add(
            AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=allowed.id,
                access_level="use",
            )
        )
        await db.commit()

    async with async_session() as db:
        org_admin = await db.get(User, org_admin.id)
        result = await get_agent_permission_members(
            agent.id,
            execution_assignable=True,
            page=1,
            page_size=50,
            current_user=org_admin,
            db=db,
        )
        candidate_ids = {uuid.UUID(item["id"]) for item in result["items"]}
        assert creator.id in candidate_ids
        assert allowed.id in candidate_ids
        assert org_admin.id in candidate_ids
        assert denied.id not in candidate_ids


async def test_agent_user_can_read_trigger_identity_labels_without_manage_access():
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Read Trigger {suffix}", slug=f"read-trigger-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"read_creator_{suffix}")
        reader = await _user(db, tenant.id, "member", f"read_member_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Read Trigger Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"read-trigger-{suffix}",
            type="cron",
            config={
                "expr": "0 9 * * *",
                "_secret": "hidden",
                "headers": {"Authorization": "Bearer hidden"},
                "credential": "hidden",
                "cookie": "hidden",
            },
            reason="daily",
        )
        db.add(trigger)
        await db.commit()

    async with async_session() as db:
        reader = await db.get(User, reader.id)
        rows = await list_agent_triggers(agent.id, reader)
    assert len(rows) == 1
    assert rows[0].creator_display_name == creator.display_name
    assert rows[0].execution_user_display_name == creator.display_name
    assert rows[0].config == {"expr": "0 9 * * *"}


async def test_org_admin_agent_tool_assigns_valid_target_without_touching_queued_identity():
    from app.services.execution_identity import handle_reassign_background_execution_user

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Org {suffix}", slug=f"org-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"creator_{suffix}")
        org_admin = await _user(db, tenant.id, "org_admin", f"org_admin_{suffix}")
        candidate = await _user(db, tenant.id, "member", f"candidate_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Org Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"trigger-{suffix}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="daily",
        )
        ctx = ChatSession(
            agent_id=agent.id,
            user_id=org_admin.id,
            source_channel="web",
            title="admin",
        )
        db.add_all([trigger, ctx])
        await db.flush()
        anchor = ChatMessage(
            agent_id=agent.id,
            user_id=org_admin.id,
            role="user",
            content="please hand over",
            conversation_id=str(ctx.id),
        )
        db.add(anchor)
        pending = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            execution_user_id=None,
            status="pending",
            source="cron",
            idempotency_key=f"queued-{suffix}",
            payload={},
            payload_text="",
        )
        db.add(pending)
        await db.commit()

    result = await handle_reassign_background_execution_user(
        agent.id,
        org_admin.id,
        str(ctx.id),
        anchor.id,
        {
            "resource_type": "trigger",
            "resource_id": str(trigger.id),
            "execution_user_id": str(candidate.id),
            "expected_execution_user_id": str(creator.id),
            "reason": "handover",
        },
    )
    assert result.startswith("✅")

    async with async_session() as db:
        refreshed_trigger = await db.get(AgentTrigger, trigger.id)
        refreshed_pending = await db.get(TriggerExecution, pending.id)
        assert refreshed_trigger.execution_user_id == candidate.id
        assert refreshed_pending.execution_user_id == creator.id
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "background_execution_user_changed",
                AuditLog.agent_id == agent.id,
            )
        )
        assert audit.details["reason"] == "handover"
        assert audit.details["frozen_execution_count"] == 1


async def test_reassignment_freezes_each_on_message_execution_to_its_payload_origin():
    from app.services.execution_identity import reassign_background_execution_user

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Origin {suffix}", slug=f"origin-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"origin_creator_{suffix}")
        admin = await _user(db, tenant.id, "org_admin", f"origin_admin_{suffix}")
        first = await _user(db, tenant.id, "member", f"origin_first_{suffix}")
        second = await _user(db, tenant.id, "member", f"origin_second_{suffix}")
        candidate = await _user(db, tenant.id, "member", f"origin_next_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Origin Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"origin-trigger-{suffix}",
            type="on_message",
            config={"_origin_session_id": str(uuid.uuid4()), "_origin_user_id": str(first.id)},
            reason="message",
        )
        db.add(trigger)
        await db.flush()
        executions = []
        for index, origin in enumerate((first, second)):
            execution = TriggerExecution(
                trigger_id=trigger.id,
                agent_id=agent.id,
                execution_user_id=None,
                status="pending",
                source="on_message",
                idempotency_key=f"origin-{suffix}-{index}",
                payload={
                    "_origin_session_id": str(uuid.uuid4()),
                    "_origin_user_id": str(origin.id),
                },
                payload_text="",
            )
            db.add(execution)
            executions.append(execution)
        await db.commit()

    async with async_session() as db:
        await reassign_background_execution_user(
            db,
            actor_user_id=admin.id,
            agent_id=agent.id,
            resource_type="trigger",
            resource_id=trigger.id,
            execution_user_id=candidate.id,
            expected_execution_user_id=creator.id,
            expected_provided=True,
        )
        await db.commit()

    async with async_session() as db:
        frozen = [await db.get(TriggerExecution, item.id) for item in executions]
        assert [item.execution_user_id for item in frozen] == [first.id, second.id]


async def test_task_reassignment_preserves_only_the_active_run_snapshot():
    from app.services.execution_identity import reassign_background_execution_user

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Task Run {suffix}", slug=f"task-run-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"run_creator_{suffix}")
        admin = await _user(db, tenant.id, "org_admin", f"run_admin_{suffix}")
        candidate = await _user(db, tenant.id, "member", f"run_next_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Task Run Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        task = Task(
            agent_id=agent.id,
            title="Active run",
            created_by=creator.id,
            execution_user_id=creator.id,
            status="doing",
        )
        db.add(task)
        await db.flush()
        start = TaskLog(task_id=task.id, content="🤖 开始执行任务...")
        unrelated = TaskLog(task_id=task.id, content="manual progress")
        db.add_all([start, unrelated])
        await db.commit()

    async with async_session() as db:
        change = await reassign_background_execution_user(
            db,
            actor_user_id=admin.id,
            agent_id=agent.id,
            resource_type="task",
            resource_id=task.id,
            execution_user_id=candidate.id,
            expected_execution_user_id=creator.id,
            expected_provided=True,
        )
        await db.commit()
        assert change.frozen_execution_count == 1

    async with async_session() as db:
        assert (await db.get(TaskLog, start.id)).execution_user_id == creator.id
        assert (await db.get(TaskLog, unrelated.id)).execution_user_id is None
        assert (await db.get(Task, task.id)).execution_user_id == candidate.id


async def test_claim_freezes_null_on_message_identity_from_execution_payload():
    from app.services.trigger_runtime.executions import claim_pending_trigger_executions

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Claim {suffix}", slug=f"claim-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"claim_creator_{suffix}")
        origin = await _user(db, tenant.id, "member", f"claim_origin_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Claim Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"claim-trigger-{suffix}",
            type="on_message",
            config={"_origin_session_id": str(uuid.uuid4()), "_origin_user_id": str(creator.id)},
            reason="message",
        )
        db.add(trigger)
        await db.flush()
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            execution_user_id=None,
            status="pending",
            source="on_message",
            idempotency_key=f"claim-{suffix}",
            payload={
                "_origin_session_id": str(uuid.uuid4()),
                "_origin_user_id": str(origin.id),
            },
            payload_text="",
        )
        db.add(execution)
        await db.commit()

    pairs = await claim_pending_trigger_executions(sources=["on_message"], limit=100)
    claimed = next(item for item, _trigger in pairs if item.id == execution.id)
    assert claimed.execution_user_id == origin.id

    async with async_session() as db:
        assert (await db.get(TriggerExecution, execution.id)).execution_user_id == origin.id


async def test_claim_preserves_inactive_origin_but_rejects_cross_tenant_origin():
    from app.services.execution_identity import (
        ExecutionIdentityError,
        resolve_execution_user_id,
    )
    from app.services.trigger_runtime.executions import claim_pending_trigger_executions

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Inactive Claim {suffix}", slug=f"inactive-claim-{suffix}")
        foreign_tenant = Tenant(
            name=f"Foreign Claim {suffix}",
            slug=f"foreign-claim-{suffix}",
        )
        db.add_all([tenant, foreign_tenant])
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"inactive_creator_{suffix}")
        inactive = await _user(db, tenant.id, "member", f"inactive_origin_{suffix}")
        inactive.is_active = False
        foreign = await _user(db, foreign_tenant.id, "member", f"foreign_origin_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Inactive Claim Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"inactive-claim-trigger-{suffix}",
            type="on_message",
            config={},
            reason="message",
        )
        db.add(trigger)
        await db.flush()
        inactive_execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            status="pending",
            source="on_message",
            idempotency_key=f"inactive-claim-{suffix}",
            payload={
                "_origin_session_id": str(uuid.uuid4()),
                "_origin_user_id": str(inactive.id),
            },
            payload_text="",
        )
        foreign_execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            status="pending",
            source="on_message",
            idempotency_key=f"foreign-claim-{suffix}",
            payload={
                "_origin_session_id": str(uuid.uuid4()),
                "_origin_user_id": str(foreign.id),
            },
            payload_text="",
        )
        db.add_all([inactive_execution, foreign_execution])
        await db.commit()

    pairs = await claim_pending_trigger_executions(sources=["on_message"], limit=100)
    claimed_by_id = {execution.id: execution for execution, _trigger in pairs}
    assert claimed_by_id[inactive_execution.id].execution_user_id == inactive.id
    assert claimed_by_id[foreign_execution.id].execution_user_id == creator.id

    async with async_session() as db:
        loaded_agent = await db.get(Agent, agent.id)
        with pytest.raises(ExecutionIdentityError, match="missing or inactive"):
            await resolve_execution_user_id(db, loaded_agent, inactive.id)


async def test_member_creator_and_unattended_context_cannot_use_reassignment_tool():
    from app.services.execution_identity import handle_reassign_background_execution_user

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Locked {suffix}", slug=f"locked-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, "member", f"creator_locked_{suffix}")
        admin = await _user(db, tenant.id, "org_admin", f"admin_locked_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Locked Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name=f"locked-trigger-{suffix}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="daily",
        )
        member_ctx = ChatSession(
            agent_id=agent.id,
            user_id=creator.id,
            source_channel="web",
            title="member",
        )
        trigger_ctx = ChatSession(
            agent_id=agent.id,
            user_id=None,
            source_channel="trigger",
            title="background",
        )
        forged_human_ctx = ChatSession(
            agent_id=agent.id,
            user_id=admin.id,
            source_channel="web",
            title="background event in human session",
        )
        db.add_all([trigger, member_ctx, trigger_ctx, forged_human_ctx])
        await db.flush()
        member_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=creator.id,
            role="user",
            content="please hand over",
            conversation_id=str(member_ctx.id),
        )
        trigger_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=admin.id,
            role="user",
            content="background event",
            conversation_id=str(trigger_ctx.id),
            message_meta={"kind": "on_message_event", "trigger_execution_id": str(uuid.uuid4())},
        )
        forged_human_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=admin.id,
            role="user",
            content="background event in web history",
            conversation_id=str(forged_human_ctx.id),
            message_meta={"kind": "on_message_event", "trigger_execution_id": str(uuid.uuid4())},
        )
        db.add_all([member_anchor, trigger_anchor, forged_human_anchor])
        await db.commit()

    member_args = {
        "resource_type": "trigger",
        "resource_id": str(trigger.id),
        "execution_user_id": str(creator.id),
        "expected_execution_user_id": str(creator.id),
        "reason": "handover",
    }
    member_result = await handle_reassign_background_execution_user(
        agent.id, creator.id, str(member_ctx.id), member_anchor.id, member_args
    )
    assert "Only platform administrators and organization administrators" in member_result

    admin_args = {
        **member_args,
        "execution_user_id": str(admin.id),
    }
    unattended_result = await handle_reassign_background_execution_user(
        agent.id, admin.id, str(trigger_ctx.id), trigger_anchor.id, admin_args
    )
    assert "only run in a human interactive session" in unattended_result

    forged_human_result = await handle_reassign_background_execution_user(
        agent.id,
        admin.id,
        str(forged_human_ctx.id),
        forged_human_anchor.id,
        admin_args,
    )
    assert "only run in a human interactive session" in forged_human_result
