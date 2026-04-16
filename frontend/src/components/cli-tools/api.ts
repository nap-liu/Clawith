// API wrappers for the CLI-tools backend at /api/tools/cli.

import type {
  CliTool,
  RuntimeConfig,
  SandboxConfig,
  TestRunRequest,
  TestRunResponse,
} from './types';

// Backend refuses any top-level `binary` or `config` key on create /
// update (extra=forbid). Wire-level types here enumerate only the
// admin-editable fields so TypeScript catches accidental regressions at
// the call site.
export interface CliToolCreateBody {
  name: string;
  display_name: string;
  description?: string;
  parameters_schema?: Record<string, unknown>;
  runtime?: RuntimeConfig;
  sandbox?: SandboxConfig;
  tenant_id?: string | null;
}

export interface CliToolUpdateBody {
  display_name?: string;
  description?: string;
  parameters_schema?: Record<string, unknown>;
  runtime?: RuntimeConfig;
  sandbox?: SandboxConfig;
  is_active?: boolean;
}

function authHeader(): HeadersInit {
  const token = localStorage.getItem('token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(init.headers || {}),
    },
  });
  if (res.status === 204) return undefined as unknown as T;
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const cliToolsApi = {
  list: () => request<CliTool[]>('/api/tools/cli'),

  get: (id: string) => request<CliTool>(`/api/tools/cli/${id}`),

  create: (body: CliToolCreateBody) =>
    request<CliTool>('/api/tools/cli', { method: 'POST', body: JSON.stringify(body) }),

  update: (id: string, body: CliToolUpdateBody) =>
    request<CliTool>(`/api/tools/cli/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  delete: (id: string) =>
    request<void>(`/api/tools/cli/${id}`, { method: 'DELETE' }),

  testRun: (id: string, req: TestRunRequest) =>
    request<TestRunResponse>(`/api/tools/cli/${id}/test-run`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // multipart — can't use the JSON request helper.
  uploadBinary: async (id: string, file: File): Promise<CliTool> => {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(`/api/tools/cli/${id}/binary`, {
      method: 'POST',
      headers: authHeader(),
      body: fd,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`upload failed: ${res.status} ${text}`);
    }
    return res.json();
  },
};
