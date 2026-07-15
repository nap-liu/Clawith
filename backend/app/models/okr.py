"""OKR system models.

Core tables powering the OKR feature:
  - OKRObjective        : Company / user / agent level Objectives
  - OKRKeyResult        : Key Results hanging under an Objective
  - OKRAlignment        : Many-to-many alignment relationships between O/KRs
  - OKRProgressLog      : Full history of KR progress changes
  - WorkReport          : Legacy daily / weekly work reports
  - MemberDailyReport   : Member-level final daily submissions
  - CompanyReport       : Company-level daily / weekly / monthly summaries
  - OKRSettings         : Per-tenant OKR feature configuration (single row)
"""

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    CheckConstraint,
    Index,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OKRObjective(Base):
    """An Objective at company, user, or agent level.

    owner_type:
      - "company" : company-wide O (owner_id is NULL)
      - "user"    : individual human O  (owner_id = User.id)
      - "agent"   : individual Agent O  (owner_id = Agent.id)
    """

    __tablename__ = "okr_objectives"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Owner — who owns this Objective
    owner_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "company" | "user" | "agent"
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )  # NULL for company-level O
    # Canonical owner identity. Both NULL means a company-level objective;
    # exactly one identifies a natural person or digital employee.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Period
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # draft | active | completed | archived

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "NOT (owner_user_id IS NOT NULL AND owner_agent_id IS NOT NULL)",
            name="ck_okr_objective_single_owner",
        ),
    )


class OKRKeyResult(Base):
    """A measurable Key Result under an Objective.

    focus_ref links to an Agent's Focus file name (e.g. "content_quality"),
    enabling the OKR Agent to trace progress through Reflection Sessions.
    """

    __tablename__ = "okr_key_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    objective_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("okr_objectives.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)

    # Measurement
    target_value: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    current_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str | None] = mapped_column(String(50))  # e.g. "%", "followers", "万元"

    # Optional link to an Agent's focus file (by basename without .md)
    focus_ref: Mapped[str | None] = mapped_column(String(200))

    # Status computed or set by OKR Agent
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="on_track"
    )  # on_track | at_risk | behind | completed

    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OKRAlignment(Base):
    """Many-to-many alignment between Objectives or Key Results.

    Allows an individual O to align to multiple company KRs, or to peer Os.
    source → target means "source is aligned to / contributes toward target".
    """

    __tablename__ = "okr_alignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Source entity (the lower-level O or KR that is aligning upward/sideward)
    source_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "objective" | "key_result"
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Target entity (the higher-level or peer O/KR being aligned to)
    target_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "objective" | "key_result"
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_id", "target_type", "target_id",
            name="uq_okr_alignment",
        ),
    )


class OKRProgressLog(Base):
    """Immutable log entry every time a KR's current_value changes.

    Enables full progress curve visualization and audit trail.
    """

    __tablename__ = "okr_progress_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kr_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("okr_key_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    previous_value: Mapped[float] = mapped_column(Float, nullable=False)
    new_value: Mapped[float] = mapped_column(Float, nullable=False)

    # Who / what triggered the update
    source: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # "okr_agent" | "manual" | "self_report"

    # Optional free-text note extracted from conversation by the OKR Agent
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkReport(Base):
    """A daily or weekly work report submitted by a user or agent.

    Content is collected by the OKR Agent through conversation and
    structured with LLM extraction. Not named OKRReport because work
    reports are general progress updates, not OKR-specific documents.
    """

    __tablename__ = "work_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Author (human user or agent)
    author_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "user" | "agent"
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )

    report_type: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # "daily" | "weekly"

    # The date this report refers to (for daily: the day; for weekly: the Monday of that week)
    period_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Markdown-formatted report content (structured by OKR Agent or written manually)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # How this report was created
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="okr_agent_collected"
    )  # "okr_agent_collected" | "manual"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL) <> (agent_id IS NOT NULL)",
            name="ck_work_report_single_author",
        ),
    )


