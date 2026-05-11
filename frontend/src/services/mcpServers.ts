import { fetchJson } from './api';
import type {
  MCPServer,
  MCPServerCreatePayload,
  MCPServerUpdatePayload,
  TestConnectionResult,
  MCPServerOverride,
  MCPServerOverridePutPayload,
  OverridesGrouped,
  DryRunRequest,
  DryRunResponse,
} from '../types/mcpServer';

export const mcpServersApi = {
  list: () => fetchJson<MCPServer[]>('/admin/mcp-servers'),
  get: (id: string) => fetchJson<MCPServer>(`/admin/mcp-servers/${id}`),
  create: (data: MCPServerCreatePayload) =>
    fetchJson<MCPServer>('/admin/mcp-servers', {
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
};

export const mcpOverridesApi = {
  list: (serverId: string) =>
    fetchJson<OverridesGrouped>(`/admin/mcp-servers/${serverId}/overrides`),
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
