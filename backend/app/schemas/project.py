"""Request/response contracts for AI-native projects."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectAgentToolSetting(BaseModel):
    tool_id: uuid.UUID
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectAgentMCPServerOverrideSetting(BaseModel):
    server_id: uuid.UUID
    system_prompt_block: str | None = None
    url_template: str | None = None
    headers_template: dict | None = None
    credential_template: str | None = None
    command_template: str | None = None
    args_template: list[str] | None = None
    env_template: dict | None = None


class ProjectAgentInitialSettings(BaseModel):
    config_snapshot: dict = Field(default_factory=dict)
    tools: list[ProjectAgentToolSetting] = Field(default_factory=list)
    mcp_server_overrides: list[ProjectAgentMCPServerOverrideSetting] = Field(default_factory=list)
    mcp_capability_ids: list[uuid.UUID] = Field(default_factory=list)
    skill_capability_ids: list[uuid.UUID] = Field(default_factory=list)


class ProjectMemberCreate(BaseModel):
    agent_id: uuid.UUID
    is_leader: bool = False
    is_enabled: bool = True
    enabled_inherited_capability_ids: list[uuid.UUID] = Field(default_factory=list)
    settings: ProjectAgentInitialSettings | None = None


class ProjectCapabilityCreate(BaseModel):
    capability_type: Literal["skill", "mcp", "tool"]
    capability_id: uuid.UUID | None = None
    capability_name: str | None = None
    source: Literal["shared", "inherited"] = "shared"
    inherited_from_agent_id: uuid.UUID | None = None
    is_enabled: bool = True
    scope: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    goal: str = ""
    objective: str | None = None
    success_criteria: list[str] = Field(default_factory=list)
    visibility: Literal["private", "shared"] = "private"
    shared_with_user_ids: list[uuid.UUID] = Field(default_factory=list)
    shared_user_ids: list[uuid.UUID] = Field(default_factory=list)
    status: Literal["planning", "initializing"] = "planning"
    template_id: uuid.UUID | None = None
    members: list[ProjectMemberCreate] = Field(default_factory=list)
    capabilities: list[ProjectCapabilityCreate] = Field(default_factory=list)
    shared_capability_ids: list[uuid.UUID] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)
    git: dict = Field(default_factory=dict)
    runtime: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_frontend_payload(self):
        if not self.goal and self.objective:
            self.goal = self.objective
        if not self.shared_with_user_ids and self.shared_user_ids:
            self.shared_with_user_ids = self.shared_user_ids
        if self.git or self.runtime:
            self.settings = {**self.settings, "git": self.git, "runtime": self.runtime}
        return self


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    goal: str | None = None
    success_criteria: list[str] | None = None
    visibility: Literal["private", "shared"] | None = None
    status: (
        Literal["planning", "initializing", "running", "waiting", "paused", "completed", "archived", "failed"] | None
    ) = None
    settings: dict | None = None
    shared_with_user_ids: list[uuid.UUID] | None = None
    execution_user_id: uuid.UUID | None = None


class ProjectSettingsUpdate(BaseModel):
    """Project-local runtime and governance settings.

    Extra keys are intentionally accepted because project templates can define
    domain-specific policies. Git repository ownership/configuration is guarded
    by the endpoint and cannot be changed through this contract.
    """

    model_config = ConfigDict(extra="allow")

    policies: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None
    current_signal: str | None = Field(default=None, max_length=500)
    next_action: str | None = Field(default=None, max_length=500)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    owner_user_id: uuid.UUID
    template_id: uuid.UUID | None
    name: str
    description: str
    goal: str
    success_criteria: list
    visibility: str
    status: str
    settings: dict
    created_at: datetime
    updated_at: datetime


class ProjectMemberUpdate(BaseModel):
    is_enabled: bool | None = None
    is_leader: bool | None = None
    config_snapshot: dict | None = None


class ProjectMemberToolUpdate(BaseModel):
    tool_id: uuid.UUID
    enabled: bool


class ProjectSkillBackfillRequest(BaseModel):
    action: Literal["dry_run", "apply", "rollback"] = "dry_run"
    operation_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_operation(self):
        if self.action == "rollback" and self.operation_id is None:
            raise ValueError("operation_id is required for rollback")
        if self.action != "rollback" and self.operation_id is not None:
            raise ValueError("operation_id is only supported for rollback")
        return self


class ProjectMemberLifecycleRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ProjectMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    name_snapshot: str
    role_snapshot: str
    config_snapshot: dict
    source_updated_at: datetime | None
    is_leader: bool
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ProjectAgentCreate(BaseModel):
    """Create a project-owned Agent, optionally from a visible standard Agent."""

    source_agent_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=2, max_length=100)
    role_description: str | None = Field(default=None, max_length=500)
    soul: str | None = Field(default=None, max_length=200_000)
    core_memory: str | None = Field(default=None, max_length=200_000)
    is_leader: bool = False

    @model_validator(mode="after")
    def require_name_for_blank_agent(self):
        if self.source_agent_id is None and not (self.name or "").strip():
            raise ValueError("name is required when source_agent_id is not provided")
        return self


class ProjectAgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    role_description: str | None = Field(default=None, max_length=500)
    soul: str | None = Field(default=None, max_length=200_000)
    core_memory: str | None = Field(default=None, max_length=200_000)


class ProjectAgentLifecycleRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ProjectAgentPromoteRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)


class ProjectAgentOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    member_id: uuid.UUID
    source_agent_id: uuid.UUID | None = None
    name: str
    role_description: str
    avatar_url: str | None = None
    status: str
    agent_dir: str
    soul: str
    core_memory: str
    is_leader: bool
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ProjectAgentPromotionOut(BaseModel):
    id: uuid.UUID
    name: str
    role_description: str
    source_project_id: uuid.UUID
    source_project_agent_id: uuid.UUID


class ProjectAccessGrantCreate(BaseModel):
    user_id: uuid.UUID
    role: Literal["view", "edit"] = "view"


class ProjectAccessGrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    created_by_user_id: uuid.UUID | None
    created_at: datetime


class LeaderUpdate(BaseModel):
    agent_id: uuid.UUID


class CapabilityUpdate(BaseModel):
    is_enabled: bool | None = None
    scope: dict | None = None
    config: dict | None = None


class CapabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    capability_type: str
    capability_id: uuid.UUID | None
    capability_name: str
    key: str | None = None
    asset_id: str | None = None
    affected_member_count: int = 0
    description: str = ""
    availability: Literal["available", "missing", "restricted"] = "missing"
    version: str | None = None
    file_count: int = 0
    size_bytes: int = 0
    source: str
    inherited_from_agent_id: uuid.UUID | None
    is_enabled: bool
    scope: dict
    config: dict
    created_at: datetime
    updated_at: datetime


class ProjectTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    category: str = "general"
    version: str = "1.0.0"
    is_published: bool = False
    definition: dict = Field(default_factory=dict)


class ProjectTemplateFromProjectCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    category: str = "general"
    version: str = "1.0.0"
    is_published: bool = False
    included_skill_binding_ids: list[uuid.UUID] = Field(default_factory=list)


class ProjectTemplateFromProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    category: str | None = None
    version: str | None = None
    is_published: bool | None = None
    included_skill_binding_ids: list[uuid.UUID] = Field(default_factory=list)


class ProjectTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    created_by_user_id: uuid.UUID | None
    name: str
    description: str
    category: str
    version: str
    is_published: bool
    definition: dict
    created_at: datetime
    updated_at: datetime


class ProjectFromTemplateCreate(BaseModel):
    template_id: uuid.UUID
    name: str | None = None
    description: str | None = None
    visibility: Literal["private", "shared"] = "private"
    overrides: dict = Field(default_factory=dict)


class WorkItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    parent_id: uuid.UUID | None = None
    assignee_agent_id: uuid.UUID | None = None
    status: Literal["backlog", "todo", "in_progress", "review", "blocked", "done"] = "backlog"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependency_ids: list[uuid.UUID] = Field(default_factory=list)
    due_at: datetime | None = None


class WorkItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    assignee_agent_id: uuid.UUID | None = None
    status: Literal["backlog", "todo", "in_progress", "review", "blocked", "done"] | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    acceptance_criteria: list[str] | None = None
    dependency_ids: list[uuid.UUID] | None = None
    due_at: datetime | None = None


class WorkItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    parent_id: uuid.UUID | None
    assignee_agent_id: uuid.UUID | None
    created_by_user_id: uuid.UUID | None
    created_by_agent_id: uuid.UUID | None
    title: str
    description: str
    status: str
    priority: str
    acceptance_criteria: list
    dependency_ids: list
    due_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProjectRunCreate(BaseModel):
    work_item_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    trigger_type: Literal["manual", "leader", "a2a", "schedule", "retry"] = "manual"
    input: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_actionable_execution(self):
        """Reject unscoped Runs that can only produce generic progress prose."""

        if self.work_item_id is not None:
            return self
        actionable = any(str(self.input.get(field) or "").strip() for field in ("task", "objective", "message"))
        if not actionable:
            raise ValueError("work_item_id or an explicit task/objective/message is required")
        return self


class ProjectRunUpdate(BaseModel):
    status: Literal["queued", "running", "waiting", "succeeded", "failed", "cancelled"] | None = None
    output: dict | None = None
    error: str | None = None


class ProjectFrozenModelSummary(BaseModel):
    name: str | None = None
    availability: Literal["available", "missing"]


class ProjectFrozenMemberConfigSummary(BaseModel):
    primary_model: ProjectFrozenModelSummary | None = None
    fallback_model: ProjectFrozenModelSummary | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tool_rounds: int | None = None
    has_project_instruction: bool = False


class ProjectFrozenCapabilityItem(BaseModel):
    type: Literal["tool", "mcp", "skill", "other"]
    key: str
    name: str
    source: Literal["project", "member"]


class ProjectFrozenCapabilitySummary(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    items: list[ProjectFrozenCapabilityItem] = Field(default_factory=list)


class ProjectFrozenMemberSummary(BaseModel):
    project_member_id: uuid.UUID
    agent_id: uuid.UUID
    name: str | None = None
    responsibility: str = ""
    is_leader: bool = False
    configuration: ProjectFrozenMemberConfigSummary
    capabilities: ProjectFrozenCapabilitySummary


class ProjectRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    work_item_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    initiated_by_user_id: uuid.UUID | None
    status: str
    trigger_type: str
    title: str | None = None
    input: dict
    output: dict
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Stable execution identity. ``session_id`` is the exact conversation a
    # viewer should open (the A2A timeline for A2A runs, otherwise the durable
    # child session); ``subagent_session_id`` is always the worker child.
    project_member_id: uuid.UUID | None = None
    agent_name: str | None = None
    member_snapshot: ProjectFrozenMemberSummary | None = None
    session_id: uuid.UUID | None = None
    subagent_session_id: uuid.UUID | None = None
    group_session_id: uuid.UUID | None = None


class ProjectMilestoneOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    project_id: uuid.UUID
    commit: str
    short_commit: str
    message: str
    author: str | None = None
    created_at: datetime
    commit_created_at: datetime | str | None = None
    run_id: uuid.UUID | None = None
    work_item_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    subagent_session_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    paths: list[str] = Field(default_factory=list)
    changed: bool | None = None
    related_run_ids: list[uuid.UUID] = Field(default_factory=list)
    related_work_item_ids: list[uuid.UUID] = Field(default_factory=list)


class ProjectRunMemberSnapshotOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    project_member_id: uuid.UUID
    agent_id: uuid.UUID
    is_leader: bool
    member_snapshot: ProjectFrozenMemberSummary
    created_at: datetime


class ProjectEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=100)
    summary: str = Field(default="", max_length=500)
    work_item_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    actor_agent_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)


class ProjectEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    work_item_id: uuid.UUID | None
    run_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    actor_agent_id: uuid.UUID | None
    from_agent_id: uuid.UUID | None
    to_agent_id: uuid.UUID | None
    event_type: str
    summary: str
    event_metadata: dict
    member_snapshot: ProjectFrozenMemberSummary | None = None
    created_at: datetime


class WorkItemDetailOut(BaseModel):
    work_item: WorkItemOut
    runs: list[ProjectRunOut] = Field(default_factory=list)
    sessions: list[dict] = Field(default_factory=list)
    events: list[ProjectEventOut] = Field(default_factory=list)
    commits: list[dict] = Field(default_factory=list)
    files: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)


class A2AWakeRequest(BaseModel):
    from_agent_id: uuid.UUID
    to_agent_id: uuid.UUID
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1)
    mode: Literal["consult", "delegate", "review"]
    expected_output: str = Field(min_length=1, max_length=10_000)
    work_item_id: uuid.UUID
    new_conversation: bool = False


class ProjectGroupMessageCreate(BaseModel):
    content: str = Field(default="", max_length=100_000)
    # Attachment extraction can be substantially larger than the compact
    # timeline label. It is execution-only input and is not duplicated in
    # ChatMessage.message_meta.
    llm_content: str | None = Field(default=None, max_length=250_000)
    mentions: list[uuid.UUID] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list, max_length=10)
    work_item_id: uuid.UUID | None = None
    sender_agent_id: uuid.UUID | None = None
    client_message_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_content_or_attachment(self):
        if not self.content.strip() and not (self.llm_content or "").strip() and not self.attachments:
            raise ValueError("content, llm_content, or attachments is required")
        return self


class ProjectKickoffConfirm(BaseModel):
    confirmation: str | None = Field(default=None, max_length=10_000)


class GitRestoreRequest(BaseModel):
    commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    message: str | None = None


class GitBranchRequest(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")
    from_commit: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,64}$")


class GitRemoteRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class GitCloneRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    branch: str | None = Field(default=None, min_length=1, max_length=255)


class ProjectFileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str


class GitCommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    paths: list[str] | None = None
    milestone: bool = False

    @model_validator(mode="after")
    def validate_paths(self):
        if self.paths is not None and not self.paths:
            raise ValueError("paths must be omitted or contain at least one path")
        if self.paths and len(self.paths) != len(set(self.paths)):
            raise ValueError("paths must not contain duplicates")
        return self
