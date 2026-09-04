import type { MCPServerEditorDraftOverride } from "../../types/mcpServer";

export type ProjectScope = "mine" | "shared" | "running" | "archived" | "all";

export type ProjectStatus =
  | "planning"
  | "initializing"
  | "running"
  | "waiting"
  | "paused"
  | "completed"
  | "archived"
  | "failed";

export type ProjectVisibility = "private" | "shared";

export type ProjectAccessRole = "owner" | "edit" | "view";

export interface ProjectMemberSummary {
  agent_id: string;
  agent_name: string;
  avatar_url?: string | null;
  role?: string | null;
  is_leader: boolean;
  status?: string | null;
}

export interface ProjectSummary {
  id: string;
  name: string;
  description?: string | null;
  objective: string;
  status: ProjectStatus;
  visibility: ProjectVisibility;
  progress: number;
  members: ProjectMemberSummary[];
  leader_name?: string | null;
  active_agent_count?: number;
  current_signal?: string | null;
  next_action?: string | null;
  owner_id?: string | null;
  owner_name?: string | null;
  access_role: ProjectAccessRole;
  is_project_owner?: boolean;
  can_delete?: boolean;
  can_manage_sharing?: boolean;
  can_manage_execution_user?: boolean;
  shared_with_user_ids?: string[];
  shared_with_names?: string[];
  execution_user_id?: string | null;
  execution_user_name?: string | null;
  settings?: Record<string, unknown>;
  updated_at: string;
  created_at: string;
}

export interface ProjectListResponse {
  items: ProjectSummary[];
  total: number;
}

export interface ProjectTemplateCapability {
  id?: string;
  binding_id?: string;
  type?: "tool" | "mcp" | "skill" | string;
  key?: string | null;
  name: string;
  version?: string | null;
  source?: string | null;
  owner_agent_id?: string | null;
  owner_agent_name?: string | null;
  member_id?: string | null;
  member_agent_id?: string | null;
  member_name?: string | null;
  member_role?: string | null;
  path?: string | null;
  is_enabled?: boolean;
  file_count?: number;
  size_bytes?: number;
  files_available?: boolean;
  selected?: boolean;
  selection_state?: "selected" | "unselected" | string;
  availability?: "available" | "missing" | "restricted" | string | null;
  affected_members?: ProjectTemplateAffectedMember[];
  affected_member_count?: number;
}

export interface ProjectTemplateAffectedMember {
  member_id?: string | null;
  agent_id?: string | null;
  name: string;
  role?: string | null;
  is_active?: boolean;
}

export interface ProjectTemplateRole {
  key: string;
  name: string;
  description?: string | null;
  required?: boolean;
}

export interface ProjectTemplateAssetSummary {
  file_count: number;
  total_file_count: number;
  digital_employee_file_count: number;
  digital_employee_count: number;
  total_size_bytes: number;
  excluded_file_count: number;
  skill_count: number;
  mcp_server_count: number;
  capability_count: number;
}

export interface ProjectTemplateManifest {
  roles: ProjectTemplateRole[];
  skills: ProjectTemplateCapability[];
  mcp_servers: ProjectTemplateCapability[];
  capabilities: ProjectTemplateCapability[];
  asset_summary: ProjectTemplateAssetSummary;
}

