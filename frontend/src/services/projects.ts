import { fetchJson } from './api';
import type {
    ProjectBootstrapOptions,
    ProjectCreatePayload,
    ProjectCreateResponse,
    ProjectListResponse,
    ProjectScope,
    ProjectStatus,
    ProjectSummary,
    ProjectTemplate,
    ProjectTemplateCapability,
    ProjectTemplateRole,
} from '../features/projects/types';

type JsonRecord = Record<string, unknown>;
type JsonCollection = JsonRecord[] | { items: JsonRecord[]; total?: number };

export type ProjectFileKind = 'text' | 'image' | 'video' | 'audio' | 'binary';

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
    ticket_expires_in: number;
};

export type ProjectGitDiffFile = {
    path: string;
    status: 'added' | 'modified' | 'deleted';
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

const record = (value: unknown): JsonRecord => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const string = (value: unknown, fallback = ''): string => typeof value === 'string' || typeof value === 'number' ? String(value) : fallback;
const boolean = (value: unknown): boolean => value === true;
const number = (value: unknown, fallback = 0): number => Number.isFinite(Number(value)) ? Number(value) : fallback;

function normalizeStatus(value: unknown): ProjectStatus {
    const raw = string(value, 'initializing');
    if (raw === 'active') return 'running';
    if (raw === 'draft') return 'initializing';
    if (raw === 'success' || raw === 'succeeded') return 'completed';
    return ['planning', 'initializing', 'running', 'waiting', 'paused', 'completed', 'archived', 'failed'].includes(raw)
        ? raw as ProjectStatus
        : 'initializing';
}

function normalizeProject(value: unknown): ProjectSummary {
    const source = record(value);
    const settings = record(source.settings);
    const sharedWith = array(source.shared_with).map(record);
    const editable = typeof source.editable === 'boolean'
        ? source.editable
        : typeof source.can_edit === 'boolean'
            ? source.can_edit
            : null;
    return {
        id: string(source.id),
        name: string(source.name, '未命名项目'),
        description: string(source.description) || null,
        objective: string(source.objective || source.goal),
        status: normalizeStatus(source.status),
        visibility: source.visibility === 'shared' ? 'shared' : 'private',
        progress: Math.max(0, Math.min(100, number(source.progress ?? source.progress_percent))),
        members: array(source.members).map(item => {
            const member = record(item);
            return {
                agent_id: string(member.agent_id || member.id),
                agent_name: string(member.agent_name || member.name_snapshot || member.name, 'Agent'),
                avatar_url: string(member.avatar_url) || null,
                role: string(member.role || member.role_snapshot) || null,
                is_leader: boolean(member.is_leader),
                status: string(member.status) || null,
            };
        }),
        leader_name: string(source.leader_name) || null,
        active_agent_count: number(source.active_agent_count),
        current_signal: string(source.current_signal || settings.current_signal) || null,
        next_action: string(source.next_action || settings.next_action) || null,
        owner_id: string(source.owner_id || source.owner_user_id) || null,
        owner_name: string(source.owner_name) || null,
        editable,
        shared_with_user_ids: (array(source.shared_with_user_ids).length
            ? array(source.shared_with_user_ids)
            : sharedWith.map(item => item.user_id || item.id)).map(item => string(item)).filter(Boolean),
        shared_with_names: array(source.shared_with_names).map(item => string(item)).filter(Boolean),
        created_at: string(source.created_at, new Date(0).toISOString()),
        updated_at: string(source.updated_at || source.created_at, new Date(0).toISOString()),
    };
}

function normalizeRole(value: unknown, index: number): ProjectTemplateRole {
    if (typeof value === 'string') return { key: `role-${index}`, name: value };
    const source = record(value);
    return {
        key: string(source.key || source.id, `role-${index}`),
        name: string(source.name || source.label, `角色 ${index + 1}`),
        description: string(source.description) || null,
        required: boolean(source.required),
    };
}

function normalizeTemplateCapability(value: unknown): ProjectTemplateCapability {
    if (typeof value === 'string') return { name: value };
    const source = record(value);
    return {
        id: string(source.id) || undefined,
        name: string(source.name || source.display_name, '未命名能力'),
        version: string(source.version) || null,
        source: string(source.source) || null,
    };
}

function normalizeTemplate(value: unknown): ProjectTemplate {
    const source = record(value);
    const definition = record(source.definition);
    const roles = array(source.roles).length ? array(source.roles) : array(definition.roles || definition.members);
    const skills = array(source.skills).length ? array(source.skills) : array(definition.skills);
    const mcps = array(source.mcp_servers).length ? array(source.mcp_servers) : array(definition.mcp_servers);
    return {
        id: string(source.id),
        name: string(source.name, '未命名模板'),
        description: string(source.description),
        category: string(source.category, '通用'),
        author_name: string(source.author_name, 'Clawith'),
        version: string(source.version, '1.0.0'),
        usage_count: number(source.usage_count),
        featured: boolean(source.featured ?? definition.featured),
        objective_hint: string(source.objective_hint || definition.goal || definition.objective) || null,
        success_criteria: array(source.success_criteria).length
            ? array(source.success_criteria).map(item => string(item)).filter(Boolean)
            : array(definition.success_criteria).map(item => string(item)).filter(Boolean),
        roles: roles.map(normalizeRole),
        skills: skills.map(normalizeTemplateCapability),
        mcp_servers: mcps.map(normalizeTemplateCapability),
        created_at: string(source.created_at) || null,
        updated_at: string(source.updated_at) || null,
    };
}

function normalizeCollection(payload: JsonCollection): { items: JsonRecord[]; total: number } {
    if (Array.isArray(payload)) return { items: payload, total: payload.length };
    return { items: payload.items || [], total: payload.total ?? payload.items?.length ?? 0 };
}

export const projectsApi = {
    async list(params: { scope: ProjectScope; query?: string; status?: string }): Promise<ProjectListResponse> {
        const query = new URLSearchParams({ scope: params.scope });
        if (params.query) query.set('q', params.query);
        if (params.status && params.status !== 'all') query.set('status', params.status);
        const response = await fetchJson<JsonCollection>(`/projects?${query}`);
        const collection = normalizeCollection(response);
        return { items: collection.items.map(normalizeProject), total: collection.total };
    },

    async get(projectId: string): Promise<ProjectSummary> {
        return normalizeProject(await fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}`));
    },

    async listTemplates(params?: { category?: string; query?: string }): Promise<ProjectTemplate[]> {
        const query = new URLSearchParams();
        if (params?.category && params.category !== 'all') query.set('category', params.category);
        if (params?.query) query.set('q', params.query);
        const response = await fetchJson<JsonCollection>(`/projects/templates${query.size ? `?${query}` : ''}`);
        return normalizeCollection(response).items.map(normalizeTemplate);
    },

    async getTemplate(templateId: string): Promise<ProjectTemplate> {
        return normalizeTemplate(await fetchJson<JsonRecord>(`/projects/templates/${encodeURIComponent(templateId)}`));
    },

    async bootstrapOptions(): Promise<ProjectBootstrapOptions> {
        const source = record(await fetchJson<JsonRecord>('/projects/bootstrap-options'));
        const skills = array(source.skills).map(item => {
            const skill = record(item);
            return { id: string(skill.id), name: string(skill.name, '未命名 Skill'), description: string(skill.description) || null, kind: 'skill' as const, source: 'project' as const, version: string(skill.version) || null, risk_level: null, enabled_by_default: true };
        });
        const mcpServers = array(source.mcp_servers).map(item => {
            const mcp = record(item);
            return { id: string(mcp.id), name: string(mcp.display_name || mcp.name, '未命名 MCP'), description: string(mcp.description || mcp.transport) || null, kind: 'mcp' as const, source: 'project' as const, version: string(mcp.version) || null, risk_level: null, enabled_by_default: true };
        });
        const normalizedCapabilities = array(source.capabilities).map(item => {
            const capability = record(item);
            const risk = string(capability.risk_level);
            return {
                id: string(capability.id),
                capability_id: string(capability.capability_id) || null,
                name: string(capability.name || capability.display_name, '未命名能力'),
                description: string(capability.description) || null,
                kind: capability.kind === 'mcp' || capability.type === 'mcp' ? 'mcp' as const : 'skill' as const,
                source: capability.source === 'agent' || capability.source === 'inherited' ? 'agent' as const : 'project' as const,
                owner_agent_id: string(capability.owner_agent_id || capability.agent_id) || null,
                owner_agent_name: string(capability.owner_agent_name || capability.agent_name) || null,
                version: string(capability.version) || null,
                risk_level: ['low', 'medium', 'high'].includes(risk) ? risk as 'low' | 'medium' | 'high' : null,
                enabled_by_default: capability.enabled_by_default !== false,
            };
        });
        return {
            agents: array(source.agents).map(item => {
                const agent = record(item);
                return { id: string(agent.id), name: string(agent.name, '未命名 Agent'), avatar_url: string(agent.avatar_url) || null, role_description: string(agent.role_description || agent.role, '项目成员'), status: string(agent.status, 'idle'), skill_count: number(agent.skill_count), mcp_count: number(agent.mcp_count) };
            }),
            capabilities: normalizedCapabilities.length ? normalizedCapabilities : [...skills, ...mcpServers],
            users: array(source.users).map(item => {
                const user = record(item);
                return {
                    id: string(user.id),
                    name: string(user.name || user.display_name, '未命名成员'),
                    email: string(user.email) || null,
                    avatar_url: string(user.avatar_url) || null,
                };
            }),
        };
    },

    async create(payload: ProjectCreatePayload): Promise<ProjectCreateResponse> {
        const response = await fetchJson<JsonRecord>('/projects', {
            method: 'POST',
            body: JSON.stringify({
                ...payload,
                goal: payload.objective,
                status: 'planning',
                settings: {
                    git: payload.git,
                    runtime: payload.runtime,
                    planning: {
                        state: 'draft',
                        intent: 'discuss_with_leader_before_launch',
                        launch_confirmed: false,
                    },
                },
            }),
        });
        const project = normalizeProject(response);
        return { id: project.id, name: project.name, status: project.status };
    },

    async update(projectId: string, payload: { visibility?: 'private' | 'shared'; shared_with_user_ids?: string[] }): Promise<ProjectSummary> {
        return normalizeProject(await fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}`, {
            method: 'PATCH',
            body: JSON.stringify(payload),
        }));
    },

    dashboard: (projectId: string) => fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/dashboard`),
    listMembers: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/members`),
    addMember: (projectId: string, payload: { agent_id: string; is_leader?: boolean; is_enabled?: boolean; enabled_inherited_capability_ids?: string[] }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/members`, {
            method: 'POST',
            body: JSON.stringify({
                agent_id: payload.agent_id,
                is_leader: payload.is_leader ?? false,
                is_enabled: payload.is_enabled ?? true,
                enabled_inherited_capability_ids: payload.enabled_inherited_capability_ids ?? [],
            }),
        }),
    removeMember: (projectId: string, memberId: string, reason?: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}/remove`, {
            method: 'POST',
            body: JSON.stringify({ reason: reason || null }),
        }),
    restoreMember: (projectId: string, memberId: string, reason?: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}/restore`, {
            method: 'POST',
            body: JSON.stringify({ reason: reason || null }),
        }),
    listCapabilities: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/capabilities`),
    listWorkItems: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/work-items`),
    getWorkItemDetail: (projectId: string, workItemId: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(workItemId)}`),
    createWorkItem: (projectId: string, payload: { title: string; description?: string; assignee_agent_id?: string | null; status?: string; priority?: string; acceptance_criteria?: string[]; dependency_ids?: string[] }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/work-items`, { method: 'POST', body: JSON.stringify(payload) }),
    patchWorkItem: (projectId: string, workItemId: string, payload: { title?: string; description?: string; assignee_agent_id?: string | null; status?: string; priority?: string; acceptance_criteria?: string[]; dependency_ids?: string[] }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(workItemId)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    listRuns: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/runs`),
    listEvents: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/events?limit=500`),
    listMilestones: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/milestones`),
    getGroupSession: (projectId: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/group-session`),
    getLeaderSession: (projectId: string) =>
        fetchJson<{ id: string; project_id: string; agent_id: string; title?: string; source_channel?: string; discussion_count?: number }>(`/projects/${encodeURIComponent(projectId)}/leader-session`),
    confirmKickoff: (projectId: string, confirmation?: string) =>
        fetchJson<{ status: string; project_id: string; run_id: string; leader_session_id: string; group_session_id: string; leader_agent_id: string; awakened_agent_ids: string[]; subagent_run_id: string; subagent_session_id: string; transcript_path: string; transcript_commit: string }>(`/projects/${encodeURIComponent(projectId)}/kickoff/confirm`, {
            method: 'POST',
            body: JSON.stringify({ confirmation: confirmation || undefined }),
        }),
    async listGroupMessages(projectId: string, sessionId: string): Promise<JsonRecord[]> {
        const [response, projectRuns] = await Promise.all([
            fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/group-sessions/${encodeURIComponent(sessionId)}/messages?limit=500`),
            fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/runs`).catch(() => []),
        ]);
        const currentRunById = new Map<string, JsonRecord>(projectRuns.map(run => [
            string(run.id || run.run_id),
            record(run),
        ] as [string, JsonRecord]));
        return array(response.items).map(item => {
            const message = record(item);
            const metadata = record(message.metadata || message.message_meta);
            const subagentRuns = array(metadata.subagent_runs).map(item => {
                const subagentRun = record(item);
                const current = currentRunById.get(string(subagentRun.project_run_id));
                return current ? {
                    ...subagentRun,
                    status: current.status || subagentRun.status,
                    error: current.error || subagentRun.error,
                } : subagentRun;
            });
            const liveMetadata = subagentRuns.length ? { ...metadata, subagent_runs: subagentRuns } : metadata;
            return {
                ...message,
                display_content: message.display_content ?? message.content ?? '',
                attachments: array(message.attachments).length ? message.attachments : array(metadata.attachments),
                sender_name: message.sender_name || metadata.sender_name,
                metadata: liveMetadata,
                message_meta: liveMetadata,
            };
        });
    },
    sendGroupMessage: (projectId: string, sessionId: string, payload: { content: string; llm_content?: string; mentions: string[]; attachments: JsonRecord[]; sender_agent_id?: string }) =>
        fetchJson<{ message?: JsonRecord; awakened_agent_ids?: string[]; subagent_runs?: Array<{ project_run_id?: string; run_id: string | null; session_id: string | null; agent_id: string; status: string; error?: string }> }>(`/projects/${encodeURIComponent(projectId)}/group-sessions/${encodeURIComponent(sessionId)}/messages`, {
            method: 'POST',
            body: JSON.stringify({ ...payload, client_message_id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}` }),
        }),
    createEvent: (projectId: string, payload: { event_type: string; summary?: string; work_item_id?: string; run_id?: string; actor_agent_id?: string; metadata?: JsonRecord }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/events`, { method: 'POST', body: JSON.stringify(payload) }),
    patchMember: (projectId: string, memberId: string, payload: { is_enabled?: boolean; is_leader?: boolean; config_snapshot?: JsonRecord }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    setLeader: (projectId: string, agentId: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/leader`, { method: 'PUT', body: JSON.stringify({ agent_id: agentId }) }),
    getGit: (projectId: string, limit = 100) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git?limit=${encodeURIComponent(String(limit))}`),
    getGitDiff: (projectId: string, params: { commit: string; parent?: string; path?: string }) => {
        const query = new URLSearchParams({ commit: params.commit });
        if (params.parent) query.set('parent', params.parent);
        if (params.path) query.set('path', params.path);
        return fetchJson<ProjectGitDiff>(`/projects/${encodeURIComponent(projectId)}/git/diff?${query}`);
    },
    async listGitRemotes(projectId: string): Promise<Array<{ name: string; url: string }>> {
        const response = record(await fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/remotes`));
        return array(response.items).map(item => {
            const remote = record(item);
            return { name: string(remote.name), url: string(remote.url) };
        }).filter(remote => remote.name && remote.url);
    },
    putGitRemote: (projectId: string, name: string, url: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/remotes/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ url }) }),
    deleteGitRemote: (projectId: string, name: string) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/remotes/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    cloneGitRepository: (projectId: string, payload: { url: string; branch?: string }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/clone`, { method: 'POST', body: JSON.stringify({ url: payload.url, branch: payload.branch || undefined }) }),
    patchCapability: (projectId: string, bindingId: string, payload: { enabled?: boolean; is_enabled?: boolean }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/capabilities/${encodeURIComponent(bindingId)}`, { method: 'PATCH', body: JSON.stringify({ is_enabled: payload.is_enabled ?? payload.enabled }) }),
    patchRun: (projectId: string, runId: string, payload: { status?: string; output?: JsonRecord; error?: string }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    createRun: (projectId: string, payload: { input?: JsonRecord; objective?: string; work_item_id?: string; agent_id?: string }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/runs`, { method: 'POST', body: JSON.stringify({ ...payload, input: payload.input ?? (payload.objective ? { objective: payload.objective } : {}) }) }),
    async sendA2A(projectId: string, payload: { from_agent_id?: string; to_agent_id: string; message: string; mode?: string; type?: string }) {
        let fromAgentId = payload.from_agent_id;
        if (!fromAgentId) {
            const members = await projectsApi.listMembers(projectId);
            const source = members.find(member => member.is_leader === true && member.agent_id !== payload.to_agent_id) || members.find(member => member.agent_id !== payload.to_agent_id);
            fromAgentId = string(source?.agent_id);
        }
        const requestedMode = payload.mode || payload.type || 'notify';
        const mode = requestedMode === 'wake' || requestedMode === 'broadcast' ? 'notify' : requestedMode;
        return fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/a2a`, { method: 'POST', body: JSON.stringify({ from_agent_id: fromAgentId, to_agent_id: payload.to_agent_id, message: payload.message, mode }) });
    },
    restoreCommit: (projectId: string, payload: { commit?: string; commit_hash?: string; message?: string }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/restore`, { method: 'POST', body: JSON.stringify({ commit: payload.commit || payload.commit_hash, message: payload.message }) }),
    createBranch: (projectId: string, payload: { name?: string; branch_name?: string; from_commit?: string; commit_hash?: string }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/branches`, { method: 'POST', body: JSON.stringify({ name: payload.name || payload.branch_name, from_commit: payload.from_commit || payload.commit_hash }) }),
    getSettings: (projectId: string) => fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/settings`),
    updateSettings: (projectId: string, settings: JsonRecord) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/settings`, { method: 'PATCH', body: JSON.stringify(settings) }),
    listFiles: (projectId: string) => fetchJson<JsonRecord[]>(`/projects/${encodeURIComponent(projectId)}/files`),
    getFileContent: (projectId: string, path: string, maxChars = 200_000) => {
        const query = new URLSearchParams({ path, max_chars: String(maxChars) });
        return fetchJson<ProjectFileContent>(`/projects/${encodeURIComponent(projectId)}/files/content?${query}`);
    },
    writeFile: (projectId: string, payload: { path: string; content: string }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/files`, { method: 'PUT', body: JSON.stringify(payload) }),
    commitFiles: (projectId: string, payload: { message: string; paths?: string[]; milestone?: boolean }) =>
        fetchJson<JsonRecord>(`/projects/${encodeURIComponent(projectId)}/git/commit`, { method: 'POST', body: JSON.stringify(payload) }),
};

export const projectApi = projectsApi;
