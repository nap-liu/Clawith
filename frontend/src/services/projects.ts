import { fetchJson } from "./api";
import type {
  ProjectBootstrapOptions,
  ProjectCreatePayload,
  ProjectCreateResponse,
  ProjectFromTemplatePayload,
  ProjectListResponse,
  ProjectOwnedAgent,
  ProjectOwnedAgentCreatePayload,
  ProjectOwnedAgentPromotion,
  ProjectOwnedAgentUpdatePayload,
  ProjectScope,
  ProjectStatus,
  ProjectSummary,
  ProjectTemplate,
  ProjectTemplateAssetSummary,
  ProjectTemplateCapability,
  ProjectTemplateFromProjectPayload,
  ProjectTemplateManifest,
  ProjectTemplateRole,
} from "../features/projects/types";

type JsonRecord = Record<string, unknown>;
type JsonCollection = JsonRecord[] | { items: JsonRecord[]; total?: number };

export type ProjectFileKind = "text" | "image" | "video" | "audio" | "binary";

export type ProjectFileContent = {
  path: string;
  name: string;
  commit: string;
  size: number;
  mime_type: string;
  kind: ProjectFileKind;
  is_text: boolean;
  is_editable: boolean;
  content: string | null;
  truncated: boolean;
  raw_url: string;
  download_url: string;
  html_preview_url: string | null;
  ticket_expires_in: number;
};

export type ProjectDirectoryArchive = {
  path: string;
  head: string;
  name: string;
  file_count: number;
  total_size: number;
  download_url: string;
  ticket_expires_in: number;
};

export type ProjectGitDiffFile = {
  path: string;
  status: "added" | "modified" | "deleted";
  additions: number | null;
  deletions: number | null;
  binary: boolean;
  original_content: string | null;
  modified_content: string | null;
  content_included: boolean;
  content_truncated: boolean;
  original_size: number;
  modified_size: number;
};

export type ProjectGitDiff = {
  commit: string;
  target_commit: string;
  parent: string | null;
  parent_commit: string | null;
  available_parent_commits: string[];
  is_root: boolean;
  path: string | null;
  patch: string;
  patch_truncated: boolean;
  patch_bytes: number;
  max_patch_bytes: number;
  files: ProjectGitDiffFile[];
  files_truncated: boolean;
};

const record = (value: unknown): JsonRecord =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
const array = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];
const string = (value: unknown, fallback = ""): string =>
  typeof value === "string" || typeof value === "number"
    ? String(value)
    : fallback;
const boolean = (value: unknown): boolean => value === true;
const number = (value: unknown, fallback = 0): number =>
  Number.isFinite(Number(value)) ? Number(value) : fallback;

function normalizeStatus(value: unknown): ProjectStatus {
  const raw = string(value, "initializing");
  if (raw === "active") return "running";
  if (raw === "draft") return "initializing";
  if (raw === "success" || raw === "succeeded") return "completed";
  return [
    "planning",
    "initializing",
    "running",
    "waiting",
    "paused",
    "completed",
    "archived",
    "failed",
  ].includes(raw)
    ? (raw as ProjectStatus)
    : "initializing";
}

