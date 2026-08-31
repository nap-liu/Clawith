"""OKR report routes."""

import uuid
from datetime import date

from fastapi import Depends, HTTPException
from sqlalchemy import select

from app.api.auth import get_current_user
from app.api.okr_models import (
    CompanyReportOut,
    CompanyReportRegenerate,
    MemberDailyReportOut,
    MemberDailyReportUpsert,
    WorkReportOut,
    _serialize_company_report,
)
from app.api.okr_shared import async_session, router
from app.models.okr import WorkReport


@router.get("/member-daily-reports", response_model=list[MemberDailyReportOut])
async def list_member_daily_reports(
    report_date: str | None = None,
    user=Depends(get_current_user),
):
    """List all member daily reports for a specific date plus missing members."""
    from app.services.okr_reporting import list_member_daily_reports_for_date

    target_day = date.fromisoformat(report_date) if report_date else date.today()
    items = await list_member_daily_reports_for_date(user.tenant_id, target_day)
    return [
        MemberDailyReportOut(
            id=f"{item.get('user_id') or item.get('agent_id')}:{target_day.isoformat()}",
            user_id=item.get("user_id"),
            agent_id=item.get("agent_id"),
            display_name=item["display_name"],
            avatar_url=item["avatar_url"],
            group_label=item["group_label"],
            report_date=target_day.isoformat(),
            content=item["content"],
            status=item["status"],
            submitted_at=item["submitted_at"],
            updated_at=item["updated_at"],
        )
        for item in items
    ]


@router.post("/member-daily-reports", response_model=MemberDailyReportOut)
async def upsert_member_daily_report(
    body: MemberDailyReportUpsert,
    user=Depends(get_current_user),
):
    """Create or update a member daily report.

    Regular members can only edit their own user report.
    Org admins and platform admins may specify a tenant member explicitly.
    """
    from app.services.okr_reporting import (
        list_tracked_okr_members,
        upsert_member_daily_report as _upsert,
    )

    target_user_id = uuid.UUID(body.user_id) if body.user_id else None
    target_agent_id = uuid.UUID(body.agent_id) if body.agent_id else None
    if not target_user_id and not target_agent_id:
        target_user_id = user.id

    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        if target_user_id != user.id or target_agent_id is not None:
            raise HTTPException(403, "You can only submit your own daily report")

    report_date = date.fromisoformat(body.report_date)
    report = await _upsert(
        tenant_id=user.tenant_id,
        user_id=target_user_id,
        agent_id=target_agent_id,
        report_date=report_date,
        content=body.content,
        source=body.source,
    )
    member_map = {
        (("user", str(member.user_id)) if member.user_id else ("agent", str(member.agent_id))): member
        for member in await list_tracked_okr_members(user.tenant_id)
    }
    report_key = ("user", str(report.user_id)) if report.user_id else ("agent", str(report.agent_id))
    member_meta = member_map.get(report_key)
    return MemberDailyReportOut(
        id=str(report.id),
        user_id=str(report.user_id) if report.user_id else None,
        agent_id=str(report.agent_id) if report.agent_id else None,
        display_name=member_meta.display_name if member_meta else str(report.user_id or report.agent_id),
        avatar_url=member_meta.avatar_url if member_meta else None,
        group_label=member_meta.group_label if member_meta else "Members",
        report_date=report.report_date.isoformat(),
        content=report.content,
        status=report.status,
        submitted_at=report.submitted_at.isoformat() if report.submitted_at else None,
        updated_at=report.updated_at.isoformat() if report.updated_at else None,
    )


@router.get("/company-reports", response_model=list[CompanyReportOut])
async def list_company_reports_api(
    report_type: str | None = None,
    limit: int = 50,
    user=Depends(get_current_user),
):
    """List company-level reports from the new reporting pipeline."""
    from app.services.okr_reporting import list_company_reports

    reports = await list_company_reports(user.tenant_id, report_type=report_type, limit=limit)
    return [_serialize_company_report(report) for report in reports]


@router.post("/company-reports/regenerate", response_model=CompanyReportOut)
async def regenerate_company_report(
    body: CompanyReportRegenerate,
    user=Depends(get_current_user),
):
    """Rebuild a single company report for a target period."""
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can regenerate company reports")

    from app.services.okr_reporting import (
        generate_company_daily_report,
        generate_company_monthly_report,
        generate_company_weekly_report,
    )

    period_start = date.fromisoformat(body.period_start)
    if body.report_type == "daily":
        report = await generate_company_daily_report(user.tenant_id, period_start)
    elif body.report_type == "weekly":
        report = await generate_company_weekly_report(user.tenant_id, period_start)
    elif body.report_type == "monthly":
        report = await generate_company_monthly_report(user.tenant_id, period_start)
    else:
        raise HTTPException(400, "Invalid report_type")

    return _serialize_company_report(report)


@router.get("/reports", response_model=list[WorkReportOut])
async def list_reports(
    report_type: str | None = None,  # "daily" | "weekly" | None for both
    limit: int = 50,
    user=Depends(get_current_user),
):
    """List work reports for the current tenant, newest first."""
    async with async_session() as db:
        query = (
            select(WorkReport)
            .where(WorkReport.tenant_id == user.tenant_id)
            .order_by(WorkReport.period_date.desc(), WorkReport.created_at.desc())
            .limit(limit)
        )
        if report_type:
            query = query.where(WorkReport.report_type == report_type)

        result = await db.execute(query)
        reports = result.scalars().all()

    return [
        WorkReportOut(
            id=str(r.id),
            user_id=str(r.user_id) if r.user_id else None,
            agent_id=str(r.agent_id) if r.agent_id else None,
            report_type=r.report_type,
            period_date=r.period_date.isoformat(),
            content=r.content,
            source=r.source,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in reports
    ]
