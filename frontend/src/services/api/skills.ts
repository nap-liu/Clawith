import { request } from "./core";

export const skillApi = {
  list: () => request<any[]>("/skills/"),
  get: (id: string) => request<any>(`/skills/${id}`),
  create: (data: any) =>
    request<any>("/skills/", { method: "POST", body: JSON.stringify(data) }),
  update: (id: string, data: any) =>
    request<any>(`/skills/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  delete: (id: string) => request<void>(`/skills/${id}`, { method: "DELETE" }),
  browse: {
    list: (path: string) =>
      request<any[]>(`/skills/browse/list?path=${encodeURIComponent(path)}`),
    read: (path: string) =>
      request<{ content: string }>(
        `/skills/browse/read?path=${encodeURIComponent(path)}`,
      ),
    write: (path: string, content: string) =>
      request<any>("/skills/browse/write", {
        method: "PUT",
        body: JSON.stringify({ path, content }),
      }),
    delete: (path: string) =>
      request<any>(`/skills/browse/delete?path=${encodeURIComponent(path)}`, {
        method: "DELETE",
      }),
  },
  clawhub: {
    search: (q: string) =>
      request<any[]>(`/skills/clawhub/search?q=${encodeURIComponent(q)}`),
    detail: (slug: string) => request<any>(`/skills/clawhub/detail/${slug}`),
    install: (slug: string) =>
      request<any>("/skills/clawhub/install", {
        method: "POST",
        body: JSON.stringify({ slug }),
      }),
  },
  importFromUrl: (url: string) =>
    request<any>("/skills/import-from-url", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  previewUrl: (url: string) =>
    request<any>("/skills/import-from-url/preview", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  settings: {
    getToken: () =>
      request<{
        configured: boolean;
        source: string;
        masked: string;
        clawhub_configured: boolean;
        clawhub_masked: string;
      }>("/skills/settings/token"),
    setToken: (github_token: string) =>
      request<any>("/skills/settings/token", {
        method: "PUT",
        body: JSON.stringify({ github_token }),
      }),
    setClawhubKey: (clawhub_key: string) =>
      request<any>("/skills/settings/token", {
        method: "PUT",
        body: JSON.stringify({ clawhub_key }),
      }),
  },
  agentImport: {
    fromClawhub: (agentId: string, slug: string) =>
      request<any>(`/agents/${agentId}/files/import-from-clawhub`, {
        method: "POST",
        body: JSON.stringify({ slug }),
      }),
    fromUrl: (agentId: string, url: string) =>
      request<any>(`/agents/${agentId}/files/import-from-url`, {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
  },
  market: {
    list: (q = "") =>
      request<MarketSkill[]>(
        `/skills/market${q ? `?q=${encodeURIComponent(q)}` : ""}`,
      ),
    detail: (skillId: string) =>
      request<MarketSkill>(`/skills/market/${skillId}`),
    mine: () => request<MarketSkill[]>("/skills/mine"),
    publish: (agentId: string, data: PublishMarketSkillInput) =>
      request<MarketSkill>(`/agents/${agentId}/skills/publish`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    install: (agentId: string, skillId: string) =>
      request<any>(`/agents/${agentId}/skills/install`, {
        method: "POST",
        body: JSON.stringify({ skill_id: skillId }),
      }),
    uninstall: (agentId: string, skillId: string) =>
      request<any>(`/agents/${agentId}/skills/uninstall`, {
        method: "POST",
        body: JSON.stringify({ skill_id: skillId }),
      }),
    offline: (skillId: string) =>
      request<MarketSkill>(`/skills/${skillId}/offline`, { method: "POST" }),
    relist: (skillId: string) =>
      request<MarketSkill>(`/skills/${skillId}/relist`, { method: "POST" }),
    deleteOffline: (skillId: string) =>
      request<{ status: string; skill_id: string }>(
        `/skills/market/${skillId}`,
        { method: "DELETE" },
      ),
  },
};

export type MarketSkill = {
  id: string;
  name: string;
  description: string;
  category: string;
  icon: string;
  folder_name: string;
  visibility: "tenant" | "public";
  status: "draft" | "published" | "offline";
  version: number;
  downloads: number;
  is_builtin: boolean;
  publisher_name: string;
  publisher_user_id?: string | null;
  publisher_agent_id?: string | null;
  updated_at?: string | null;
  skill_md?: string;
  files?: Array<{ path: string; content: string }>;
};

export type PublishMarketSkillInput = {
  path: string;
  name: string;
  description: string;
  category: string;
  visibility: "tenant" | "public";
};

export const triggerApi = {
  list: (agentId: string) => request<any[]>(`/agents/${agentId}/triggers`),
  executions: (agentId: string, limit = 100) =>
    request<any[]>(`/agents/${agentId}/trigger-executions?limit=${limit}`),
  update: (agentId: string, triggerId: string, data: any) =>
    request<any>(`/agents/${agentId}/triggers/${triggerId}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  delete: (agentId: string, triggerId: string) =>
    request<void>(`/agents/${agentId}/triggers/${triggerId}`, {
      method: "DELETE",
    }),
};

export const credentialApi = {
  list: (agentId: string) => request<any[]>(`/agents/${agentId}/credentials/`),
  create: (agentId: string, data: any) =>
    request<any>(`/agents/${agentId}/credentials/`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  update: (agentId: string, credentialId: string, data: any) =>
    request<any>(`/agents/${agentId}/credentials/${credentialId}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  delete: (agentId: string, credentialId: string) =>
    request<void>(`/agents/${agentId}/credentials/${credentialId}`, {
      method: "DELETE",
    }),
};

export const controlApi = {
  click: (
    agentId: string,
    data: { session_id: string; x: number; y: number; button?: string },
  ) =>
    request<any>(`/agents/${agentId}/control/click`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  type: (agentId: string, data: { session_id: string; text: string }) =>
    request<any>(`/agents/${agentId}/control/type`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  pressKeys: (agentId: string, data: { session_id: string; keys: string[] }) =>
    request<any>(`/agents/${agentId}/control/press_keys`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  drag: (
    agentId: string,
    data: {
      session_id: string;
      from_x: number;
      from_y: number;
      to_x: number;
      to_y: number;
      duration_ms?: number;
    },
  ) =>
    request<any>(`/agents/${agentId}/control/drag`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  currentUrl: (agentId: string, data: { session_id: string }) =>
    request<{ status: string; url: string }>(
      `/agents/${agentId}/control/current-url`,
      { method: "POST", body: JSON.stringify(data) },
    ),
  screenshot: (agentId: string, data: { session_id: string }) =>
    request<any>(`/agents/${agentId}/control/screenshot`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  lock: (
    agentId: string,
    data: { session_id: string; platform_hint?: string; env_type?: string },
  ) =>
    request<any>(`/agents/${agentId}/control/lock`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  unlock: (
    agentId: string,
    data: {
      session_id: string;
      export_cookies?: boolean;
      platform_hint?: string;
    },
  ) =>
    request<any>(`/agents/${agentId}/control/unlock`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
};

export interface Pat {
  id: string;
  name: string;
  token_prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  scope: "read" | "write";
}

export interface PatCreated extends Pat {
  token: string;
}

export const patApi = {
  list: () => request<Pat[]>("/personal-access-tokens"),
  create: (data: {
    name: string;
    scope?: "read" | "write";
    expires_at?: string;
  }) =>
    request<PatCreated>("/personal-access-tokens", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  revoke: (id: string) =>
    request<{ ok: boolean }>(`/personal-access-tokens/${id}`, {
      method: "DELETE",
    }),
};