function normalizeProject(value: unknown): ProjectSummary {
  const source = record(value);
  const settings = record(source.settings);
  const sharedWith = array(source.shared_with).map(record);
  const editable =
    typeof source.editable === "boolean"
      ? source.editable
      : typeof source.can_edit === "boolean"
        ? source.can_edit
        : null;
  const accessRole = ["owner", "edit", "view"].includes(
    string(source.access_role),
  )
    ? (string(source.access_role) as "owner" | "edit" | "view")
    : editable
      ? "edit"
      : "view";
  return {
    id: string(source.id),
    name: string(source.name, "未命名项目"),
    description: string(source.description) || null,
    objective: string(source.objective || source.goal),
    status: normalizeStatus(source.status),
    visibility: source.visibility === "shared" ? "shared" : "private",
    progress: Math.max(
      0,
      Math.min(100, number(source.progress ?? source.progress_percent)),
    ),
    members: array(source.members).map((item) => {
      const member = record(item);
      return {
        agent_id: string(member.agent_id || member.id),
        agent_name: string(
          member.agent_name || member.name_snapshot || member.name,
          "数字员工",
        ),
        avatar_url: string(member.avatar_url) || null,
        role: string(member.role || member.role_snapshot) || null,
        is_leader: boolean(member.is_leader),
        status: string(member.status) || null,
      };
    }),
    leader_name: string(source.leader_name) || null,
    active_agent_count: number(source.active_agent_count),
    current_signal:
      string(source.current_signal || settings.current_signal) || null,
    next_action: string(source.next_action || settings.next_action) || null,
    owner_id: string(source.owner_id || source.owner_user_id) || null,
    owner_name: string(source.owner_name) || null,
    access_role: accessRole,
    shared_with_user_ids: (array(source.shared_with_user_ids).length
      ? array(source.shared_with_user_ids)
      : sharedWith.map((item) => item.user_id || item.id)
    )
      .map((item) => string(item))
      .filter(Boolean),
    shared_with_names: array(source.shared_with_names)
      .map((item) => string(item))
      .filter(Boolean),
    execution_user_id: string(source.execution_user_id) || null,
    execution_user_name: string(source.execution_user_name) || null,
    created_at: string(source.created_at, new Date(0).toISOString()),
    updated_at: string(
      source.updated_at || source.created_at,
      new Date(0).toISOString(),
    ),
  };
}

function normalizeRole(value: unknown, index: number): ProjectTemplateRole {
  if (typeof value === "string") return { key: `role-${index}`, name: value };
  const source = record(value);
  return {
    key: string(source.key || source.id, `role-${index}`),
    name: string(source.name || source.label, `角色 ${index + 1}`),
    description: string(source.description) || null,
    required: boolean(source.required),
  };
}

function normalizeTemplateCapability(
  value: unknown,
  fallbackType?: "tool" | "mcp" | "skill",
): ProjectTemplateCapability {
  if (typeof value === "string") return { name: value, type: fallbackType };
  const source = record(value);
  const affectedMembers = array(source.affected_members).map((item) => {
    const member = record(item);
    return {
      member_id: string(member.member_id) || null,
      agent_id: string(member.agent_id || member.member_agent_id) || null,
      name: string(member.name || member.member_name),
      role: string(member.role || member.member_role) || null,
      is_active: member.is_active !== false,
    };
  });
  const rawSelected = source.selected;
  return {
    id: string(source.id) || undefined,
    binding_id:
      string(source.binding_id || source.project_capability_binding_id) ||
      undefined,
    type:
      string(source.type || source.capability_type, fallbackType) || undefined,
    key: string(source.key) || null,
    name: string(source.name || source.display_name),
    version: string(source.version) || null,
    source: string(source.source) || null,
    owner_agent_id:
      string(source.owner_agent_id || source.inherited_from_agent_id) || null,
    owner_agent_name:
      string(
        source.owner_agent_name || source.member_name || source.agent_name,
      ) || null,
    member_id: string(source.member_id) || null,
    member_agent_id: string(source.member_agent_id) || null,
    member_name:
      string(source.member_name || source.owner_agent_name) || null,
    member_role: string(source.member_role) || null,
    path: string(source.path) || null,
    is_enabled: source.is_enabled !== false,
    file_count: number(source.file_count || source.files_count),
    size_bytes: number(source.size_bytes || source.total_size_bytes),
    files_available: boolean(
      source.files_available ?? source.has_files ?? source.file_count,
    ),
    selected:
      rawSelected === undefined || rawSelected === null
        ? undefined
        : boolean(rawSelected),
    selection_state: string(source.selection_state) || undefined,
    availability: string(source.availability) || null,
    affected_members: affectedMembers,
    affected_member_count: number(
      source.affected_member_count,
      affectedMembers.length,
    ),
  };
}