class MemberDailyReport(Base):
    """The final normalized daily report for a single member on a specific day.

    The stored content is the OKR Agent's final distilled version, not the
    member's raw chat transcript. Raw discussions remain in chat history.
    """

    __tablename__ = "member_daily_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "user" | "agent"
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    content: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )  # final concise report, target length <= 2000 chars
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="submitted"
    )  # submitted | late | revised | incomplete
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="okr_agent_assisted"
    )  # okr_agent_assisted | manual
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_type", "member_id", "report_date",
            name="uq_member_daily_report",
        ),
        Index(
            "uq_member_daily_report_user",
            "tenant_id",
            "user_id",
            "report_date",
            unique=True,
            postgresql_where=sa.text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_member_daily_report_agent",
            "tenant_id",
            "agent_id",
            "report_date",
            unique=True,
            postgresql_where=sa.text("agent_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(user_id IS NOT NULL) <> (agent_id IS NOT NULL)",
            name="ck_member_daily_report_single_member",
        ),
    )


@event.listens_for(OKRObjective, "before_insert")
@event.listens_for(OKRObjective, "before_update")
def _sync_objective_owner(_mapper, _connection, target: OKRObjective) -> None:
    if target.owner_user_id is not None:
        target.owner_agent_id = None
        target.owner_type = "user"
        target.owner_id = target.owner_user_id
    elif target.owner_agent_id is not None:
        target.owner_user_id = None
        target.owner_type = "agent"
        target.owner_id = target.owner_agent_id
    elif target.owner_type == "user" and target.owner_id is not None:
        target.owner_user_id = target.owner_id
    elif target.owner_type == "agent" and target.owner_id is not None:
        target.owner_agent_id = target.owner_id
    else:
        target.owner_type = "company"
        target.owner_id = None


@event.listens_for(MemberDailyReport, "before_insert")
@event.listens_for(MemberDailyReport, "before_update")
def _sync_daily_report_member(_mapper, _connection, target: MemberDailyReport) -> None:
    if target.user_id is not None:
        target.agent_id = None
        target.member_type = "user"
        target.member_id = target.user_id
    elif target.agent_id is not None:
        target.user_id = None
        target.member_type = "agent"
        target.member_id = target.agent_id
    elif target.member_type == "user" and target.member_id is not None:
        target.user_id = target.member_id
    elif target.member_type == "agent" and target.member_id is not None:
        target.agent_id = target.member_id


@event.listens_for(WorkReport, "before_insert")
@event.listens_for(WorkReport, "before_update")
def _sync_work_report_author(_mapper, _connection, target: WorkReport) -> None:
    if target.user_id is not None:
        target.agent_id = None
        target.author_type = "user"
        target.author_id = target.user_id
    elif target.agent_id is not None:
        target.user_id = None
        target.author_type = "agent"
        target.author_id = target.agent_id
    elif target.author_type == "user" and target.author_id is not None:
        target.user_id = target.author_id
    elif target.author_type == "agent" and target.author_id is not None:
        target.agent_id = target.author_id


class CompanyReport(Base):
    """A company-level derived OKR report.

    Reports are generated from lower-level data:
      - daily   <- member_daily_reports
      - weekly  <- company daily reports
      - monthly <- company weekly reports
    """

    __tablename__ = "company_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_type: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # daily | weekly | monthly
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    period_label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    submitted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "report_type", "period_start", "period_end",
            name="uq_company_report_period",
        ),
    )


class OKRSettings(Base):
    """Per-tenant OKR configuration. Always exactly one row per tenant.

    Created with defaults when OKR is first enabled; never deleted.
    """

    __tablename__ = "okr_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Master switch — all OKR functionality gates on this
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # First time OKR was enabled for this tenant. Once set, the OKR cadence is
    # treated as locked so historical periods keep a stable reporting meaning.
    first_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Daily report collection (OKR Agent sends message to all members at daily_report_time)
    daily_report_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Time in HH:MM format (24-hour, interpreted in OKR Agent's configured timezone)
    daily_report_time: Mapped[str] = mapped_column(
        String(5), nullable=False, default="18:00"
    )
    daily_report_skip_non_workdays: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # Weekly report collection
    weekly_report_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # 0=Monday ... 6=Sunday
    weekly_report_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4
    )  # Friday by default

    # OKR cycle definition
    period_frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, default="quarterly"
    )  # "quarterly" | "monthly" | "custom"
    period_length_days: Mapped[int | None] = mapped_column(
        Integer
    )  # used only when period_frequency == "custom"

    # The canonical OKR Agent for this company (linked during seeder)
    okr_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
