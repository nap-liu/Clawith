"""OKR reporting services built on top of member daily reports.

This module implements the simplified reporting chain:

  member daily report -> company daily report -> company weekly report
  -> company monthly report

The implementation intentionally keeps summarization lightweight:
  - member reports are capped at 2000 chars at write time
  - company reports use deterministic section-building
  - bucketed aggregation is used when source volume is large
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from loguru import logger

from app.database import async_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.okr import CompanyReport, MemberDailyReport, OKRSettings
from app.models.org import AgentAgentRelationship, AgentRelationship
from app.models.user import User
from app.services.llm.client import chat_complete
from app.services.llm.utils import get_model_api_key, get_max_tokens
from app.services.okr_report_content import (
    BUCKET_SIZE,
    LLM_PROMPT_CHAR_LIMIT,
    MEMBER_DAILY_CHAR_LIMIT,
    RISK_KEYWORDS,
    _bucket_items,
    _build_company_daily_content,
    _build_company_rollup_content,
    _contains_risk,
    _dedupe_preserve_order,
    _default_report_headings,
    _extract_section_lines,
    _is_placeholder_rollup_line,
    _monday_of,
    _month_end,
    _month_start,
    _period_label,
    _sanitize_llm_report_output,
    _summarize_member_bucket,
    _truncate_for_prompt,
    _truncate_report_content,
)


@dataclass
class CompanyMember:
    """Resolved member metadata used by reporting and the Reports UI."""

    user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    display_name: str
    avatar_url: str | None
    group_label: str


@dataclass
class ResolvedReportModels:
    """Resolved OKR Agent models used for company report generation."""

    primary: LLMModel | None
    fallback: LLMModel | None
    okr_agent_id: uuid.UUID | None


async def _resolve_report_models(tenant_id: uuid.UUID) -> ResolvedReportModels:
    """Load the OKR Agent's primary/fallback models for report generation."""
    async with async_session() as db:
        settings_result = await db.execute(
            select(OKRSettings).where(OKRSettings.tenant_id == tenant_id)
        )
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.okr_agent_id:
            return ResolvedReportModels(primary=None, fallback=None, okr_agent_id=None)

        agent_result = await db.execute(select(Agent).where(Agent.id == settings.okr_agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return ResolvedReportModels(primary=None, fallback=None, okr_agent_id=settings.okr_agent_id)

        from app.services.chat_model_selection import resolve_runtime_models

        runtime_models = await resolve_runtime_models(db, agent=agent)

        return ResolvedReportModels(
            primary=runtime_models.primary_model,
            fallback=runtime_models.fallback_model,
            okr_agent_id=settings.okr_agent_id,
        )


async def list_company_members(tenant_id: uuid.UUID) -> list[CompanyMember]:
    """Return active human members plus active non-system agents in the tenant."""
    async with async_session() as db:
        users_result = await db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        agents_result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.is_system == False,  # noqa: E712
                Agent.status.notin_(["stopped", "error"]),
            )
        )

        members: list[CompanyMember] = []
        for user in users_result.scalars().all():
            members.append(
                CompanyMember(
                    user_id=user.id,
                    agent_id=None,
                    display_name=user.display_name,
                    avatar_url=user.avatar_url,
                    group_label=user.title or "Members",
                )
            )
        for agent in agents_result.scalars().all():
            members.append(
                CompanyMember(
                    user_id=None,
                    agent_id=agent.id,
                    display_name=agent.name,
                    avatar_url=agent.avatar_url,
                    group_label="Digital Employees",
                )
            )
        members.sort(key=lambda item: (item.group_label, item.display_name.lower()))
        return members


