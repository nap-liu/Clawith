import { fetchJson } from "./api";
import type {
  JsonCollection,
  JsonRecord,
  ProjectDirectoryArchive,
  ProjectFileContent,
  ProjectGitDiff,
} from "./projects/normalizers";
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
  ProjectSummary,
  ProjectTemplate,
  ProjectTemplateFromProjectPayload,
  ProjectTemplateManifest,
} from "../features/projects/types";
import {
  array,
  normalizeBootstrapOptions,
  normalizeCollection,
  normalizeGroupMessagesPage,
  normalizeProject,
  normalizeTemplate,
  normalizeTemplateManifest,
  number,
  record,
  string,
} from "./projects/normalizers";
export type {
  JsonRecord,
  JsonCollection,
  ProjectDirectoryArchive,
  ProjectFileContent,
  ProjectFileKind,
  ProjectGitDiff,
  ProjectGitDiffFile,
} from "./projects/normalizers";

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

  async delete(projectId: string): Promise<void> {
    await fetchJson<void>(`/projects/${encodeURIComponent(projectId)}`, {
      method: "DELETE",
    });
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

  async createTemplateEditor(templateId: string): Promise<ProjectSummary> {
    return normalizeProject(
      await fetchJson<JsonRecord>(
        `/projects/templates/${encodeURIComponent(templateId)}/editor`,
        { method: "POST" },
      ),
    );
  },

  async updateTemplateFromProject(
    templateId: string,
    projectId: string,
  ): Promise<ProjectTemplate> {
    return normalizeTemplate(
      await fetchJson<JsonRecord>(
        `/projects/templates/${encodeURIComponent(templateId)}/from-project/${encodeURIComponent(projectId)}`,
        { method: "PUT" },
      ),
    );
  },

  async deleteTemplate(templateId: string): Promise<void> {
    await fetchJson<void>(
      `/projects/templates/${encodeURIComponent(templateId)}`,
      { method: "DELETE" },
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
    return normalizeBootstrapOptions(
      await fetchJson<JsonRecord>("/projects/bootstrap-options"),
    );
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
    return normalizeGroupMessagesPage(response);
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
