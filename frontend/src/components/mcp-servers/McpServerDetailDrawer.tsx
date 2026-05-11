import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { mcpServersApi } from '../../services/mcpServers';
import type { MCPServerUpdatePayload, TestConnectionResult } from '../../types/mcpServer';
import { McpServerForm } from './McpServerForm';

interface Props {
  serverId: string;
  onClose: () => void;
}

export function McpServerDetailDrawer({ serverId, onClose }: Props) {
  const queryClient = useQueryClient();
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  const { data: server, isLoading } = useQuery({
    queryKey: ['mcp-server', serverId],
    queryFn: () => mcpServersApi.get(serverId),
  });

  const updateMutation = useMutation({
    mutationFn: (patch: MCPServerUpdatePayload) => mcpServersApi.update(serverId, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-server', serverId] });
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] });
    },
  });

  const testMutation = useMutation({
    mutationFn: () => mcpServersApi.testConnection(serverId),
    onSuccess: (res: TestConnectionResult) => {
      setTestResult({
        success: res.success,
        message: res.success
          ? `instructions 已刷新（${(res.instructions || '').length} 字符）`
          : (res.error || '未知错误'),
      });
      queryClient.invalidateQueries({ queryKey: ['mcp-server', serverId] });
    },
  });

  return (
    <div className="fixed inset-0 z-40 flex">
      <div className="flex-1 bg-black/30" onClick={onClose} />
      <div className="w-[640px] max-w-full bg-white shadow-xl overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b">
          <h3 className="text-lg font-semibold">{server?.display_name || '加载中…'}</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-700">✕</button>
        </div>
        {isLoading || !server ? (
          <div className="p-4 text-gray-500">加载中…</div>
        ) : (
          <McpServerForm
            server={server}
            onSave={updateMutation.mutate}
            onTestConnection={testMutation.mutate}
            saving={updateMutation.isPending}
            testing={testMutation.isPending}
            testResult={testResult}
          />
        )}
      </div>
    </div>
  );
}
