"""Digital Employee (Agent) models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, column_property

from sqlalchemy import select

from app.database import Base

# Default context window size — used as the fallback when
# agent.context_window_size is None or 0 across all channels.
# Centralizing this constant prevents inconsistent fallback values
# (see: https://github.com/dataelement/Clawith/issues/238).
DEFAULT_CONTEXT_WINDOW_SIZE = 100


class Agent(Base):
    """Digital employee (Agent) instance.

    agent_type: 'native' (platform-hosted) or 'openclaw' (remote OpenClaw bot).
    """

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("scope IN ('standard', 'project')", name="ck_agents_scope"),
        CheckConstraint(
            "(scope = 'standard' AND project_id IS NULL) OR "
            "(scope = 'project' AND project_id IS NOT NULL AND agent_dir IS NOT NULL)",
            name="ck_agents_project_scope",
        ),
        CheckConstraint(
            "daily_memory_load_days >= 0 AND daily_memory_load_days <= 30",
            name="ck_agents_daily_memory_load_days",
        ),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="ck_agents_temperature",
        ),
        CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN "
            "('none','minimal','low','medium','high','xhigh','max')",
            name="ck_agents_reasoning_effort",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role_description: Mapped[str] = mapped_column(String(500), default="")
    bio: Mapped[str | None] = mapped_column(Text)
    welcome_message: Mapped[str | None] = mapped_column(Text, default=None)

    # Ownership
    creator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))

    # Project-native agents are standard Agent runtimes whose mutable assets
    # belong to one project. They may be created from scratch or copied from a
    # source Agent; the source row always remains unchanged.
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="standard", server_default="standard")
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Agent type: 'native' (platform-hosted LLM) or 'openclaw' (remote OpenClaw bot)
    agent_type: Mapped[str] = mapped_column(String(20), default="native", nullable=False)
    # API key hash for OpenClaw gateway authentication
    api_key_hash: Mapped[str | None] = mapped_column(String(128))
    # Last time OpenClaw polled the gateway (online status indicator)
    openclaw_last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Runtime
    status: Mapped[str] = mapped_column(
        Enum("creating", "running", "idle", "stopped", "error", name="agent_status_enum", create_constraint=False),
        default="creating",
        nullable=False,
    )
    container_id: Mapped[str | None] = mapped_column(String(100))
    container_port: Mapped[int | None] = mapped_column(Integer)

    # LLM config
    primary_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))
    fallback_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Autonomy policy (L1/L2/L3)
    autonomy_policy: Mapped[dict] = mapped_column(
        JSON,
        default={
            "read_files": "L1",
            "write_workspace_files": "L2",
            "send_feishu_message": "L2",
            "send_external_message": "L3",
            "manage_relationships": "L2",
            "modify_soul": "L3",
            "access_business_system_read": "L2",
            "access_business_system_write": "L3",
            "delete_files": "L3",
            "create_calendar_event": "L2",
            "financial_operations": "L3",
            "install_skill_from_market": "L1",
            "publish_skill_to_market": "L3",
        },
    )

    # Token usage control
    max_tokens_per_day: Mapped[int | None] = mapped_column(BigInteger)
    max_tokens_per_month: Mapped[int | None] = mapped_column(BigInteger)
    tokens_used_today: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_used_month: Mapped[int] = mapped_column(BigInteger, default=0)
    last_daily_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_monthly_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_used_total: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens_today: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens_month: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens_total: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens_today: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens_month: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_creation_tokens_total: Mapped[int] = mapped_column(BigInteger, default=0)
    context_window_size: Mapped[int] = mapped_column(Integer, default=100)
    # Number of recent Daily Memory files injected into every turn. Zero
    # disables Daily Memory loading while retaining Core Memory.
    daily_memory_load_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    max_tool_rounds: Mapped[int] = mapped_column(Integer, default=50)

    # Trigger limits (per-agent, configurable from Settings UI)
    max_triggers: Mapped[int] = mapped_column(Integer, default=20)
    min_poll_interval_min: Mapped[int] = mapped_column(Integer, default=5)
    webhook_rate_limit: Mapped[int] = mapped_column(Integer, default=5)
    webhook_queue_max: Mapped[int] = mapped_column(Integer, default=1000)  # queue/merge 队列上限, 满了背压

    # Expiry control
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_expired: Mapped[bool] = mapped_column(Boolean, default=False)

    # Soft-delete control — soft-deleted agents are hidden from all listings
    # (see build_visible_agents_query) and stopped, but remain restorable.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # System agent flag — system agents (e.g. OKR Agent) cannot be deleted by users
    # and their system triggers are protected from user deletion.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Access model:
    # - company: all platform users in the tenant can access; all tenant agents can interact.
    # - private: only the creator can use/manage; hidden from Plaza.
    # - custom: explicit user access rows; agent-to-agent access is configured via Relationships.
    # Compatibility display projections; grants are the authorization source.
    access_mode: Mapped[str] = mapped_column(String(20), default="company", nullable=False)
    company_access_level: Mapped[str] = mapped_column(String(20), default="use", nullable=False)

    # Daily LLM call limit
    llm_calls_today: Mapped[int] = mapped_column(Integer, default=0)
    max_llm_calls_per_day: Mapped[int] = mapped_column(Integer, default=1000)
    llm_calls_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Template
    template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_templates.id"))

    # Heartbeat (proactive agent awareness)
    heartbeat_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    heartbeat_interval_minutes: Mapped[int] = mapped_column(Integer, default=240)
    heartbeat_active_hours: Mapped[str] = mapped_column(String(20), default="09:00-18:00")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Timezone (IANA format, e.g. "Asia/Shanghai"). None = inherit from tenant.
    timezone: Mapped[str | None] = mapped_column(String(50), default=None, nullable=True)

    # Legacy field name: controls public Agent-authored progress in external IM,
    # never raw provider reasoning.
    im_thinking_output_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    creator: Mapped["User"] = relationship("User", back_populates="created_agents", foreign_keys=[creator_id])
    project: Mapped["Project | None"] = relationship("Project", foreign_keys=[project_id])
    source_agent: Mapped["Agent | None"] = relationship("Agent", remote_side=[id], foreign_keys=[source_agent_id])

    @staticmethod
    def project_agent_dir(agent_id: uuid.UUID) -> str:
        """Return the repository-relative directory for a project Agent."""
        return f".agents/{agent_id}"

    @property
    def has_api_key(self) -> bool:
        """Whether this agent has an API key configured."""
        return bool(self.api_key_hash)

    permissions: Mapped[list["AgentPermission"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        foreign_keys="Task.agent_id",
    )
    channel_config: Mapped["ChannelConfig | None"] = relationship(back_populates="agent", uselist=False)
    primary_model: Mapped["LLMModel | None"] = relationship(foreign_keys=[primary_model_id])
    fallback_model: Mapped["LLMModel | None"] = relationship(foreign_keys=[fallback_model_id])


class AgentPermission(Base):
    """Access permission for a digital employee."""

    __tablename__ = "agent_permissions"
    __table_args__ = (
        Index(
            "uq_agent_permissions_subject",
            "agent_id",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_where=text("scope_id IS NOT NULL"),
        ),
        Index(
            "uq_agent_permissions_null_scope",
            "agent_id",
            "scope_type",
            unique=True,
            postgresql_where=text("scope_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(
        Enum("company", "department", "user", name="permission_scope_enum"),
        nullable=False,
    )
    # scope_id: null for company, department_id/user_id for department/user scope
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # access_level: 'use' = task/chat/tool/skill/workspace only, 'manage' = full access
    access_level: Mapped[str] = mapped_column(String(20), default="use", nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="permissions")


Agent.company_grant_level = column_property(
    select(AgentPermission.access_level)
    .where(AgentPermission.agent_id == Agent.id, AgentPermission.scope_type == "company")
    .correlate_except(AgentPermission)
    .scalar_subquery()
)


class AgentTemplate(Base):
    """Digital employee template for quick creation."""

    __tablename__ = "agent_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="🤖")
    category: Mapped[str] = mapped_column(String(50), default="general")
    soul_template: Mapped[str] = mapped_column(Text, default="")
    default_skills: Mapped[list] = mapped_column(JSON, default=[])
    # Smithery server IDs (e.g. "shibui/finance") to auto-import + bind when
    # an agent is created from this template. The new-agent handler in
    # api.agents.create_agent calls import_mcp_from_smithery for each, using
    # the system-level Smithery key, then assigns the resulting Tool(s) via
    # AgentTool. Idempotent: existing Tool with same mcp_server_url is reused.
    default_mcp_servers: Mapped[list] = mapped_column(JSON, default=[])
    default_autonomy_policy: Mapped[dict] = mapped_column(JSON, default={})
    # Talent Market card: 2-4 short capability bullets shown under the role
    capability_bullets: Mapped[list] = mapped_column(JSON, default=[])
    # Founding onboarding ritual. Used as the system prompt when the very first
    # human opens a chat with an agent created from this template — it guides
    # the agent to collect project context, introduce itself, and suggest a
    # first task. Every subsequent user meets the agent via a simpler built-in
    # welcoming prompt (see app.services.onboarding), not this content.
    bootstrap_content: Mapped[str | None] = mapped_column(Text, default=None)
    is_builtin: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentUserOnboarding(Base):
    """Tracks the per-(agent, user) onboarding ritual.

    A ``pending`` row is the backend's short-lived atomic greeting claim.
    Later phases mean the greeting has fired, so clients must not auto-trigger
    another one. The ``phase`` column also lets Web continue with real user
    replies that calibrate the agent before marking onboarding complete.
    """

    __tablename__ = "agent_user_onboardings"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    onboarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(
        String(32),
        default="completed",
        server_default="completed",
        nullable=False,
    )


from app.models.channel_config import ChannelConfig  # noqa: E402, F401
from app.models.llm import LLMModel  # noqa: E402, F401
from app.models.project import Project  # noqa: E402, F401
from app.models.task import Task  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
