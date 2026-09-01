import { request, uploadFile } from "./core";

export const enterpriseApi = {
  llmModels: () => {
    const tid = localStorage.getItem("current_tenant_id");
    return request<any[]>(
      `/enterprise/llm-models${tid ? `?tenant_id=${tid}` : ""}`,
    );
  },

  llmModelsForTenant: (tenantId: string) =>
    request<any[]>(
      `/enterprise/llm-models?tenant_id=${encodeURIComponent(tenantId)}`,
    ),

  setDefaultModel: (modelId: string) =>
    request<void>(`/enterprise/llm-models/${modelId}/set-default`, {
      method: "POST",
    }),

  templates: () => request<any[]>("/agents/templates"),

  listMembers: () => {
    const tid = localStorage.getItem("current_tenant_id");
    return request<any[]>(`/org/users${tid ? `?tenant_id=${tid}` : ""}`);
  },

  kbFiles: (path: string = "") =>
    request<any[]>(
      `/enterprise/knowledge-base/files?path=${encodeURIComponent(path)}`,
    ),

  kbUpload: (file: File, subPath: string = "") =>
    uploadFile(
      `/enterprise/knowledge-base/upload?sub_path=${encodeURIComponent(subPath)}`,
      file,
    ),

  kbRead: (path: string) =>
    request<{ path: string; content: string }>(
      `/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`,
    ),

  kbWrite: (path: string, content: string) =>
    request(
      `/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`,
      {
        method: "PUT",
        body: JSON.stringify({ content }),
      },
    ),

  kbDelete: (path: string) =>
    request(
      `/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`,
      {
        method: "DELETE",
      },
    ),
};

export const activityApi = {
  list: (agentId: string, limit = 50) =>
    request<any[]>(`/agents/${agentId}/activity?limit=${limit}`),
};

export const messageApi = {
  inbox: (limit = 50) => request<any[]>(`/messages/inbox?limit=${limit}`),
  unreadCount: () =>
    request<{ unread_count: number }>("/messages/unread-count"),
  markRead: (messageId: string) =>
    request<void>(`/messages/${messageId}/read`, { method: "PUT" }),
  markAllRead: () => request<void>("/messages/read-all", { method: "PUT" }),
};

export const scheduleApi = {
  list: (agentId: string) => request<any[]>(`/agents/${agentId}/schedules/`),
  create: (
    agentId: string,
    data: {
      name: string;
      instruction: string;
      cron_expr: string;
      model_id?: string | null;
      temperature?: number | null;
      soul?: boolean;
      memory?: boolean;
    },
  ) =>
    request<any>(`/agents/${agentId}/schedules/`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  update: (agentId: string, scheduleId: string, data: any) =>
    request<any>(`/agents/${agentId}/schedules/${scheduleId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  delete: (agentId: string, scheduleId: string) =>
    request<void>(`/agents/${agentId}/schedules/${scheduleId}`, {
      method: "DELETE",
    }),
  trigger: (agentId: string, scheduleId: string) =>
    request<any>(`/agents/${agentId}/schedules/${scheduleId}/run`, {
      method: "POST",
    }),
  history: (agentId: string, scheduleId: string) =>
    request<any[]>(`/agents/${agentId}/schedules/${scheduleId}/history`),
};
