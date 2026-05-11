import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { mcpOverridesApi } from '../../services/mcpServers';
import type { MCPServerOverride, MCPServerOverridePutPayload } from '../../types/mcpServer';

interface Props {
  serverId: string;
  lockedScope?:
    | { scope_type: 'agent'; scope_id: string }
    | { scope_type: 'tenant'; tenant_only: true };
}

export function OverrideMatrix({ serverId, lockedScope }: Props) {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [addingScope, setAddingScope] = useState<'tenant' | 'agent' | null>(null);
  const [newScopeId, setNewScopeId] = useState('');
  const [newPrompt, setNewPrompt] = useState('');

  const agentScopeId = lockedScope?.scope_type === 'agent' ? lockedScope.scope_id : undefined;

  const { data: overrides, isLoading } = useQuery({
    queryKey: ['mcp-overrides', serverId, agentScopeId],
    queryFn: () => mcpOverridesApi.list(serverId, agentScopeId),
  });

  const upsertMut = useMutation({
    mutationFn: (input: { scope_type: 'tenant' | 'agent'; scope_id: string; payload: MCPServerOverridePutPayload }) =>
      input.scope_type === 'tenant'
        ? mcpOverridesApi.putTenant(serverId, input.scope_id, input.payload)
        : mcpOverridesApi.putAgent(serverId, input.scope_id, input.payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-overrides', serverId] });
      setEditingId(null);
      setAddingScope(null);
      setNewScopeId('');
      setNewPrompt('');
    },
  });

  const deleteMut = useMutation({
    mutationFn: (input: { scope_type: 'tenant' | 'agent'; scope_id: string }) =>
      input.scope_type === 'tenant'
        ? mcpOverridesApi.deleteTenant(serverId, input.scope_id)
        : mcpOverridesApi.deleteAgent(serverId, input.scope_id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['mcp-overrides', serverId] }),
  });

  if (isLoading) {
    return <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '12px 0' }}>加载中…</div>;
  }

  const isAgentLocked = lockedScope?.scope_type === 'agent';
  const isTenantOnly = lockedScope?.scope_type === 'tenant';

  const visibleTenant = isAgentLocked ? [] : (overrides?.tenant || []);
  const visibleAgent = isAgentLocked
    ? (overrides?.agent || []).filter(o => o.scope_id === agentScopeId)
    : isTenantOnly
    ? []
    : (overrides?.agent || []);

  const showTenant = !isAgentLocked;
  const showAgent = !isTenantOnly;
  // "add tenant" UI only appears for platform-admin (no lockedScope) — tenant_only mode hides it
  const showAddTenant = showTenant && !lockedScope;
  const showAddAgent = isAgentLocked && visibleAgent.length === 0;

  const renderRow = (o: MCPServerOverride) => {
    const isEditing = editingId === o.id;
    return (
      <div key={o.id} style={{
        padding: '10px 12px',
        borderBottom: '1px solid var(--border-subtle)',
        background: isEditing ? 'var(--bg-elevated)' : 'transparent',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: isEditing ? '8px' : '0' }}>
          <code style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{o.scope_id.slice(0, 8)}…</code>
          {!isEditing && (
            <span style={{ flex: 1, fontSize: '12px', color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {(o.system_prompt_block || '').slice(0, 80) || <span style={{ fontStyle: 'italic', color: 'var(--text-tertiary)' }}>(空)</span>}
            </span>
          )}
          {!isEditing && (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => { setEditingId(o.id); setEditValue(o.system_prompt_block || ''); }}>
                编辑
              </button>
              <button className="btn btn-ghost btn-sm" style={{ color: 'var(--error)' }}
                      onClick={() => deleteMut.mutate({ scope_type: o.scope_type, scope_id: o.scope_id })}>
                删除
              </button>
            </>
          )}
        </div>
        {isEditing && (
          <>
            <textarea
              className="form-input"
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              rows={4}
              style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', width: '100%' }}
            />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '6px', marginTop: '6px' }}>
              <button className="btn btn-secondary btn-sm" onClick={() => setEditingId(null)}>取消</button>
              <button className="btn btn-primary btn-sm" disabled={upsertMut.isPending}
                      onClick={() => upsertMut.mutate({
                        scope_type: o.scope_type, scope_id: o.scope_id,
                        payload: { system_prompt_block: editValue },
                      })}>
                {upsertMut.isPending ? '保存中…' : '保存'}
              </button>
            </div>
          </>
        )}
      </div>
    );
  };

  return (
    <div>
      {showTenant && (
        <div>
          {visibleTenant.length === 0 && !showAddTenant && (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '12px 0' }}>暂无租户级 override</div>
          )}
          {visibleTenant.map(renderRow)}
          {showAddTenant && addingScope !== 'tenant' && (
            <button className="btn btn-ghost btn-sm" style={{ marginTop: '8px' }} onClick={() => setAddingScope('tenant')}>
              + 添加租户 override
            </button>
          )}
          {addingScope === 'tenant' && (
            <div style={{ padding: '10px 12px', background: 'var(--bg-elevated)', borderRadius: '6px', marginTop: '8px' }}>
              <label className="form-label" style={{ fontSize: '11px' }}>tenant_id</label>
              <input className="form-input" value={newScopeId} onChange={(e) => setNewScopeId(e.target.value)}
                     placeholder="UUID" style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', marginBottom: '8px' }} />
              <label className="form-label" style={{ fontSize: '11px' }}>prompt 片段</label>
              <textarea className="form-input" value={newPrompt} onChange={(e) => setNewPrompt(e.target.value)}
                        rows={3} style={{ fontFamily: 'var(--font-mono)', fontSize: '12px' }} />
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '6px', marginTop: '8px' }}>
                <button className="btn btn-secondary btn-sm" onClick={() => { setAddingScope(null); setNewScopeId(''); setNewPrompt(''); }}>取消</button>
                <button className="btn btn-primary btn-sm" disabled={upsertMut.isPending || !newScopeId.trim()}
                        onClick={() => upsertMut.mutate({
                          scope_type: 'tenant', scope_id: newScopeId.trim(),
                          payload: { system_prompt_block: newPrompt },
                        })}>
                  {upsertMut.isPending ? '添加中…' : '添加'}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {showAgent && (
        <div style={{ marginTop: showTenant && visibleTenant.length > 0 ? '12px' : '0' }}>
          {visibleAgent.length === 0 && !showAddAgent && (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '12px 0' }}>该 agent 暂无 override</div>
          )}
          {visibleAgent.map(renderRow)}
          {showAddAgent && (
            <button className="btn btn-primary btn-sm" style={{ marginTop: '8px' }}
                    onClick={() => upsertMut.mutate({
                      scope_type: 'agent', scope_id: agentScopeId!,
                      payload: { system_prompt_block: '' },
                    })}>
              + 为该 agent 添加 override
            </button>
          )}
        </div>
      )}
    </div>
  );
}
