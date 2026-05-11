import { fetchJson } from './api';
import type {
  MCPServer,
  MCPServerCreatePayload,
  MCPServerUpdatePayload,
  TestConnectionResult,
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
