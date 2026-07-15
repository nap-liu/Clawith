import uuid
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.okr import MemberDailyReportUpsert, ObjectiveCreate, _obj_to_out
from app.database import async_session
from app.models.agent import Agent
from app.models.okr import MemberDailyReport, OKRObjective, WorkReport
from app.models.tenant import Tenant
from app.models.user import User
from app.services.okr_reporting import upsert_member_daily_report


def test_objective_contract_exposes_only_canonical_ids():
    user_id = uuid.uuid4()
    body = ObjectiveCreate(
        title="Canonical owner",
        user_id=str(user_id),
        period_start="2026-07-01",
        period_end="2026-09-30",
    )
    assert body.user_id == str(user_id)
    assert "owner_id" not in ObjectiveCreate.model_fields
    assert "owner_type" not in ObjectiveCreate.model_fields

    with pytest.raises(ValidationError):
        ObjectiveCreate(
            title="Ambiguous owner",
            user_id=str(user_id),
            agent_id=str(uuid.uuid4()),
            period_start="2026-07-01",
            period_end="2026-09-30",
        )


def test_objective_output_uses_user_id_or_agent_id_not_generic_owner():
    agent_id = uuid.uuid4()
    objective = OKRObjective(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        title="Agent objective",
        owner_type="agent",  # transitional persistence field
        owner_id=agent_id,  # transitional persistence field
        owner_agent_id=agent_id,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
        status="active",
        created_at=datetime.now(timezone.utc),
    )

    payload = _obj_to_out(objective).model_dump()
    assert payload["agent_id"] == str(agent_id)
    assert payload["user_id"] is None
    assert "owner_id" not in payload
    assert "owner_type" not in payload


def test_daily_report_contract_is_exactly_one_canonical_id_or_current_user():
    assert "member_id" not in MemberDailyReportUpsert.model_fields
    assert "member_type" not in MemberDailyReportUpsert.model_fields
    current_user_request = MemberDailyReportUpsert(
        report_date="2026-07-15",
        content="done",
    )
    assert current_user_request.user_id is None
    assert current_user_request.agent_id is None

    with pytest.raises(ValidationError):
        MemberDailyReportUpsert(
            report_date="2026-07-15",
            content="ambiguous",
            user_id=str(uuid.uuid4()),
            agent_id=str(uuid.uuid4()),
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_canonical_okr_ids_persist_and_sync_internal_compatibility_fields():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"OKR identity {suffix}", slug=f"okr-identity-{suffix}")
        db.add(tenant)
        await db.flush()
        user = User(
            tenant_id=tenant.id,
            display_name="Canonical Human",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=user.id,
            name="Canonical Agent",
            status="idle",
        )
        db.add(agent)
        await db.flush()

        objective = OKRObjective(
            tenant_id=tenant.id,
            title="Canonical objective",
            owner_user_id=user.id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 9, 30),
        )
        work_report = WorkReport(
            tenant_id=tenant.id,
            agent_id=agent.id,
            report_type="daily",
            period_date=date(2026, 7, 15),
            content="done",
        )
        db.add_all([objective, work_report])
        await db.commit()
        tenant_id = tenant.id
        user_id = user.id
        agent_id = agent.id
        objective_id = objective.id
        work_report_id = work_report.id

    async with async_session() as db:
        persisted_objective = await db.get(OKRObjective, objective_id)
        persisted_work_report = await db.get(WorkReport, work_report_id)
        assert persisted_objective.owner_user_id == user_id
        assert persisted_objective.owner_agent_id is None
        assert persisted_objective.owner_type == "user"
        assert persisted_objective.owner_id == user_id
        assert persisted_work_report.user_id is None
        assert persisted_work_report.agent_id == agent_id
        assert persisted_work_report.author_type == "agent"
        assert persisted_work_report.author_id == agent_id

    daily_report = await upsert_member_daily_report(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=None,
        report_date=date(2026, 7, 15),
        content="canonical daily report",
    )
    async with async_session() as db:
        persisted_daily_report = await db.scalar(
            select(MemberDailyReport).where(MemberDailyReport.id == daily_report.id)
        )
        assert persisted_daily_report.user_id == user_id
        assert persisted_daily_report.agent_id is None
        assert persisted_daily_report.member_type == "user"
        assert persisted_daily_report.member_id == user_id


@pytest.mark.asyncio(loop_scope="module")
async def test_daily_report_service_rejects_cross_tenant_canonical_id():
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant_a = Tenant(name=f"OKR A {suffix}", slug=f"okr-a-{suffix}")
        tenant_b = Tenant(name=f"OKR B {suffix}", slug=f"okr-b-{suffix}")
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        foreign_user = User(
            tenant_id=tenant_b.id,
            display_name="Foreign Human",
            role="member",
            is_active=True,
        )
        db.add(foreign_user)
        await db.commit()
        tenant_a_id = tenant_a.id
        foreign_user_id = foreign_user.id

    with pytest.raises(ValueError, match="not an active user in this tenant"):
        await upsert_member_daily_report(
            tenant_id=tenant_a_id,
            user_id=foreign_user_id,
            agent_id=None,
            report_date=date(2026, 7, 15),
            content="must fail closed",
        )
