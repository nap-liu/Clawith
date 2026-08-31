"""OKR API schemas and serialization helpers."""

import uuid

from pydantic import BaseModel, model_validator

from app.models.okr import CompanyReport, OKRKeyResult, OKRObjective


class OKRSettingsOut(BaseModel):
    enabled: bool
    first_enabled_at: str | None = None
    daily_report_enabled: bool
    daily_report_time: str
    daily_report_skip_non_workdays: bool = True
    weekly_report_enabled: bool
    weekly_report_day: int
    period_frequency: str
    period_length_days: int | None = None
    period_frequency_locked: bool = False
    # OKR Agent UUID for the chat-link button in the UI
    okr_agent_id: str | None = None


class OKRSettingsUpdate(BaseModel):
    enabled: bool | None = None
    daily_report_enabled: bool | None = None
    daily_report_time: str | None = None
    daily_report_skip_non_workdays: bool | None = None
    weekly_report_enabled: bool | None = None
    weekly_report_day: int | None = None
    period_frequency: str | None = None
    period_length_days: int | None = None


class KeyResultOut(BaseModel):
    id: str
    objective_id: str
    title: str
    target_value: float
    current_value: float
    unit: str | None = None
    focus_ref: str | None = None
    status: str
    last_updated_at: str
    created_at: str
    # Alignment refs (read-only summary)
    alignments: list[dict] = []


class ObjectiveOut(BaseModel):
    id: str
    title: str
    description: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    # Resolved human-readable name of the owner (user display_name / agent name).
    # None for company-level objectives.
    owner_name: str | None = None
    period_start: str
    period_end: str
    status: str
    created_at: str
    key_results: list[KeyResultOut] = []


class ObjectiveCreate(BaseModel):
    title: str
    description: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    period_start: str
    period_end: str

    @model_validator(mode="after")
    def validate_owner(self):
        if self.user_id and self.agent_id:
            raise ValueError("Provide at most one of user_id or agent_id")
        return self


class ObjectiveUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None


class KeyResultCreate(BaseModel):
    title: str
    target_value: float = 100.0
    unit: str | None = None
    focus_ref: str | None = None


class KeyResultUpdate(BaseModel):
    title: str | None = None
    current_value: float | None = None
    target_value: float | None = None
    unit: str | None = None
    focus_ref: str | None = None
    status: str | None = None


class ProgressUpdate(BaseModel):
    value: float
    note: str | None = None
    # Optional explicit status override; when omitted, auto-computed from progress ratio
    status: str | None = None


class PeriodOut(BaseModel):
    start: str
    end: str
    label: str
    is_current: bool


class WorkReportOut(BaseModel):
    id: str
    user_id: str | None = None
    agent_id: str | None = None
    report_type: str
    period_date: str
    content: str
    source: str
    created_at: str


class MemberDailyReportOut(BaseModel):
    id: str
    user_id: str | None = None
    agent_id: str | None = None
    display_name: str
    avatar_url: str | None = None
    group_label: str
    report_date: str
    content: str
    status: str
    submitted_at: str | None = None
    updated_at: str | None = None


class MemberDailyReportUpsert(BaseModel):
    report_date: str
    content: str
    user_id: str | None = None
    agent_id: str | None = None
    source: str = "manual"

    @model_validator(mode="after")
    def validate_member(self):
        if self.user_id and self.agent_id:
            raise ValueError("Provide at most one of user_id or agent_id")
        return self


class CompanyReportOut(BaseModel):
    id: str
    report_type: str
    period_start: str
    period_end: str
    period_label: str
    content: str
    submitted_count: int
    missing_count: int
    needs_refresh: bool
    generated_at: str
    updated_at: str


class CompanyReportRegenerate(BaseModel):
    report_type: str
    period_start: str


def _kr_to_out(kr: OKRKeyResult) -> KeyResultOut:
    return KeyResultOut(
        id=str(kr.id),
        objective_id=str(kr.objective_id),
        title=kr.title,
        target_value=kr.target_value,
        current_value=kr.current_value,
        unit=kr.unit,
        focus_ref=kr.focus_ref,
        status=kr.status,
        last_updated_at=kr.last_updated_at.isoformat() if kr.last_updated_at else "",
        created_at=kr.created_at.isoformat() if kr.created_at else "",
    )


def _obj_to_out(
    obj: OKRObjective,
    krs: list[OKRKeyResult] | None = None,
    owner_name: str | None = None,
) -> ObjectiveOut:
    return ObjectiveOut(
        id=str(obj.id),
        title=obj.title,
        description=obj.description,
        user_id=str(obj.owner_user_id) if obj.owner_user_id else None,
        agent_id=str(obj.owner_agent_id) if obj.owner_agent_id else None,
        owner_name=owner_name,
        period_start=obj.period_start.isoformat(),
        period_end=obj.period_end.isoformat(),
        status=obj.status,
        created_at=obj.created_at.isoformat() if obj.created_at else "",
        key_results=[_kr_to_out(kr) for kr in (krs or [])],
    )


def _serialize_company_report(report: CompanyReport) -> CompanyReportOut:
    return CompanyReportOut(
        id=str(report.id),
        report_type=report.report_type,
        period_start=report.period_start.isoformat(),
        period_end=report.period_end.isoformat(),
        period_label=report.period_label,
        content=report.content,
        submitted_count=report.submitted_count,
        missing_count=report.missing_count,
        needs_refresh=report.needs_refresh,
        generated_at=report.generated_at.isoformat() if report.generated_at else "",
        updated_at=report.updated_at.isoformat() if report.updated_at else "",
    )
