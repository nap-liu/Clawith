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

  return (
    <div className="p-4 space-y-4">
      <div className="space-y-3">
        <div className="flex items-center gap-3">
          <label className="text-sm font-medium w-20">身份：</label>
          <select value={identity} onChange={(e) => setIdentity(e.target.value as 'current_user' | 'synthetic')}
                  className="border rounded px-2 py-1 text-sm">
            <option value="synthetic">Synthetic（dummy 值，可分享截图）</option>
            <option value="current_user">Current User（你自己的身份）</option>
          </select>
        </div>
        <div className="flex items-center gap-3">
          <label className="text-sm font-medium w-20">作用域：</label>
          <select value={scope} onChange={(e) => setScope(e.target.value as 'platform' | 'tenant' | 'agent')}
                  className="border rounded px-2 py-1 text-sm">
            <option value="platform">仅平台层</option>
            <option value="tenant">平台 + 租户</option>
            <option value="agent">平台 + 租户 + agent</option>
          </select>
        </div>
        {scope !== 'platform' && (
          <div className="flex items-center gap-3">
            <label className="text-sm font-medium w-20">tenant_id：</label>
            <input value={tenantId} onChange={(e) => setTenantId(e.target.value)}
                   placeholder="UUID" className="border rounded px-2 py-1 text-sm flex-1 font-mono" />
          </div>
        )}
        {scope === 'agent' && (
          <div className="flex items-center gap-3">
            <label className="text-sm font-medium w-20">agent_id：</label>
            <input value={agentId} onChange={(e) => setAgentId(e.target.value)}
                   placeholder="UUID" className="border rounded px-2 py-1 text-sm flex-1 font-mono" />
          </div>
        )}
      </div>

      <button
        onClick={() => dryRun.mutate()}
        disabled={dryRun.isPending || (scope !== 'platform' && !tenantId)}
        className="px-4 py-1.5 bg-blue-600 text-white rounded disabled:opacity-50"
      >
        {dryRun.isPending ? '渲染中…' : '预览'}
      </button>

      {dryRun.error && (
        <div className="text-sm text-red-600">错误：{(dryRun.error as Error).message}</div>
      )}

      {result && (
        <div className="space-y-3 border-t pt-4">
          <ResultField label="使用的层" value={result.used_layers.join(' + ')} />
          <ResultField label="Resolved URL" value={result.resolved_url} mono />
          <ResultField label="Resolved Headers" value={JSON.stringify(result.resolved_headers, null, 2)} mono pre />
          <ResultField label="Resolved Credential" value={result.resolved_credential_state === 'set' ? '✓ set（已脱敏）' : '— unset'} />
          <ResultField label="Resolved Prompt" value={result.resolved_prompt || '(空)'} pre />
          {result.errors.length > 0 && (
            <div className="text-sm text-red-600">
              <div className="font-medium">渲染错误：</div>
              {result.errors.map((e, i) => <div key={i} className="text-xs ml-2">• {e}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ResultField({ label, value, mono, pre }: { label: string; value: string; mono?: boolean; pre?: boolean }) {
  return (
    <div>
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      {pre ? (
        <pre className={`bg-gray-50 p-2 rounded overflow-auto max-h-48 text-xs ${mono ? 'font-mono' : ''}`}>{value}</pre>
      ) : (
        <div className={`text-sm ${mono ? 'font-mono text-xs' : ''}`}>{value}</div>
      )}
    </div>
  );
}
