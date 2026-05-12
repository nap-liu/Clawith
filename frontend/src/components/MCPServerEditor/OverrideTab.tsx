import React, { useEffect, useState } from 'react';
import { mcpOverridesApi } from '../../services/mcpServers';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorRole } from './types';
import { useResolvedPreview } from './useResolvedPreview';

interface Props {
  server: MCPServer;
  agentId: string;
  role: EditorRole;
  onSaved: () => void;
}

export default function OverrideTab({ server, agentId, onSaved }: Props) {
  const [promptDraft, setPromptDraft] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Load existing agent override (if any)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const groups = await mcpOverridesApi.list(server.id, agentId);
        if (cancelled) return;
        const existing = groups.agent.find((o) => o.scope_id === agentId);
        setPromptDraft(existing?.system_prompt_block ?? '');
      } catch (e: any) {
        if (!cancelled) setErr(e?.message ?? String(e));
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, [server.id, agentId]);

  const { data: preview } = useResolvedPreview({
    serverId: server.id,
    agentId,
    draftOverrides: { system_prompt_block: promptDraft || null },
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      await mcpOverridesApi.putAgent(server.id, agentId, {
        system_prompt_block: promptDraft || null,
      });
      onSaved();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  };

  const clearOverride = async () => {
    setSaving(true);
    setErr(null);
    try {
      await mcpOverridesApi.deleteAgent(server.id, agentId);
      setPromptDraft('');
      onSaved();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!loaded) return <div style={{ color: 'var(--text-secondary)' }}>加载中…</div>;

  return (
    <div>
      <div style={{
        padding: 10, background: 'var(--bg-tertiary)', borderRadius: 6,
        marginBottom: 14, fontSize: 11, color: 'var(--text-tertiary)',
      }}>
        <div style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>Final 预览（实际发出去的值）</div>
        <div>URL: <code style={{ color: 'var(--text-secondary)' }}>{preview?.resolved_url ?? '…'}</code></div>
        {preview?.resolved_headers && Object.entries(preview.resolved_headers).map(([k, v]) => (
          <div key={k}><code>{k}: {v}</code></div>
        ))}
        {preview?.resolved_prompt && (
          <div style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>
            Prompt: <code style={{ color: 'var(--text-secondary)' }}>{preview.resolved_prompt}</code>
          </div>
        )}
        {preview?.used_layers && (
          <div style={{ marginTop: 4 }}>层: {preview.used_layers.join(' → ')}</div>
        )}
      </div>
      <div style={{ marginBottom: 14 }}>
        <label style={{ display: 'block', marginBottom: 4, fontSize: 12, color: 'var(--text-secondary)' }}>
          Prompt Override (追加到 platform/tenant 之后)
        </label>
        <textarea
          value={promptDraft}
          onChange={(e) => setPromptDraft(e.target.value)}
          rows={4}
          placeholder="留空 = 删除本 agent 的 override（恢复继承上层）"
          style={{
            width: '100%', boxSizing: 'border-box', padding: 8, fontSize: 12,
            background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)',
            borderRadius: 6, color: 'var(--text-primary)',
            fontFamily: 'ui-monospace, monospace', resize: 'vertical',
          }}
        />
        <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>
          可用占位符: ${'{agent.name}'} ${'{tenant.id}'}. ${'{user.*}'} 不允许（仅 URL/headers/credential 可用）。
        </div>
      </div>
      {err && <div style={{ color: '#ef4444', fontSize: 12, marginBottom: 8 }}>{err}</div>}
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button
          onClick={clearOverride}
          disabled={saving}
          style={{
            padding: '6px 14px', fontSize: 12,
            background: 'none', color: 'var(--text-secondary)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 6,
            cursor: saving ? 'not-allowed' : 'pointer',
          }}
        >
          清除 override
        </button>
        <button
          onClick={save}
          disabled={saving}
          style={{
            padding: '6px 14px', fontSize: 12,
            background: 'var(--accent-primary)', color: '#fff',
            border: 'none', borderRadius: 6,
            cursor: saving ? 'not-allowed' : 'pointer',
          }}
        >
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
  );
}
