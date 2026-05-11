import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { mcpServersApi } from '../../services/mcpServers';
import type { MCPServer, MCPServerCreatePayload } from '../../types/mcpServer';
import { McpServerDetailDrawer } from '../../components/mcp-servers/McpServerDetailDrawer';

export function McpServersPage() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  const { data: servers, isLoading, error } = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: mcpServersApi.list,
  });

  const createMutation = useMutation({
    mutationFn: (payload: MCPServerCreatePayload) => mcpServersApi.create(payload),
    onSuccess: (newServer) => {
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
      setShowCreate(false);
      setSelectedId(newServer.id);
    },
  });

  if (isLoading) return <div className="p-4 text-gray-500">加载中…</div>;
  if (error) return <div className="p-4 text-red-500">加载失败：{(error as Error).message}</div>;

  return (
    <div className="p-4">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">MCP Servers</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          + 新建
        </button>
      </div>

      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b text-sm text-gray-600">
            <th className="text-left p-2">名称</th>
            <th className="text-left p-2">显示名</th>
            <th className="text-left p-2">URL</th>
            <th className="text-left p-2">租户</th>
            <th className="text-left p-2">凭证</th>
            <th className="text-left p-2">Instructions</th>
          </tr>
        </thead>
        <tbody>
          {servers?.map((srv: MCPServer) => (
            <tr
              key={srv.id}
              className="border-b hover:bg-gray-50 cursor-pointer"
              onClick={() => setSelectedId(srv.id)}
            >
              <td className="p-2 font-mono text-sm">{srv.name}</td>
              <td className="p-2">{srv.display_name}</td>
              <td className="p-2 text-xs text-gray-600 truncate max-w-xs">{srv.base_url_template}</td>
              <td className="p-2 text-sm">{srv.tenant_id ? '租户' : '平台'}</td>
              <td className="p-2 text-sm">
                {srv.credential_state === 'set' ? '✓' : '—'}
              </td>
              <td className="p-2 text-sm text-gray-500">
                {srv.instructions ? '✓' : '—'}
              </td>
            </tr>
          ))}
          {!servers?.length && (
            <tr>
              <td colSpan={6} className="p-4 text-center text-gray-500">暂无 MCP server</td>
            </tr>
          )}
        </tbody>
      </table>

      {showCreate && (
        <CreateServerDialog
          onClose={() => setShowCreate(false)}
          onSubmit={(p) => createMutation.mutate(p)}
          submitting={createMutation.isPending}
          error={createMutation.error as Error | null}
        />
      )}

      {selectedId && (
        <McpServerDetailDrawer
          serverId={selectedId}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  );
}

function CreateServerDialog(props: {
  onClose: () => void;
  onSubmit: (p: MCPServerCreatePayload) => void;
  submitting: boolean;
  error: Error | null;
}) {
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [url, setUrl] = useState('');

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg p-6 w-[480px] max-w-full">
        <h3 className="text-lg font-semibold mb-4">新建 MCP Server</h3>
        <div className="space-y-3">
          <div>
            <label className="block text-sm font-medium mb-1">名称（slug）</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              pattern="[a-z0-9_-]+"
              className="w-full border rounded px-2 py-1"
              placeholder="ragflow_internal"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">显示名</label>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="w-full border rounded px-2 py-1"
              placeholder="RAGFlow 内部知识库"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Base URL Template</label>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="w-full border rounded px-2 py-1"
              placeholder="https://rag.example/mcp"
            />
          </div>
        </div>
        {props.error && (
          <div className="mt-3 text-sm text-red-600">{props.error.message}</div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={props.onClose} className="px-3 py-1.5 border rounded">取消</button>
          <button
            onClick={() => props.onSubmit({ name, display_name: displayName, base_url_template: url })}
            disabled={!name || !displayName || !url || props.submitting}
            className="px-3 py-1.5 bg-blue-600 text-white rounded disabled:opacity-50"
          >
            {props.submitting ? '创建中…' : '创建'}
          </button>
        </div>
      </div>
    </div>
  );
}
