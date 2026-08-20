export type ProjectScope = 'mine' | 'shared' | 'running' | 'archived';

export type ProjectStatus =
    | 'initializing'
    | 'running'
    | 'waiting'
    | 'paused'
    | 'completed'
    | 'archived'
    | 'failed';

export type ProjectVisibility = 'private' | 'shared';

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
    created_at?: string | null;
    updated_at?: string | null;
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

export type CapabilitySource = 'project' | 'agent';
export type CapabilityKind = 'skill' | 'mcp';

export interface ProjectCapabilityOption {
    id: string;
    name: string;
    description?: string | null;
    kind: CapabilityKind;
    source: CapabilitySource;
    owner_agent_id?: string | null;
    owner_agent_name?: string | null;
    version?: string | null;
    risk_level?: 'low' | 'medium' | 'high' | null;
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
        repository_mode: 'managed' | 'external';
        repository_url?: string | null;
        branch_policy: 'project' | 'work_item';
    };
    runtime: {
        mode: 'economy' | 'balanced' | 'quality';
        monthly_budget: number;
        approval_policy: 'risk' | 'all_writes' | 'manual';
    };
    visibility: ProjectVisibility;
    shared_with_user_ids: string[];
}

export interface ProjectCreateResponse {
    id: string;
    status: ProjectStatus;
    name: string;
}
