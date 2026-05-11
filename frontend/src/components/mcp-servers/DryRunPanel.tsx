import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { mcpOverridesApi } from '../../services/mcpServers';
import type { DryRunRequest, DryRunResponse } from '../../types/mcpServer';

interface Props {
  serverId: string;
}

export function DryRunPanel({ serverId }: Props) {
  const [identity, setIdentity] = useState<'current_user' | 'synthetic'>('synthetic');
  const [scope, setScope] = useState<'platform' | 'tenant' | 'agent'>('platform');
  const [tenantId, setTenantId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [result, setResult] = useState<DryRunResponse | null>(null);

  const dryRun = useMutation({
    mutationFn: () => {
      const payload: DryRunRequest = {
        identity, scope,
        tenant_id: scope !== 'platform' ? tenantId : undefined,
        agent_id: scope === 'agent' ? agentId : undefined,
      };
      return mcpOverridesApi.dryRun(serverId, payload);
    },
    onSuccess: setResult,
  });

  const fieldStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: '8px' };
  const labelStyle: React.CSSProperties = { fontSize: '12px', color: 'var(--text-secondary)', minWidth: '64px' };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      <div style={fieldStyle}>
        <label style={labelStyle}>身份</label>
        <select className="form-input" value={identity} onChange={(e) => setIdentity(e.target.value as 'current_user' | 'synthetic')}
                style={{ fontSize: '12px', flex: 1 }}>
          <option value="synthetic">Synthetic（dummy 值，可分享截图）</option>
          <option value="current_user">Current User（你自己）</option>
        </select>
      </div>
      <div style={fieldStyle}>
        <label style={labelStyle}>作用域</label>
        <select className="form-input" value={scope} onChange={(e) => setScope(e.target.value as 'platform' | 'tenant' | 'agent')}
                style={{ fontSize: '12px', flex: 1 }}>
          <option value="platform">仅平台层</option>
          <option value="tenant">平台 + 租户</option>
          <option value="agent">平台 + 租户 + agent</option>
        </select>
      </div>
      {scope !== 'platform' && (
        <div style={fieldStyle}>
          <label style={labelStyle}>tenant_id</label>
          <input className="form-input" value={tenantId} onChange={(e) => setTenantId(e.target.value)}
                 placeholder="UUID" style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', flex: 1 }} />
        </div>
      )}
      {scope === 'agent' && (
        <div style={fieldStyle}>
          <label style={labelStyle}>agent_id</label>
          <input className="form-input" value={agentId} onChange={(e) => setAgentId(e.target.value)}
                 placeholder="UUID" style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', flex: 1 }} />
        </div>
      )}
      <div>
        <button className="btn btn-secondary btn-sm" onClick={() => dryRun.mutate()}
                disabled={dryRun.isPending || (scope !== 'platform' && !tenantId.trim()) || (scope === 'agent' && !agentId.trim())}>
          {dryRun.isPending ? '渲染中…' : '预览'}
        </button>
      </div>

      {dryRun.error && (
        <div style={{ fontSize: '12px', color: 'var(--error)' }}>
          错误：{(dryRun.error as Error).message}
        </div>
      )}

      {result && (
        <div style={{ paddingTop: '10px', borderTop: '1px solid var(--border-subtle)', display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <Field label="使用的层" value={result.used_layers.join(' + ')} />
          <Field label="Resolved URL" value={result.resolved_url} mono />
          <Field label="Resolved Headers" value={JSON.stringify(result.resolved_headers, null, 2)} mono pre />
          <Field label="Resolved Credential" value={result.resolved_credential_state === 'set' ? '✓ set（已脱敏）' : '— unset'} />
          <Field label="Resolved Prompt" value={result.resolved_prompt || '(空)'} pre />
          {result.errors.length > 0 && (
            <div style={{ fontSize: '12px', color: 'var(--error)' }}>
              <div style={{ fontWeight: 500, marginBottom: '4px' }}>渲染错误：</div>
              {result.errors.map((e, i) => <div key={i} style={{ fontSize: '11px', marginLeft: '8px' }}>• {e}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, value, mono, pre }: { label: string; value: string; mono?: boolean; pre?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '3px' }}>{label}</div>
      {pre ? (
        <pre style={{
          background: 'var(--bg-elevated)', padding: '8px 10px', borderRadius: '6px',
          maxHeight: '180px', overflow: 'auto', fontSize: '11px',
          fontFamily: mono ? 'var(--font-mono)' : 'inherit',
          margin: 0, color: 'var(--text-secondary)',
        }}>{value}</pre>
      ) : (
        <div style={{
          fontSize: '12px', color: 'var(--text-primary)',
          fontFamily: mono ? 'var(--font-mono)' : 'inherit',
          wordBreak: 'break-all',
        }}>{value}</div>
      )}
    </div>
  );
}
