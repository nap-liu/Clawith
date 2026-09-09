import type { Agent, ExploreAgentPage, Task } from "../../types";
import { API_BASE, request, uploadFile, uploadFileWithProgress } from "./core";

export const agentApi = {
  list: (tenantId?: string) =>
    request<Agent[]>(`/agents/${tenantId ? `?tenant_id=${tenantId}` : ""}`),

  explore: (params: {
    tenantId?: string;
    search?: string;
    status?: string;
    page?: number;
    pageSize?: number;
    signal?: AbortSignal;
  } = {}) => {
    const query = new URLSearchParams();
    if (params.tenantId) query.set("tenant_id", params.tenantId);
    if (params.search) query.set("search", params.search);
    if (params.status) query.set("status", params.status);
    query.set("page", String(params.page || 1));
    query.set("page_size", String(params.pageSize || 24));
    return request<ExploreAgentPage>(`/agents/explore?${query.toString()}`, {
      signal: params.signal,
    });
  },

  async exploreAll(params: {
    tenantId: string;
    signal?: AbortSignal;
  }): Promise<ExploreAgentPage & { incomplete: boolean; load_error?: string }> {
    const pageSize = 500;
    const maxConsistencyAttempts = 2;

    consistencyAttempts: for (
      let consistencyAttempt = 1;
      consistencyAttempt <= maxConsistencyAttempts;
      consistencyAttempt += 1
    ) {
      const items: ExploreAgentPage["items"] = [];
      const seen = new Set<string>();
      let page = 1;
      let expectedTotal: number | null = null;
      let counts: ExploreAgentPage["counts"] = {
        all: 0,
        running: 0,
        idle: 0,
        stopped: 0,
      };

      for (;;) {
        let response: ExploreAgentPage;
        try {
          response = await agentApi.explore({
            tenantId: params.tenantId,
            page,
            pageSize,
            signal: params.signal,
          });
        } catch (error) {
          if (params.signal?.aborted || items.length === 0) throw error;
          return {
            items,
            total: expectedTotal ?? items.length,
            page,
            page_size: pageSize,
            has_more: true,
            counts,
            incomplete: true,
            load_error: error instanceof Error ? error.message : String(error),
          };
        }

        expectedTotal ??= response.total;
        counts = response.counts;
        let added = 0;
        for (const item of response.items) {
          if (seen.has(item.id)) continue;
          seen.add(item.id);
          items.push(item);
          added += 1;
        }

        if (response.total !== expectedTotal) {
          if (consistencyAttempt < maxConsistencyAttempts) {
            continue consistencyAttempts;
          }
          return {
            ...response,
            items,
            page_size: pageSize,
            incomplete: true,
            load_error: "Agent directory total changed while loading",
          };
        }

        if (!response.has_more) {
          if (seen.size !== expectedTotal) {
            if (consistencyAttempt < maxConsistencyAttempts) {
              continue consistencyAttempts;
            }
            return {
              ...response,
              items,
              page_size: pageSize,
              incomplete: true,
              load_error: "Agent directory unique item count did not match total",
            };
          }
          return {
            ...response,
            items,
            page_size: pageSize,
            incomplete: false,
          };
        }

        if (added === 0 || page >= 10_000) {
          return {
            ...response,
            items,
            page_size: pageSize,
            incomplete: true,
            load_error: "Agent directory pagination did not make progress",
          };
        }
        page += 1;
      }
    }

    throw new Error("Agent directory consistency retry loop exhausted");
  },

  get: (id: string) => request<Agent>(`/agents/${id}`),
  create: (data: any) =>
    request<any>("/agents/", { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: Partial<Agent>) =>
    request<Agent>(`/agents/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  delete: (id: string) => request<void>(`/agents/${id}`, { method: "DELETE" }),
  start: (id: string) =>
    request<Agent>(`/agents/${id}/start`, { method: "POST" }),
  stop: (id: string) =>
    request<Agent>(`/agents/${id}/stop`, { method: "POST" }),
  metrics: (id: string) => request<any>(`/agents/${id}/metrics`),
  collaborators: (id: string) => request<any[]>(`/agents/${id}/collaborators`),
  templates: () => request<any[]>("/agents/templates"),
  generateApiKey: (id: string) =>
    request<{ api_key: string; message: string }>(`/agents/${id}/api-key`, {
      method: "POST",
    }),
  gatewayMessages: (id: string) =>
    request<any[]>(`/agents/${id}/gateway-messages`),
  resolveConfirmation: (
    id: string,
    cid: string,
    value: string,
    label?: string,
  ) =>
    request<any>(`/agents/${id}/confirmations/${cid}/resolve`, {
      method: "POST",
      body: JSON.stringify({ value, label }),
    }),
};

export const chatSessionApi = {
  list: (
    agentId: string,
    options: {
      scope?: "mine" | "all";
      source_channel?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    params.set("scope", options.scope || "mine");
    if (options.source_channel) params.set("source_channel", options.source_channel);
    if (options.limit != null) params.set("limit", String(options.limit));
    if (options.offset != null) params.set("offset", String(options.offset));
    return request<any[]>(`/agents/${agentId}/sessions?${params.toString()}`);
  },

  listPage: (
    agentId: string,
    options: {
      scope?: "mine" | "all";
      source_channel?: string;
      limit?: number;
      offset?: number;
      cursor?: string;
      exclude_mine?: boolean;
      signal?: AbortSignal;
    } = {},
  ) => {
    const params = new URLSearchParams();
    params.set("scope", options.scope || "mine");
    params.set("paginated", "true");
    if (options.source_channel) params.set("source_channel", options.source_channel);
    if (options.limit != null) params.set("limit", String(options.limit));
    if (options.offset != null) params.set("offset", String(options.offset));
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.exclude_mine) params.set("exclude_mine", "true");
    return request<{
      items: any[];
      has_more: boolean;
      next_offset: number | null;
      next_cursor: string | null;
    }>(`/agents/${agentId}/sessions?${params.toString()}`, {
      signal: options.signal,
    });
  },

  get: (agentId: string, sessionId: string, taskId?: string) =>
    request<Record<string, any> & { view_scope: "mine" | "all" }>(
      `/agents/${agentId}/sessions/${sessionId}${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''}`,
    ),

  execution: (agentId: string, sessionId: string) =>
    request<Record<string, any> | null>(
      `/agents/${agentId}/sessions/${sessionId}/execution`,
    ),

  create: (
    agentId: string,
    data: { title?: string; source_channel?: string },
  ) =>
    request<any>(`/agents/${agentId}/sessions`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  messages: (agentId: string, sessionId: string, limit = 200) =>
    request<any[]>(
      `/agents/${agentId}/sessions/${sessionId}/messages?limit=${limit}`,
    ),

  messagesPage: (
    agentId: string,
    sessionId: string,
    options: { limit?: number; before?: string } = {},
  ) => {
    const params = new URLSearchParams({
      limit: String(Math.min(500, Math.max(1, options.limit ?? 500))),
      paginated: "true",
    });
    if (options.before) params.set("before", options.before);
    return request<{
      items: any[];
      has_more: boolean;
      next_cursor: string | null;
    }>(
      `/agents/${agentId}/sessions/${sessionId}/messages?${params.toString()}`,
    );
  },

  async allMessages(agentId: string, sessionId: string): Promise<any[]> {
    const pages: any[][] = [];
    const seenMessages = new Set<string>();
    const seenCursors = new Set<string>();
    let before: string | undefined;
    for (;;) {
      const page = await chatSessionApi.messagesPage(agentId, sessionId, { before });
      pages.push(page.items.filter((message) => {
        if (seenMessages.has(message.id)) return false;
        seenMessages.add(message.id);
        return true;
      }));
      if (!page.has_more) return pages.reverse().flat();
      if (!page.next_cursor || seenCursors.has(page.next_cursor)) {
        throw new Error("Session history pagination did not advance");
      }
      seenCursors.add(page.next_cursor);
      before = page.next_cursor;
    }
  },
};

export const taskApi = {
  list: (agentId: string, status?: string, type?: string) => {
    const params = new URLSearchParams();
    if (status) params.set("status_filter", status);
    if (type) params.set("type_filter", type);
    return request<Task[]>(`/agents/${agentId}/tasks/?${params}`);
  },

  create: (agentId: string, data: any) =>
    request<Task>(`/agents/${agentId}/tasks/`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  update: (
    agentId: string,
    taskId: string,
    data: Partial<Task> & { expected_execution_user_id?: string | null },
  ) =>
    request<Task>(`/agents/${agentId}/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  getLogs: (agentId: string, taskId: string) =>
    request<
      { id: string; task_id: string; content: string; created_at: string }[]
    >(`/agents/${agentId}/tasks/${taskId}/logs`),

  trigger: (agentId: string, taskId: string) =>
    request<any>(`/agents/${agentId}/tasks/${taskId}/trigger`, {
      method: "POST",
    }),
};

export const fileApi = {
  list: (agentId: string, path: string = "") =>
    request<any[]>(
      `/agents/${agentId}/files/?path=${encodeURIComponent(path)}`,
    ),
  read: (agentId: string, path: string) =>
    request<{ path: string; content: string }>(
      `/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`,
    ),
  write: (agentId: string, path: string, content: string) =>
    request(
      `/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
      },
    ),
  autosave: (
    agentId: string,
    path: string,
    content: string,
    sessionId?: string | null,
  ) =>
    request<{ status: string; path: string; revision_id?: string }>(
      `/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          content,
          autosave: true,
          session_id: sessionId || undefined,
        }),
      },
    ),
  delete: (agentId: string, path: string) =>
    request(
      `/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`,
      {
        method: "DELETE",
      },
    ),
  preview: (agentId: string, path: string) =>
    request<any>(
      `/agents/${agentId}/files/preview?path=${encodeURIComponent(path)}`,
    ),
  lock: (agentId: string, path: string, sessionId?: string | null) =>
    request<any>(`/agents/${agentId}/files/locks`, {
      method: "POST",
      body: JSON.stringify({ path, session_id: sessionId || undefined }),
    }),
  unlock: (agentId: string, path: string) =>
    request<any>(
      `/agents/${agentId}/files/locks?path=${encodeURIComponent(path)}`,
      {
        method: "DELETE",
      },
    ),
  revisions: (agentId: string, path: string) =>
    request<any[]>(
      `/agents/${agentId}/files/revisions?path=${encodeURIComponent(path)}`,
    ),
  restoreRevision: (agentId: string, revisionId: string) =>
    request<any>(`/agents/${agentId}/files/restore`, {
      method: "POST",
      body: JSON.stringify({ revision_id: revisionId }),
    }),
  upload: (
    agentId: string,
    file: File,
    path: string = "workspace/knowledge_base",
    onProgress?: (pct: number) => void,
  ) =>
    onProgress
      ? uploadFileWithProgress(
          `/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`,
          file,
          onProgress,
        ).promise
      : uploadFile(
          `/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`,
          file,
        ),
  importSkill: (agentId: string, skillId: string) =>
    request<any>(`/agents/${agentId}/files/import-skill`, {
      method: "POST",
      body: JSON.stringify({ skill_id: skillId }),
    }),
  createPlaybackTicket: (agentId: string, path: string, messageId: string) =>
    request<{
      playback_session_id: string;
      playback_url: string;
      mime_type: string;
      size_bytes: number;
      absolute_expires_at: number;
    }>(`/agents/${agentId}/files/playback-ticket`, {
      method: "POST",
      body: JSON.stringify({ path, message_id: messageId }),
    }),
  playbackStatus: (agentId: string, sessionId: string, signature: string) =>
    request<{ status: string; absolute_expires_at?: number }>(
      `/agents/${agentId}/files/playback/${encodeURIComponent(sessionId)}/status?signature=${encodeURIComponent(signature)}`,
      {},
      { redirectOnUnauthorized: false },
    ),
  downloadUrl: (
    agentId: string,
    path: string,
    options?: { inline?: boolean },
  ) => {
    const token = localStorage.getItem("token");
    const params = new URLSearchParams({ path, token: token || "" });
    if (options?.inline) params.set("inline", "1");
    return `${API_BASE}/agents/${agentId}/files/download?${params.toString()}`;
  },
};

