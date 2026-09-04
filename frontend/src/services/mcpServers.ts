import { fetchJson } from './api';
import type {
  MCPServer,
  MCPServerCreatePayload,
  MCPServerImportPayload,
  MCPServerImportResult,
  MCPServerUpdatePayload,
  TestConnectionResult,
  MCPToolRefreshResult,
  MCPServerOverride,
  MCPServerOverridePutPayload,
  OverridesGrouped,
  DryRunRequest,
  DryRunResponse,
} from '../types/mcpServer';

export const mcpServersApi = {
  list: () => fetchJson<MCPServer[]>('/admin/mcp-servers'),
  get: (id: string, agentId?: string) => {
    const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
    return fetchJson<MCPServer>(`/admin/mcp-servers/${id}${qs}`);
  },
  create: (data: MCPServerCreatePayload) =>
    fetchJson<MCPServer>('/admin/mcp-servers', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  import: (data: MCPServerImportPayload) =>
    fetchJson<MCPServerImportResult>('/admin/mcp-servers/import', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: MCPServerUpdatePayload) =>
    fetchJson<MCPServer>(`/admin/mcp-servers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    fetchJson<void>(`/admin/mcp-servers/${id}`, { method: 'DELETE' }),
  testConnection: (id: string) =>
    fetchJson<TestConnectionResult>(`/admin/mcp-servers/${id}/test-connection`, {
      method: 'POST',
    }),
  refreshTools: (id: string, agentId?: string) => {
    const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
    return fetchJson<MCPToolRefreshResult>(
      `/admin/mcp-servers/${id}/refresh-tools${qs}`,
      { method: 'POST' },
    );
  },
};

export const mcpOverridesApi = {
  list: (serverId: string, agentId?: string) => {
    const qs = agentId ? `?agent_id=${agentId}` : '';
    return fetchJson<OverridesGrouped>(`/admin/mcp-servers/${serverId}/overrides${qs}`);
  },
  putTenant: (serverId: string, tenantId: string, payload: MCPServerOverridePutPayload) =>
    fetchJson<MCPServerOverride>(
      `/admin/mcp-servers/${serverId}/overrides/tenant/${tenantId}`,
      { method: 'PUT', body: JSON.stringify(payload) },
    ),
  deleteTenant: (serverId: string, tenantId: string) =>
    fetchJson<void>(
      `/admin/mcp-servers/${serverId}/overrides/tenant/${tenantId}`,
      { method: 'DELETE' },
    ),
  putAgent: (serverId: string, agentId: string, payload: MCPServerOverridePutPayload) =>
    fetchJson<MCPServerOverride>(
      `/admin/mcp-servers/${serverId}/overrides/agent/${agentId}`,
      { method: 'PUT', body: JSON.stringify(payload) },
    ),
  deleteAgent: (serverId: string, agentId: string) =>
    fetchJson<void>(
      `/admin/mcp-servers/${serverId}/overrides/agent/${agentId}`,
      { method: 'DELETE' },
    ),
  dryRun: (serverId: string, payload: DryRunRequest) =>
    fetchJson<DryRunResponse>(
      `/admin/mcp-servers/${serverId}/dry-run`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
};