function normalizeTemplateAssetSummary(
  value: unknown,
): ProjectTemplateAssetSummary {
  const source = record(value);
  return {
    file_count: number(source.file_count),
    total_file_count: number(
      source.total_file_count,
      number(source.file_count),
    ),
    digital_employee_file_count: number(source.digital_employee_file_count),
    digital_employee_count: number(source.digital_employee_count),
    total_size_bytes: number(source.total_size_bytes),
    excluded_file_count: number(source.excluded_file_count),
    skill_count: number(source.skill_count),
    mcp_server_count: number(source.mcp_server_count),
    capability_count: number(source.capability_count),
  };
}

function normalizeTemplateManifest(value: unknown): ProjectTemplateManifest {
  const source = record(value);
  const skills = array(source.skills).map((item) =>
    normalizeTemplateCapability(item, "skill"),
  );
  const mcpServers = array(source.mcp_servers).map((item) =>
    normalizeTemplateCapability(item, "mcp"),
  );
  const platformCapabilities = array(source.capabilities).length
    ? array(source.capabilities).map((item) => normalizeTemplateCapability(item))
    : mcpServers;
  const assetSummary = normalizeTemplateAssetSummary(source.asset_summary);
  return {
    roles: array(source.roles).map(normalizeRole),
    skills,
    mcp_servers: mcpServers,
    capabilities: [...skills, ...platformCapabilities],
    asset_summary: {
      ...assetSummary,
      capability_count:
        assetSummary.capability_count || platformCapabilities.length,
    },
  };
}

function normalizeTemplate(value: unknown): ProjectTemplate {
  const source = record(value);
  const definition = record(source.definition);
  const rawAssetSummary = definition.asset_summary;
  const roles = array(source.roles).length
    ? array(source.roles)
    : array(definition.roles || definition.members);
  const skills = array(source.skills).length
    ? array(source.skills)
    : array(definition.skills);
  const mcps = array(source.mcp_servers).length
    ? array(source.mcp_servers)
    : array(definition.mcp_servers);
  return {
    id: string(source.id),
    name: string(source.name, "未命名模板"),
    description: string(source.description),
    category: string(source.category, "通用"),
    author_name: string(source.author_name, "平台模板"),
    version: string(source.version, "1.0.0"),
    usage_count: number(source.usage_count),
    featured: boolean(source.featured ?? definition.featured),
    objective_hint:
      string(
        source.objective_hint || definition.goal || definition.objective,
      ) || null,
    success_criteria: array(source.success_criteria).length
      ? array(source.success_criteria)
          .map((item) => string(item))
          .filter(Boolean)
      : array(definition.success_criteria)
          .map((item) => string(item))
          .filter(Boolean),
    roles: roles.map(normalizeRole),
    skills: skills.map((item) => normalizeTemplateCapability(item)),
    mcp_servers: mcps.map((item) => normalizeTemplateCapability(item)),
    snapshot_backed:
      Boolean(rawAssetSummary) &&
      typeof rawAssetSummary === "object" &&
      !Array.isArray(rawAssetSummary),
    asset_summary: rawAssetSummary
      ? normalizeTemplateAssetSummary(rawAssetSummary)
      : null,
    created_at: string(source.created_at) || null,
    updated_at: string(source.updated_at) || null,
  };
}

function normalizeCollection(payload: JsonCollection): {
  items: JsonRecord[];
  total: number;
} {
  if (Array.isArray(payload)) return { items: payload, total: payload.length };
  return {
    items: payload.items || [],
    total: payload.total ?? payload.items?.length ?? 0,
  };
}

