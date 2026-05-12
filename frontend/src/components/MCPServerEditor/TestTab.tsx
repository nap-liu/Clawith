import React, { useState } from 'react';
import { IconCheck, IconAlertTriangle } from '@tabler/icons-react';
import { mcpServersApi, mcpOverridesApi } from '../../services/mcpServers';
import type { MCPServer, TestConnectionResult, DryRunResponse } from '../../types/mcpServer';

interface Props {
  server: MCPServer;
  agentId?: string;
}

type Result =
  | { kind: 'connection'; body: TestConnectionResult; ok: boolean }
  | { kind: 'dry-run'; body: DryRunResponse; ok: boolean };

export default function TestTab({ server, agentId }: Props) {
  const [running, setRunning] = useState<'connection' | 'dry-run' | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const testConnection = async () => {
    setRunning('connection');
    setErr(null);
    setResult(null);
    try {
      const body = await mcpServersApi.testConnection(server.id);
      setResult({ kind: 'connection', body, ok: !!body?.success });
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setRunning(null);
    }
  };

  const dryRun = async () => {
    setRunning('dry-run');
    setErr(null);
    setResult(null);
    try {
      const body = await mcpOverridesApi.dryRun(server.id, {
        identity: 'current_user',
        scope: agentId ? 'agent' : 'platform',
        agent_id: agentId ?? null,
      });
      setResult({ kind: 'dry-run', body, ok: !body.errors?.length });
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setRunning(null);
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        <button
          onClick={testConnection}
          disabled={running !== null}
          style={{
            padding: '6px 14px', fontSize: 12,
            border: '1px solid var(--border-subtle)', borderRadius: 6,
            background: 'var(--bg-secondary)', color: 'var(--text-primary)',
            cursor: running ? 'not-allowed' : 'pointer',
          }}
        >
          {running === 'connection' ? '测试中…' : 'Test Connection'}
        </button>
        <button
          onClick={dryRun}
          disabled={running !== null}
          style={{
            padding: '6px 14px', fontSize: 12,
            border: '1px solid var(--border-subtle)', borderRadius: 6,
            background: 'var(--bg-secondary)', color: 'var(--text-primary)',
            cursor: running ? 'not-allowed' : 'pointer',
          }}
        >
          {running === 'dry-run' ? '运行中…' : 'Dry Run（仅解析占位符）'}
        </button>
      </div>
      {err && <div style={{ color: '#ef4444', fontSize: 12 }}>{err}</div>}
      {result && (
        <div style={{
          padding: 10, background: 'var(--bg-tertiary)', borderRadius: 6,
          fontSize: 11, color: 'var(--text-secondary)',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6,
            color: result.ok ? '#10b981' : '#ef4444',
          }}>
            {result.ok ? <IconCheck size={14} /> : <IconAlertTriangle size={14} />}
            <span>{result.kind}: {result.ok ? 'OK' : 'FAILED'}</span>
          </div>
          <pre style={{
            whiteSpace: 'pre-wrap', margin: 0,
            fontFamily: 'ui-monospace, monospace', fontSize: 11,
          }}>
            {JSON.stringify(result.body, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
