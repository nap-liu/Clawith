import type {
  ProjectBootstrapOptions,
  ProjectStatus,
  ProjectSummary,
  ProjectTemplate,
  ProjectTemplateAssetSummary,
  ProjectTemplateCapability,
  ProjectTemplateManifest,
  ProjectTemplateRole,
} from "../../features/projects/types";

export type JsonRecord = Record<string, unknown>;
export type JsonCollection = JsonRecord[] | { items: JsonRecord[]; total?: number };

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

export const record = (value: unknown): JsonRecord =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
export const array = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];
export const string = (value: unknown, fallback = ""): string =>
  typeof value === "string" || typeof value === "number"
    ? String(value)
    : fallback;
export const boolean = (value: unknown): boolean => value === true;
export const number = (value: unknown, fallback = 0): number =>
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

export function normalizeProject(value: unknown): ProjectSummary {
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
    is_project_owner: boolean(source.is_project_owner),
    can_delete:
      typeof source.can_delete === "boolean"
        ? source.can_delete
        : accessRole === "owner",
    can_manage_sharing:
      typeof source.can_manage_sharing === "boolean"
        ? source.can_manage_sharing
        : accessRole === "owner",
    can_manage_execution_user: boolean(source.can_manage_execution_user),
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
    settings,
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

export function normalizeTemplateManifest(
  value: unknown,
): ProjectTemplateManifest {
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

export function normalizeTemplate(value: unknown): ProjectTemplate {
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
    tenant_id: string(source.tenant_id) || null,
    created_by_user_id:
      string(source.created_by_user_id || source.author_user_id) || null,
    can_edit: boolean(source.can_edit),
    can_delete: boolean(source.can_delete),
    created_at: string(source.created_at) || null,
    updated_at: string(source.updated_at) || null,
  };
}

export function normalizeCollection(payload: JsonCollection): {
  items: JsonRecord[];
  total: number;
} {
  if (Array.isArray(payload)) return { items: payload, total: payload.length };
  return {
    items: payload.items || [],
    total: payload.total ?? payload.items?.length ?? 0,
  };
}

export function normalizeBootstrapOptions(
  value: unknown,
): ProjectBootstrapOptions {
  const source = record(value);
  const normalizedCapabilities = array(source.capabilities).map((item) => {
    const capability = record(item);
    const risk = string(capability.risk_level);
    return {
      id: string(capability.id),
      capability_id: string(capability.capability_id) || null,
      name: string(capability.name || capability.display_name, "未命名能力"),
      description: string(capability.description) || null,
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
      origin:
        capability.origin === "company" ||
        capability.origin === "digital_employee" ||
        capability.origin === "market"
          ? capability.origin
          : capability.source === "agent" ||
              capability.source === "inherited"
            ? ("digital_employee" as const)
            : capability.kind === "mcp" || capability.type === "mcp"
              ? ("company" as const)
              : ("market" as const),
      owner_agent_id:
        string(capability.owner_agent_id || capability.agent_id) || null,
      owner_agent_name:
        string(capability.owner_agent_name || capability.agent_name) || null,
      version: string(capability.version) || null,
      tool_count: number(capability.tool_count),
      risk_level: ["low", "medium", "high"].includes(risk)
        ? (risk as "low" | "medium" | "high")
        : null,
      enabled_by_default: capability.enabled_by_default === true,
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
        primary_model_id: string(agent.primary_model_id) || null,
        fallback_model_id: string(agent.fallback_model_id) || null,
        max_tool_rounds:
          typeof agent.max_tool_rounds === "number"
            ? agent.max_tool_rounds
            : null,
      };
    }),
    tools: array(source.tools).map((item) => {
      const tool = record(item);
      return {
        id: string(tool.id),
        name: string(tool.name),
        display_name: string(tool.display_name || tool.name),
        description: string(tool.description) || null,
        category: string(tool.category) || null,
        type: string(tool.type) || null,
        icon: string(tool.icon) || null,
        mcp_server_id: string(tool.mcp_server_id) || null,
        mcp_server_name: string(tool.mcp_server_name) || null,
        enabled: tool.enabled !== false,
        can_disable: tool.can_disable !== false,
        agent_config: record(tool.agent_config),
        config_schema: Object.keys(record(tool.config_schema)).length
          ? record(tool.config_schema)
          : null,
        source: string(tool.source) || null,
        agent_tool_source: string(tool.agent_tool_source) || null,
        installed_by_agent_id: string(tool.installed_by_agent_id) || null,
      };
    }),
    capabilities: normalizedCapabilities,
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
}

export function normalizeGroupMessagesPage(response: JsonRecord): {
  items: JsonRecord[];
  hasMore: boolean;
  nextCursor: string | null;
  turn: JsonRecord;
} {
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
}