export const projectsApi = {
  async list(params: {
    scope: ProjectScope;
    query?: string;
    status?: string;
  }): Promise<ProjectListResponse> {
    const query = new URLSearchParams({ scope: params.scope });
    if (params.query) query.set("q", params.query);
    if (params.status && params.status !== "all")
      query.set("status", params.status);
    const response = await fetchJson<JsonCollection>(`/projects?${query}`);
    const collection = normalizeCollection(response);
    return {
      items: collection.items.map(normalizeProject),
      total: collection.total,
    };
  },

  async get(projectId: string): Promise<ProjectSummary> {
    return normalizeProject(
      await fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}`),
    );
  },

  async listTemplates(params?: {
    category?: string;
    query?: string;
  }): Promise<ProjectTemplate[]> {
    const query = new URLSearchParams();
    if (params?.category && params.category !== "all")
      query.set("category", params.category);
    if (params?.query) query.set("q", params.query);
    const response = await fetchJson<JsonCollection>(
      `/projects/templates${query.size ? `?${query}` : ""}`,
    );
    return normalizeCollection(response).items.map(normalizeTemplate);
  },

  async getTemplate(templateId: string): Promise<ProjectTemplate> {
    return normalizeTemplate(
      await fetchJson<JsonRecord>(
        `/projects/templates/${encodeURIComponent(templateId)}`,
      ),
    );
  },

  async createTemplateFromProject(
    projectId: string,
    payload: ProjectTemplateFromProjectPayload,
  ): Promise<ProjectTemplate> {
    return normalizeTemplate(
      await fetchJson<JsonRecord>(
        `/projects/${encodeURIComponent(projectId)}/templates`,
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
      ),
    );
  },

  async getTemplateManifest(
    projectId: string,
  ): Promise<ProjectTemplateManifest> {
    return normalizeTemplateManifest(
      await fetchJson<JsonRecord>(
        `/projects/${encodeURIComponent(projectId)}/template-manifest`,
      ),
    );
  },

  async bootstrapOptions(): Promise<ProjectBootstrapOptions> {
    const source = record(
      await fetchJson<JsonRecord>("/projects/bootstrap-options"),
    );
    const skills = array(source.skills).map((item) => {
      const skill = record(item);
      return {
        id: string(skill.id),
        name: string(skill.name, "未命名 Skill"),
        description: string(skill.description) || null,
        kind: "skill" as const,
        source: "project" as const,
        version: string(skill.version) || null,
        risk_level: null,
        enabled_by_default: true,
      };
    });
    const mcpServers = array(source.mcp_servers).map((item) => {
      const mcp = record(item);
      return {
        id: string(mcp.id),
        name: string(mcp.display_name || mcp.name, "未命名 MCP"),
        description: string(mcp.description) || null,
        kind: "mcp" as const,
        source: "project" as const,
        version: string(mcp.version) || null,
        risk_level: null,
        enabled_by_default: true,
      };
    });
    const normalizedCapabilities = array(source.capabilities).map((item) => {
      const capability = record(item);
      const risk = string(capability.risk_level);
      return {
        id: string(capability.id),
        capability_id: string(capability.capability_id) || null,
        name: string(capability.name || capability.display_name, "未命名能力"),
        description: string(capability.description) || null,
        internal_name:
          string(
            capability.key || capability.internal_name || capability.tool_name,
          ) || null,
        category: string(capability.category) || null,
        mcp_server_name: string(capability.mcp_server_name) || null,
        kind:
          capability.kind === "mcp" || capability.type === "mcp"
            ? ("mcp" as const)
            : capability.kind === "tool" || capability.type === "tool"
              ? ("tool" as const)
              : ("skill" as const),
        source:
          capability.source === "agent" || capability.source === "inherited"
            ? ("agent" as const)
            : ("project" as const),
        owner_agent_id:
          string(capability.owner_agent_id || capability.agent_id) || null,
        owner_agent_name:
          string(capability.owner_agent_name || capability.agent_name) || null,
        version: string(capability.version) || null,
        risk_level: ["low", "medium", "high"].includes(risk)
          ? (risk as "low" | "medium" | "high")
          : null,
        enabled_by_default: capability.enabled_by_default !== false,
      };
    });
    return {
      agents: array(source.agents).map((item) => {
        const agent = record(item);
        return {
          id: string(agent.id),
          name: string(agent.name, "未命名数字员工"),
          avatar_url: string(agent.avatar_url) || null,
          role_description: string(
            agent.role_description || agent.role,
            "项目成员",
          ),
          status: string(agent.status, "idle"),
          skill_count: number(agent.skill_count),
          mcp_count: number(agent.mcp_count),
        };
      }),
      capabilities: normalizedCapabilities.length
        ? normalizedCapabilities
        : [...skills, ...mcpServers],
      users: array(source.users).map((item) => {
        const user = record(item);
        return {
          id: string(user.id),
          name: string(user.name || user.display_name, "未命名成员"),
          email: string(user.email) || null,
          avatar_url: string(user.avatar_url) || null,
        };
      }),
    };
  },

  async create(payload: ProjectCreatePayload): Promise<ProjectCreateResponse> {
    const response = await fetchJson<JsonRecord>("/projects", {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        goal: payload.objective,
        status: "planning",
        settings: {
          git: payload.git,
          runtime: payload.runtime,
          planning: {
            state: "draft",
            intent: "discuss_with_leader_before_launch",
            launch_confirmed: false,
          },
        },
      }),
    });
    const project = normalizeProject(response);
    return { id: project.id, name: project.name, status: project.status };
  },

  async createFromTemplate(
    payload: ProjectFromTemplatePayload,
  ): Promise<ProjectCreateResponse> {
    const response = await fetchJson<JsonRecord>("/projects/from-template", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const project = normalizeProject(response);
    const setup = record(response.template_setup_summary);
    return {
      id: project.id,
      name: project.name,
      status: project.status,
      template_setup_summary: {
        restored_file_count: number(setup.restored_file_count),
        restored_digital_employee_count: number(
          setup.restored_digital_employee_count,
        ),
        restored_skill_count: number(setup.restored_skill_count),
        restored_connection_count: number(setup.restored_connection_count),
        restored_tool_count: number(setup.restored_tool_count),
      },
    };
  },

  async update(
    projectId: string,
    payload: {
      visibility?: "private" | "shared";
      shared_with_user_ids?: string[];
      execution_user_id?: string | null;
      status?: "running" | "paused";
    },
  ): Promise<ProjectSummary> {
    return normalizeProject(
      await fetchJson<JsonRecord>(
        `/projects/${encodeURIComponent(projectId)}`,
        {
          method: "PATCH",
          body: JSON.stringify(payload),
        },
      ),
    );
  },

  dashboard: (projectId: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/dashboard`,
    ),
  listProjectAgents: (projectId: string) =>
    fetchJson<ProjectOwnedAgent[]>(
      `/projects/${encodeURIComponent(projectId)}/agents`,
    ),
  getProjectAgent: (projectId: string, agentId: string) =>
    fetchJson<ProjectOwnedAgent>(
      `/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agentId)}`,
    ),
  createProjectAgent: (
    projectId: string,
    payload: ProjectOwnedAgentCreatePayload,
  ) =>
    fetchJson<ProjectOwnedAgent>(
      `/projects/${encodeURIComponent(projectId)}/agents`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  updateProjectAgent: (
    projectId: string,
    agentId: string,
    payload: ProjectOwnedAgentUpdatePayload,
  ) =>
    fetchJson<ProjectOwnedAgent>(
      `/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agentId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  deactivateProjectAgent: (
    projectId: string,
    agentId: string,
    reason?: string,
  ) =>
    fetchJson<ProjectOwnedAgent>(
      `/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agentId)}/deactivate`,
      { method: "POST", body: JSON.stringify({ reason: reason || null }) },
    ),
  restoreProjectAgent: (projectId: string, agentId: string, reason?: string) =>
    fetchJson<ProjectOwnedAgent>(
      `/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agentId)}/restore`,
      { method: "POST", body: JSON.stringify({ reason: reason || null }) },
    ),
  promoteProjectAgent: (projectId: string, agentId: string, name?: string) =>
    fetchJson<ProjectOwnedAgentPromotion>(
      `/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agentId)}/promote`,
      { method: "POST", body: JSON.stringify({ name: name || null }) },
    ),
  listMembers: (projectId: string) =>
    fetchJson<JsonRecord[]>(
      `/projects/${encodeURIComponent(projectId)}/members`,
    ),
  addMember: (
    projectId: string,
    payload: {
      agent_id: string;
      is_leader?: boolean;
      is_enabled?: boolean;
      enabled_inherited_capability_ids?: string[];
    },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/members`,
      {
        method: "POST",
        body: JSON.stringify({
          agent_id: payload.agent_id,
          is_leader: payload.is_leader ?? false,
          is_enabled: payload.is_enabled ?? true,
          enabled_inherited_capability_ids:
            payload.enabled_inherited_capability_ids ?? [],
        }),
      },
    ),
  removeMember: (projectId: string, memberId: string, reason?: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}/remove`,
      {
        method: "POST",
        body: JSON.stringify({ reason: reason || null }),
      },
    ),
  restoreMember: (projectId: string, memberId: string, reason?: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}/restore`,
      {
        method: "POST",
        body: JSON.stringify({ reason: reason || null }),
      },
    ),
  listCapabilities: (projectId: string) =>
    fetchJson<JsonRecord[]>(
      `/projects/${encodeURIComponent(projectId)}/capabilities`,
    ),
  async listAgentToolCatalog(agentId: string) {
    const encodedAgentId = encodeURIComponent(agentId);
    try {
      return await fetchJson<JsonRecord[]>(
        `/tools/agents/${encodedAgentId}/with-config`,
      );
    } catch {
      return fetchJson<JsonRecord[]>(`/tools/agents/${encodedAgentId}`);
    }
  },
  createCapability: (
    projectId: string,
    payload: {
      capability_type: "skill" | "mcp" | "tool";
      capability_id: string;
      capability_name: string;
      source: "shared" | "inherited";
      inherited_from_agent_id?: string | null;
      is_enabled: boolean;
    },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/capabilities`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  listWorkItems: (projectId: string) =>
    fetchJson<JsonRecord[]>(
      `/projects/${encodeURIComponent(projectId)}/work-items`,
    ),
  getWorkItemDetail: (projectId: string, workItemId: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(workItemId)}`,
    ),
  createWorkItem: (
    projectId: string,
    payload: {
      title: string;
      description?: string;
      assignee_agent_id?: string | null;
      status?: string;
      priority?: string;
      acceptance_criteria?: string[];
      dependency_ids?: string[];
    },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/work-items`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  patchWorkItem: (
    projectId: string,
    workItemId: string,
    payload: {
      title?: string;
      description?: string;
      assignee_agent_id?: string | null;
      status?: string;
      priority?: string;
      acceptance_criteria?: string[];
      dependency_ids?: string[];
    },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(workItemId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  listRuns: (projectId: string) =>
    fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/runs`),
  listEvents: (projectId: string) =>
    fetchJson<JsonRecord[]>(
      `/projects/${encodeURIComponent(projectId)}/events?limit=500`,
    ),
  listMilestones: (projectId: string) =>
    fetchJson<JsonRecord[]>(
      `/projects/${encodeURIComponent(projectId)}/milestones`,
    ),
  getGroupSession: (projectId: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/group-session`,
    ),
  getLeaderSession: (projectId: string) =>
    fetchJson<{
      id: string;
      project_id: string;
      agent_id: string;
      title?: string;
      source_channel?: string;
      discussion_count?: number;
    }>(`/projects/${encodeURIComponent(projectId)}/leader-session`),
  confirmKickoff: (projectId: string, confirmation?: string) =>
    fetchJson<{
      status: string;
      project_id: string;
      run_id: string;
      leader_session_id: string;
      group_session_id: string;
      leader_agent_id: string;
      awakened_agent_ids: string[];
      subagent_run_id: string;
      subagent_session_id: string;
      transcript_path: string;
      transcript_commit: string;
    }>(`/projects/${encodeURIComponent(projectId)}/kickoff/confirm`, {
      method: "POST",
      body: JSON.stringify({ confirmation: confirmation || undefined }),
    }),
  async listGroupMessages(
    projectId: string,
    sessionId: string,
    options: { before?: string; limit?: number } = {},
  ): Promise<{
    items: JsonRecord[];
    hasMore: boolean;
    nextCursor: string | null;
    turn: JsonRecord;
  }> {
    const params = new URLSearchParams({
      limit: String(Math.min(500, Math.max(1, options.limit ?? 500))),
    });
    if (options.before) params.set("before", options.before);
    const response = await fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/group-sessions/${encodeURIComponent(sessionId)}/messages?${params.toString()}`,
    );
    const items = array(response.items).map((item) => {
      const message = record(item);
      const metadata = record(message.metadata || message.message_meta);
      const subagentRuns = array(metadata.subagent_runs).map((item) => {
        const subagentRun = record(item);
        return subagentRun;
      });
      const liveMetadata = subagentRuns.length
        ? { ...metadata, subagent_runs: subagentRuns }
        : metadata;
      return {
        ...message,
        display_content: message.display_content ?? message.content ?? "",
        attachments: array(message.attachments).length
          ? message.attachments
          : array(metadata.attachments),
        sender_name: message.sender_name || metadata.sender_name,
        metadata: liveMetadata,
        message_meta: liveMetadata,
      };
    });
    return {
      items,
      hasMore: Boolean(response.has_more),
      nextCursor: string(response.next_cursor) || null,
      turn: record(response.turn),
    };
  },
  sendGroupMessage: (
    projectId: string,
    sessionId: string,
    payload: {
      content: string;
      llm_content?: string;
      mentions: string[];
      attachments: JsonRecord[];
      sender_agent_id?: string;
      client_message_id?: string;
    },
  ) =>
    fetchJson<{
      message?: JsonRecord;
      awakened_agent_ids?: string[];
      subagent_runs?: Array<{
        project_run_id?: string;
        run_id: string | null;
        session_id: string | null;
        agent_id: string;
        status: string;
        error?: string;
      }>;
      turn?: JsonRecord;
    }>(
      `/projects/${encodeURIComponent(projectId)}/group-sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          client_message_id:
            payload.client_message_id ||
            globalThis.crypto?.randomUUID?.() ||
            `${Date.now()}-${Math.random()}`,
        }),
      },
    ),
  createEvent: (
    projectId: string,
    payload: {
      event_type: string;
      summary?: string;
      work_item_id?: string;
      run_id?: string;
      actor_agent_id?: string;
      metadata?: JsonRecord;
    },
  ) =>
    fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/events`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  patchMember: (
    projectId: string,
    memberId: string,
    payload: {
      is_enabled?: boolean;
      is_leader?: boolean;
      config_snapshot?: JsonRecord;
    },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  setLeader: (projectId: string, agentId: string) =>
    fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/leader`, {
      method: "PUT",
      body: JSON.stringify({ agent_id: agentId }),
    }),
  getGit: (projectId: string, limit = 100) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git?limit=${encodeURIComponent(String(limit))}`,
    ),
  getGitDiff: (
    projectId: string,
    params: { commit: string; parent?: string; path?: string },
  ) => {
    const query = new URLSearchParams({ commit: params.commit });
    if (params.parent) query.set("parent", params.parent);
    if (params.path) query.set("path", params.path);
    return fetchJson<ProjectGitDiff>(
      `/projects/${encodeURIComponent(projectId)}/git/diff?${query}`,
    );
  },
  async listGitRemotes(
    projectId: string,
  ): Promise<Array<{ name: string; url: string }>> {
    const response = record(
      await fetchJson<JsonRecord>(
        `/projects/${encodeURIComponent(projectId)}/git/remotes`,
      ),
    );
    return array(response.items)
      .map((item) => {
        const remote = record(item);
        return { name: string(remote.name), url: string(remote.url) };
      })
      .filter((remote) => remote.name && remote.url);
  },
  putGitRemote: (projectId: string, name: string, url: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git/remotes/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify({ url }) },
    ),
  deleteGitRemote: (projectId: string, name: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git/remotes/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  cloneGitRepository: (
    projectId: string,
    payload: { url: string; branch?: string },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git/clone`,
      {
        method: "POST",
        body: JSON.stringify({
          url: payload.url,
          branch: payload.branch || undefined,
        }),
      },
    ),
  patchCapability: (
    projectId: string,
    bindingId: string,
    payload: { enabled?: boolean; is_enabled?: boolean },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/capabilities/${encodeURIComponent(bindingId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          is_enabled: payload.is_enabled ?? payload.enabled,
        }),
      },
    ),
  patchRun: (
    projectId: string,
    runId: string,
    payload: { status?: string; output?: JsonRecord; error?: string },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
    ),
  createRun: (
    projectId: string,
    payload: {
      input?: JsonRecord;
      objective?: string;
      work_item_id?: string;
      agent_id?: string;
    },
  ) =>
    fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/runs`, {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        input:
          payload.input ??
          (payload.objective ? { objective: payload.objective } : {}),
      }),
    }),
  async sendA2A(
    projectId: string,
    payload: {
      from_agent_id?: string;
      to_agent_id: string;
      message: string;
      mode?: string;
      type?: string;
    },
  ) {
    let fromAgentId = payload.from_agent_id;
    if (!fromAgentId) {
      const members = await projectsApi.listMembers(projectId);
      const source =
        members.find(
          (member) =>
            member.is_leader === true &&
            member.agent_id !== payload.to_agent_id,
        ) || members.find((member) => member.agent_id !== payload.to_agent_id);
      fromAgentId = string(source?.agent_id);
    }
    const requestedMode = payload.mode || payload.type || "notify";
    const mode =
      requestedMode === "wake" || requestedMode === "broadcast"
        ? "notify"
        : requestedMode;
    return fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/a2a`,
      {
        method: "POST",
        body: JSON.stringify({
          from_agent_id: fromAgentId,
          to_agent_id: payload.to_agent_id,
          message: payload.message,
          mode,
        }),
      },
    );
  },
  restoreCommit: (
    projectId: string,
    payload: { commit?: string; commit_hash?: string; message?: string },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git/restore`,
      {
        method: "POST",
        body: JSON.stringify({
          commit: payload.commit || payload.commit_hash,
          message: payload.message,
        }),
      },
    ),
  createBranch: (
    projectId: string,
    payload: {
      name?: string;
      branch_name?: string;
      from_commit?: string;
      commit_hash?: string;
    },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git/branches`,
      {
        method: "POST",
        body: JSON.stringify({
          name: payload.name || payload.branch_name,
          from_commit: payload.from_commit || payload.commit_hash,
        }),
      },
    ),
  getSettings: (projectId: string) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/settings`,
    ),
  updateSettings: (projectId: string, settings: JsonRecord) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/settings`,
      { method: "PATCH", body: JSON.stringify(settings) },
    ),
  listFiles: (projectId: string) =>
    fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/files`),
  getDirectoryArchive: (projectId: string, path = "") => {
    const query = new URLSearchParams({ path });
    return fetchJson<ProjectDirectoryArchive>(
      `/projects/${encodeURIComponent(projectId)}/files/archive?${query}`,
    );
  },
  getFileContent: (projectId: string, path: string, maxChars = 200_000) => {
    const query = new URLSearchParams({ path, max_chars: String(maxChars) });
    return fetchJson<ProjectFileContent>(
      `/projects/${encodeURIComponent(projectId)}/files/content?${query}`,
    );
  },
  writeFile: (projectId: string, payload: { path: string; content: string }) =>
    fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/files`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  commitFiles: (
    projectId: string,
    payload: { message: string; paths?: string[]; milestone?: boolean },
  ) =>
    fetchJson<JsonRecord>(
      `/projects/${encodeURIComponent(projectId)}/git/commit`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
};

export const projectApi = projectsApi;
