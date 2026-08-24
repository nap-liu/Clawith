export type ProjectScope = "mine" | "shared" | "running" | "archived";

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
  editable?: boolean | null;
  shared_with_user_ids?: string[];
  shared_with_names?: string[];
  updated_at: string;
  created_at: string;
}

export interface ProjectListResponse {
  items: ProjectSummary[];
  total: number;
}

export interface ProjectTemplateCapability {
  id?: string;
  name: string;
  version?: string | null;
  source?: string | null;
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
}

export interface ProjectTemplateManifest {
  roles: ProjectTemplateRole[];
  skills: ProjectTemplateCapability[];
  mcp_servers: ProjectTemplateCapability[];
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
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProjectTemplateFromProjectPayload {
  name?: string;
  description?: string;
  category?: string;
  version?: string;
  is_published?: boolean;
}

export interface ProjectAgentOption {
  id: string;
  name: string;
  avatar_url?: string | null;
  role_description: string;
  status: string;
  skill_count?: number;
  mcp_count?: number;
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

export interface ProjectCapabilityOption {
  id: string;
  capability_id?: string | null;
  name: string;
  description?: string | null;
  kind: CapabilityKind;
  source: CapabilitySource;
  owner_agent_id?: string | null;
  owner_agent_name?: string | null;
  version?: string | null;
  risk_level?: "low" | "medium" | "high" | null;
  enabled_by_default?: boolean;
}

export interface ProjectBootstrapOptions {
  agents: ProjectAgentOption[];
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