export interface ProjectTemplate {
  id: string;
  name: string;
  description: string;
  category: string;
  author_name?: string | null;
  version: string;
  usage_count: number;
  featured?: boolean;
  objective_hint?: string | null;
  success_criteria?: string[];
  roles: ProjectTemplateRole[];
  skills: ProjectTemplateCapability[];
  mcp_servers: ProjectTemplateCapability[];
  snapshot_backed: boolean;
  asset_summary: ProjectTemplateAssetSummary | null;
  tenant_id?: string | null;
  created_by_user_id?: string | null;
  can_edit?: boolean;
  can_delete?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProjectTemplateFromProjectPayload {
  name?: string;
  description?: string;
  category?: string;
  version?: string;
  is_published?: boolean;
  included_skill_binding_ids?: string[];
}

export interface ProjectAgentOption {
  id: string;
  name: string;
  avatar_url?: string | null;
  role_description: string;
  status: string;
  skill_count?: number;
  mcp_count?: number;
  primary_model_id?: string | null;
  fallback_model_id?: string | null;
  temperature?: number | null;
  reasoning_effort?: string | null;
  max_tool_rounds?: number | null;
}

export interface ProjectAgentToolOption {
  id: string;
  name: string;
  display_name: string;
  description?: string | null;
  category?: string | null;
  type?: string | null;
  icon?: string | null;
  mcp_server_id?: string | null;
  mcp_server_name?: string | null;
  enabled: boolean;
  can_disable: boolean;
  agent_config?: Record<string, unknown>;
  config_schema?: Record<string, unknown> | null;
  source?: string | null;
  agent_tool_source?: string | null;
  installed_by_agent_id?: string | null;
}

export interface ProjectAgentSettingsDraft {
  config_snapshot: {
    primary_model_id?: string | null;
    fallback_model_id?: string | null;
    temperature?: number | null;
    reasoning_effort?: string | null;
    max_tool_rounds?: number | string | null;
    project_instruction?: string;
  };
  tools: ProjectAgentToolOption[];
  mcp_server_overrides: Record<string, MCPServerEditorDraftOverride>;
  skill_capability_ids: string[];
}

export interface ProjectOwnedAgent {
  id: string;
  project_id: string;
  member_id: string;
  source_agent_id?: string | null;
  name: string;
  role_description: string;
  avatar_url?: string | null;
  status: string;
  agent_dir: string;
  soul: string;
  core_memory: string;
  is_leader: boolean;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProjectOwnedAgentCreatePayload {
  source_agent_id?: string | null;
  name?: string | null;
  role_description?: string | null;
  soul?: string | null;
  core_memory?: string | null;
  is_leader?: boolean;
}

export interface ProjectOwnedAgentUpdatePayload {
  name?: string;
  role_description?: string;
  soul?: string;
  core_memory?: string;
}

export interface ProjectOwnedAgentPromotion {
  id: string;
  name: string;
  role_description: string;
  source_project_id: string;
  source_project_agent_id: string;
}

export type CapabilitySource = "project" | "agent";
export type CapabilityKind = "skill" | "mcp" | "tool";
export type CapabilityOrigin = "company" | "digital_employee" | "market";

export interface ProjectCapabilityOption {
  id: string;
  capability_id?: string | null;
  name: string;
  description?: string | null;
  kind: CapabilityKind;
  source: CapabilitySource;
  origin?: CapabilityOrigin | null;
  owner_agent_id?: string | null;
  owner_agent_name?: string | null;
  tool_count?: number;
  version?: string | null;
  risk_level?: "low" | "medium" | "high" | null;
  enabled_by_default?: boolean;
}

export interface ProjectBootstrapOptions {
  agents: ProjectAgentOption[];
  tools: ProjectAgentToolOption[];
  capabilities: ProjectCapabilityOption[];
  users: ProjectShareTarget[];
}

export interface ProjectShareTarget {
  id: string;
  name: string;
  email?: string | null;
  avatar_url?: string | null;
}

export interface ProjectCreatePayload {
  name: string;
  description: string;
  objective: string;
  success_criteria: string[];
  template_id?: string | null;
  members: Array<{
    agent_id: string;
    is_leader: boolean;
    enabled_inherited_capability_ids: string[];
    settings?: {
      config_snapshot: ProjectAgentSettingsDraft["config_snapshot"];
      tools: Array<{
        tool_id: string;
        enabled: boolean;
        config?: Record<string, unknown>;
      }>;
      mcp_server_overrides: Array<
        Omit<MCPServerEditorDraftOverride, "credential_state"> & {
          server_id: string;
        }
      >;
      skill_capability_ids: string[];
    };
  }>;
  shared_capability_ids: string[];
  git: {
    repository_mode: "managed" | "external";
    repository_url?: string | null;
    branch_policy: "project" | "work_item";
  };
  runtime: {
    mode: "economy" | "balanced" | "quality";
    monthly_budget: number;
    approval_policy: "risk" | "all_writes" | "manual";
  };
  visibility: ProjectVisibility;
  shared_with_user_ids: string[];
}

export interface ProjectCreateResponse {
  id: string;
  status: ProjectStatus;
  name: string;
  template_setup_summary?: ProjectTemplateSetupSummary | null;
}

export interface ProjectTemplateSetupSummary {
  restored_file_count: number;
  restored_digital_employee_count: number;
  restored_skill_count: number;
  restored_connection_count: number;
  restored_tool_count: number;
}

export interface ProjectMemberCreateOverride {
  agent_id: string;
  is_leader: boolean;
  enabled_inherited_capability_ids: string[];
  settings?: ProjectCreatePayload["members"][number]["settings"];
}

export interface ProjectCapabilityCreateOverride {
  capability_type: CapabilityKind;
  capability_id: string;
  capability_name: string;
  source: "shared" | "inherited";
  inherited_from_agent_id?: string | null;
  is_enabled: boolean;
}

export interface ProjectFromTemplatePayload {
  template_id: string;
  name: string;
  description?: string;
  visibility: ProjectVisibility;
  overrides: {
    members?: ProjectMemberCreateOverride[];
    capabilities?: ProjectCapabilityCreateOverride[];
    shared_with_user_ids?: string[];
  };
}