export type FocusApiItem = {
  id: string;
  agent_id: string;
  key: string;
  title?: string | null;
  description: string;
  status: "in_progress" | "completed";
  kind: "normal" | "system";
  source: string;
  metadata?: Record<string, any>;
  sort_order: number;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export const focusApi = {
  list: (agentId: string, includeCompleted = true) =>
    request<FocusApiItem[]>(
      `/agents/${agentId}/focus/?include_completed=${includeCompleted ? "true" : "false"}`,
    ),
  upsert: (
    agentId: string,
    data: {
      key?: string;
      title?: string | null;
      description: string;
      status?: string;
      kind?: string;
      source?: string;
      metadata?: Record<string, any>;
    },
  ) =>
    request<FocusApiItem>(`/agents/${agentId}/focus/`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  complete: (agentId: string, key: string) =>
    request<FocusApiItem>(
      `/agents/${agentId}/focus/${encodeURIComponent(key)}/complete`,
      { method: "POST" },
    ),
};

export const channelApi = {
  get: (agentId: string) =>
    request<any>(`/agents/${agentId}/channel`).catch(() => null),
  create: (agentId: string, data: any) =>
    request<any>(`/agents/${agentId}/channel`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  update: (agentId: string, data: any) =>
    request<any>(`/agents/${agentId}/channel`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  delete: (agentId: string) =>
    request<void>(`/agents/${agentId}/channel`, { method: "DELETE" }),
  webhookUrl: (agentId: string) =>
    request<{ webhook_url: string }>(
      `/agents/${agentId}/channel/webhook-url`,
    ).catch(() => null),
};
