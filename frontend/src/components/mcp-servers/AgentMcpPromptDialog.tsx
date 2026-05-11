import { useState, useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { mcpOverridesApi } from '../../services/mcpServers';

interface Props {
  agentId: string;
  agentName: string;
  toolDisplayName: string;
  mcpServerId: string;
  onClose: () => void;
}

export function AgentMcpPromptDialog({ agentId, agentName, toolDisplayName, mcpServerId, onClose }: Props) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState<string>('');
  const [loaded, setLoaded] = useState(false);

  const { data: overrides } = useQuery({
    queryKey: ['mcp-overrides', mcpServerId, agentId],
    queryFn: () => mcpOverridesApi.list(mcpServerId, agentId),
  });

  useEffect(() => {
    if (overrides && !loaded) {
      const existing = overrides.agent.find((o: any) => o.scope_id === agentId);
      setValue(existing?.system_prompt_block || '');
      setLoaded(true);
    }
  }, [overrides, agentId, loaded]);

  const saveMut = useMutation({
    mutationFn: () => mcpOverridesApi.putAgent(mcpServerId, agentId, { system_prompt_block: value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-overrides', mcpServerId, agentId] });
      onClose();
    },
  });

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
        zIndex: 2100, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="card" style={{
        width: '520px', maxWidth: '95vw', padding: '24px',
        display: 'flex', flexDirection: 'column', gap: '14px',
      }}>
        <h3 style={{ margin: 0, fontSize: '15px', color: 'var(--text-primary)' }}>
          为「{agentName}」自定义 {toolDisplayName} prompt
        </h3>
        <div style={{
          fontSize: '12px', color: 'var(--text-secondary)',
          background: 'var(--bg-tertiary)', padding: '8px 12px', borderRadius: '6px',
        }}>
          会被追加到平台/租户层 prompt 之后。该改动仅影响当前 agent。
        </div>
        <textarea
          className="form-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={8}
          style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', resize: 'vertical' }}
          placeholder="留空 = 删除该 agent 的 override（恢复继承上层）"
        />
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
          可用占位符：<code>{'${agent.name}'}</code> <code>{'${tenant.id}'}</code>。<code>{'${user.*}'}</code> 不允许（仅 URL/headers/credential 可用）
        </div>
        {saveMut.error && (
          <div style={{ fontSize: '12px', color: 'var(--error)' }}>
            {(saveMut.error as Error).message}
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={saveMut.isPending} onClick={() => saveMut.mutate()}>
            {saveMut.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