async def list_tracked_okr_members(tenant_id: uuid.UUID) -> list[CompanyMember]:
    """Return only members currently tracked in the OKR Agent relationship network."""
    async with async_session() as db:
        settings_result = await db.execute(
            select(OKRSettings).where(OKRSettings.tenant_id == tenant_id)
        )
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.okr_agent_id:
            return []

        human_result = await db.execute(
            select(User)
            .join(AgentRelationship, AgentRelationship.user_id == User.id)
            .where(
                AgentRelationship.agent_id == settings.okr_agent_id,
                User.tenant_id == tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        agent_result = await db.execute(
            select(Agent)
            .join(
                AgentAgentRelationship,
                AgentAgentRelationship.target_agent_id == Agent.id,
            )
            .where(
                AgentAgentRelationship.agent_id == settings.okr_agent_id,
                Agent.is_system == False,  # noqa: E712
                Agent.status.notin_(["stopped", "error"]),
            )
        )

        members: list[CompanyMember] = []
        for platform_user in human_result.scalars().all():
            members.append(
                CompanyMember(
                    user_id=platform_user.id,
                    agent_id=None,
                    display_name=platform_user.display_name,
                    avatar_url=platform_user.avatar_url,
                    group_label=platform_user.title or "Members",
                )
            )
        for agent in agent_result.scalars().all():
            members.append(
                CompanyMember(
                    user_id=None,
                    agent_id=agent.id,
                    display_name=agent.name,
                    avatar_url=agent.avatar_url,
                    group_label="Digital Employees",
                )
            )
        members.sort(key=lambda item: (item.group_label, item.display_name.lower()))
        return members


async def upsert_member_daily_report(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    report_date: date,
    content: str,
    *,
    source: str = "okr_agent_assisted",
    mark_late_if_past: bool = True,
) -> MemberDailyReport:
    """Create or update a member daily report and mark related company reports dirty."""
    if (user_id is None) == (agent_id is None):
        raise ValueError("Exactly one of user_id or agent_id is required")
    normalized = _truncate_report_content(content)
    today = date.today()
    status = "late" if mark_late_if_past and report_date < today else "submitted"

    async with async_session() as db:
        if user_id is not None:
            actor_exists = await db.scalar(
                select(User.id).where(
                    User.id == user_id,
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                )
            )
            if actor_exists is None:
                raise ValueError("user_id is not an active user in this tenant")
        else:
            actor_exists = await db.scalar(
                select(Agent.id).where(
                    Agent.id == agent_id,
                    Agent.tenant_id == tenant_id,
                    Agent.is_deleted.is_(False),
                    Agent.is_expired.is_(False),
                    Agent.status.notin_(["stopped", "error"]),
                )
            )
            if actor_exists is None:
                raise ValueError("agent_id is not an active agent in this tenant")

        result = await db.execute(
            select(MemberDailyReport).where(
                MemberDailyReport.tenant_id == tenant_id,
                MemberDailyReport.user_id == user_id if user_id else MemberDailyReport.agent_id == agent_id,
                MemberDailyReport.report_date == report_date,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            previous_content = existing.content
            existing.content = normalized
            existing.status = "revised" if previous_content != normalized else existing.status
            existing.source = source
            existing.updated_at = datetime.now(timezone.utc)
            report = existing
        else:
            report = MemberDailyReport(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                report_date=report_date,
                content=normalized,
                status=status,
                source=source,
            )
            db.add(report)

        await _mark_dependent_company_reports_for_refresh(db, tenant_id, report_date)
        await db.commit()
        await db.refresh(report)
        return report


async def list_member_daily_reports_for_date(
    tenant_id: uuid.UUID,
    report_date: date,
) -> list[dict]:
    """Return all tenant members with report status for a specific date."""
    members = await list_tracked_okr_members(tenant_id)
    async with async_session() as db:
        result = await db.execute(
            select(MemberDailyReport).where(
                MemberDailyReport.tenant_id == tenant_id,
                MemberDailyReport.report_date == report_date,
            )
        )
        reports = {
            (("user", row.user_id) if row.user_id else ("agent", row.agent_id)): row
            for row in result.scalars().all()
        }

    items: list[dict] = []
    for member in members:
        member_key = ("user", member.user_id) if member.user_id else ("agent", member.agent_id)
        report = reports.get(member_key)
        items.append({
            "user_id": str(member.user_id) if member.user_id else None,
            "agent_id": str(member.agent_id) if member.agent_id else None,
            "display_name": member.display_name,
            "avatar_url": member.avatar_url,
            "group_label": member.group_label,
            "status": report.status if report else "missing",
            "content": report.content if report else "",
            "submitted_at": report.submitted_at.isoformat() if report and report.submitted_at else None,
            "updated_at": report.updated_at.isoformat() if report and report.updated_at else None,
        })
    return items


async def _generate_llm_report_content(
    tenant_id: uuid.UUID,
    report_type: str,
    period_start: date,
    period_end: date,
    payload: dict,
    *,
    fallback_content: str,
) -> str:
    """Generate a structured company report with the OKR Agent model."""
    models = await _resolve_report_models(tenant_id)
    if not models.primary:
        return fallback_content

    title, period_key = _default_report_headings(report_type)
    period_value = (
        period_start.isoformat()
        if report_type == "daily"
        else f"{period_start.isoformat()} to {period_end.isoformat()}"
    )
    system_prompt = (
        "You are the OKR reporting copilot for an enterprise workspace. "
        "Write a concise management-style markdown report in Simplified Chinese. "
        "Use only the provided facts. Do not invent progress, risks, or actions. "
        "Do not expose raw extraction mechanics such as bucket labels. "
        "Merge similar updates into coherent summaries."
    )
    user_prompt = (
        f"Generate a {report_type} company OKR report.\n"
        "Return markdown only.\n"
        "Use this exact structure:\n"
        f"# {title}\n"
        f"{period_key}: {period_value}\n\n"
        "## Executive Summary\n"
        "- 2 to 4 bullets.\n\n"
        "## Key Progress\n"
        "- Group related updates into clear bullets.\n\n"
        "## Risks and Blockers\n"
        "- Summarize meaningful risks. If none, say so briefly.\n\n"
        "## Follow-up Actions\n"
        "- Concrete next steps or reminders.\n\n"
        "## Submission Status\n"
        "- Describe submission coverage and who is still missing if relevant.\n\n"
        "Rules:\n"
        "- Keep narrative text in Simplified Chinese.\n"
        "- Preserve member names exactly as given.\n"
        "- Avoid repeating the same fact across sections.\n"
        "- Do not copy raw entries line by line if they can be merged.\n"
        "- If the source data is sparse, state that clearly and keep the structure complete.\n\n"
        "Source data (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    async def _try_model(model: LLMModel) -> str:
        response = await chat_complete(
            provider=model.provider,
            api_key=get_model_api_key(model),
            model=model.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            base_url=model.base_url,
            temperature=model.temperature,
            reasoning_effort=model.reasoning_effort,
            max_tokens=min(get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None)), 1800),
            timeout=float(getattr(model, "request_timeout", None) or 120.0),
        )
        return (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

    for candidate in (models.primary, models.fallback):
        if not candidate:
            continue
        try:
            generated = await _try_model(candidate)
            normalized = _sanitize_llm_report_output(report_type, period_start, period_end, generated)
            if normalized:
                return normalized
        except Exception as exc:
            logger.warning(
                f"[OKR] LLM company report generation failed tenant={tenant_id} "
                f"report_type={report_type} model={getattr(candidate, 'model', '?')}: {exc}"
            )

    return fallback_content




async def _upsert_company_report(
    tenant_id: uuid.UUID,
    report_type: str,
    period_start: date,
    period_end: date,
    *,
    content: str,
    submitted_count: int,
    missing_count: int,
    needs_refresh: bool = False,
) -> CompanyReport:
    """Insert or update a company report for the same period."""
    async with async_session() as db:
        result = await db.execute(
            select(CompanyReport).where(
                CompanyReport.tenant_id == tenant_id,
                CompanyReport.report_type == report_type,
                CompanyReport.period_start == period_start,
                CompanyReport.period_end == period_end,
            )
        )
        existing = result.scalar_one_or_none()
        label = _period_label(report_type, period_start, period_end)
        if existing:
            existing.content = content
            existing.period_label = label
            existing.submitted_count = submitted_count
            existing.missing_count = missing_count
            existing.needs_refresh = needs_refresh
            existing.updated_at = datetime.now(timezone.utc)
            report = existing
        else:
            report = CompanyReport(
                tenant_id=tenant_id,
                report_type=report_type,
                period_start=period_start,
                period_end=period_end,
                period_label=label,
                content=content,
                submitted_count=submitted_count,
                missing_count=missing_count,
                needs_refresh=needs_refresh,
            )
            db.add(report)
        await db.commit()
        await db.refresh(report)
        return report


async def generate_company_daily_report(tenant_id: uuid.UUID, period_day: date) -> CompanyReport:
    """Generate the company daily report for a specific day."""
    members = await list_tracked_okr_members(tenant_id)
    async with async_session() as db:
        result = await db.execute(
            select(MemberDailyReport).where(
                MemberDailyReport.tenant_id == tenant_id,
                MemberDailyReport.report_date == period_day,
            )
        )
        rows = result.scalars().all()

    submitted_lookup = {
        (("user", row.user_id) if row.user_id else ("agent", row.agent_id)): row
        for row in rows
    }
    submitted_items: list[dict] = []
    missing_items: list[dict] = []
    for member in members:
        member_key = ("user", member.user_id) if member.user_id else ("agent", member.agent_id)
        row = submitted_lookup.get(member_key)
        member_payload = {
            "display_name": member.display_name,
            "content": row.content if row else "",
        }
        if row:
            submitted_items.append(member_payload)
        else:
            missing_items.append({"display_name": member.display_name})

    content = _build_company_daily_content(
        period_day,
        len(submitted_items),
        missing_items,
        submitted_items,
    )
    llm_payload = {
        "report_type": "daily",
        "period_start": period_day.isoformat(),
        "period_end": period_day.isoformat(),
        "submitted_count": len(submitted_items),
        "missing_count": len(missing_items),
        "submitted_members": [item["display_name"] for item in submitted_items],
        "missing_members": [item["display_name"] for item in missing_items],
        "submitted_reports": [
            {
                "member_name": item["display_name"],
                "content": _truncate_for_prompt(item["content"]),
            }
            for item in submitted_items
        ],
    }
    content = await _generate_llm_report_content(
        tenant_id,
        "daily",
        period_day,
        period_day,
        llm_payload,
        fallback_content=content,
    )
    return await _upsert_company_report(
        tenant_id,
        "daily",
        period_day,
        period_day,
        content=content,
        submitted_count=len(submitted_items),
        missing_count=len(missing_items),
        needs_refresh=False,
    )


async def generate_company_weekly_report(tenant_id: uuid.UUID, week_start: date) -> CompanyReport:
    """Generate the company weekly report for the ISO week starting at week_start."""
    week_end = week_start + timedelta(days=6)
    async with async_session() as db:
        result = await db.execute(
            select(CompanyReport).where(
                CompanyReport.tenant_id == tenant_id,
                CompanyReport.report_type == "daily",
                CompanyReport.period_start >= week_start,
                CompanyReport.period_start <= week_end,
            ).order_by(CompanyReport.period_start.asc())
        )
        source_reports = result.scalars().all()

    submitted_count = max((report.submitted_count for report in source_reports), default=0)
    missing_count = max((report.missing_count for report in source_reports), default=0)
    content = _build_company_rollup_content(
        "Company Weekly Report",
        week_start,
        week_end,
        source_reports,
        missing_count=missing_count,
        submitted_count=submitted_count,
    )
    llm_payload = {
        "report_type": "weekly",
        "period_start": week_start.isoformat(),
        "period_end": week_end.isoformat(),
        "source_report_count": len(source_reports),
        "submitted_count": submitted_count,
        "missing_count": missing_count,
        "source_reports": [
            {
                "period_label": report.period_label,
                "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(),
                "submitted_count": report.submitted_count,
                "missing_count": report.missing_count,
                "content": _truncate_for_prompt(report.content, limit=1800),
            }
            for report in source_reports
        ],
    }
    content = await _generate_llm_report_content(
        tenant_id,
        "weekly",
        week_start,
        week_end,
        llm_payload,
        fallback_content=content,
    )
    return await _upsert_company_report(
        tenant_id,
        "weekly",
        week_start,
        week_end,
        content=content,
        submitted_count=submitted_count,
        missing_count=missing_count,
        needs_refresh=False,
    )


async def generate_company_monthly_report(tenant_id: uuid.UUID, month_anchor: date) -> CompanyReport:
    """Generate the company monthly report for the month containing month_anchor."""
    period_start = _month_start(month_anchor)
    period_end = _month_end(month_anchor)
    async with async_session() as db:
        result = await db.execute(
            select(CompanyReport).where(
                CompanyReport.tenant_id == tenant_id,
                CompanyReport.report_type == "weekly",
                CompanyReport.period_start >= period_start,
                CompanyReport.period_start <= period_end,
            ).order_by(CompanyReport.period_start.asc())
        )
        source_reports = result.scalars().all()

    submitted_count = max((report.submitted_count for report in source_reports), default=0)
    missing_count = max((report.missing_count for report in source_reports), default=0)
    content = _build_company_rollup_content(
        "Company Monthly Report",
        period_start,
        period_end,
        source_reports,
        missing_count=missing_count,
        submitted_count=submitted_count,
    )
    llm_payload = {
        "report_type": "monthly",
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "source_report_count": len(source_reports),
        "submitted_count": submitted_count,
        "missing_count": missing_count,
        "source_reports": [
            {
                "period_label": report.period_label,
                "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(),
                "submitted_count": report.submitted_count,
                "missing_count": report.missing_count,
                "content": _truncate_for_prompt(report.content, limit=1800),
            }
            for report in source_reports
        ],
    }
    content = await _generate_llm_report_content(
        tenant_id,
        "monthly",
        period_start,
        period_end,
        llm_payload,
        fallback_content=content,
    )
    return await _upsert_company_report(
        tenant_id,
        "monthly",
        period_start,
        period_end,
        content=content,
        submitted_count=submitted_count,
        missing_count=missing_count,
        needs_refresh=False,
    )


async def list_company_reports(
    tenant_id: uuid.UUID,
    report_type: str | None = None,
    limit: int = 50,
) -> list[CompanyReport]:
    """List company reports newest first."""
    async with async_session() as db:
        query = (
            select(CompanyReport)
            .where(CompanyReport.tenant_id == tenant_id)
            .order_by(CompanyReport.period_start.desc(), CompanyReport.updated_at.desc())
            .limit(limit)
        )
        if report_type:
            query = query.where(CompanyReport.report_type == report_type)
        result = await db.execute(query)
        return list(result.scalars().all())


async def _mark_dependent_company_reports_for_refresh(db, tenant_id: uuid.UUID, report_day: date) -> None:
    """Mark the affected company reports as stale after a member report change."""
    week_start = _monday_of(report_day)
    week_end = week_start + timedelta(days=6)
    month_start = _month_start(report_day)
    month_end = _month_end(report_day)

    result = await db.execute(
        select(CompanyReport).where(
            CompanyReport.tenant_id == tenant_id,
            or_(
                and_(
                    CompanyReport.report_type == "daily",
                    CompanyReport.period_start == report_day,
                ),
                and_(
                    CompanyReport.report_type == "weekly",
                    CompanyReport.period_start == week_start,
                    CompanyReport.period_end == week_end,
                ),
                and_(
                    CompanyReport.report_type == "monthly",
                    CompanyReport.period_start == month_start,
                    CompanyReport.period_end == month_end,
                ),
            ),
        )
    )
    for report in result.scalars().all():
        report.needs_refresh = True
        report.updated_at = datetime.now(timezone.utc)
