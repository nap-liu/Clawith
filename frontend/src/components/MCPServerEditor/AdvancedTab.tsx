import React, { useState } from 'react';
import { IconPlus, IconTrash } from '@tabler/icons-react';
import { mcpServersApi } from '../../services/mcpServers';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorRole } from './types';
import PlaceholderField from './PlaceholderField';
import { useResolvedPreview } from './useResolvedPreview';

interface Props {
  server: MCPServer;
  role: EditorRole;
  agentId?: string;
  onSaved: () => void;
}

export default function AdvancedTab({ server, agentId, onSaved }: Props) {
  const [headers, setHeaders] = useState<{ k: string; v: string }[]>(
    Object.entries(server.headers_template || {}).map(([k, v]) => ({ k, v }))
  );
  const [systemPrompt, setSystemPrompt] = useState(server.system_prompt_block || '');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const headersObj: Record<string, string> = Object.fromEntries(
    headers.filter(h => h.k.trim()).map(h => [h.k, h.v])
  );

  const { data: preview } = useResolvedPreview({
    serverId: server.id,
    agentId,
    draftOverrides: {
      headers_template: headersObj,
      system_prompt_block: systemPrompt !== (server.system_prompt_block || '') ? systemPrompt : null,
    },
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      await mcpServersApi.update(server.id, {
        headers_template: headersObj,
        system_prompt_block: systemPrompt || null,
      });
      onSaved();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <label style={{ display: 'block', marginBottom: 6, fontSize: 12, color: 'var(--text-secondary)' }}>
          Headers Template
        </label>
        {headers.map((h, i) => (
          <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
            <input
              value={h.k}
              placeholder="Header-Name"
              onChange={(e) => setHeaders((arr) => arr.map((x, j) => j === i ? { ...x, k: e.target.value } : x))}
              style={{
                flex: '0 0 200px', padding: 6, fontSize: 12,
                background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)',
                borderRadius: 4, color: 'var(--text-primary)',
              }}
            />
            <input
              value={h.v}
              placeholder="value or ${user.email}"
              onChange={(e) => setHeaders((arr) => arr.map((x, j) => j === i ? { ...x, v: e.target.value } : x))}
              style={{
                flex: 1, padding: 6, fontSize: 12,
                background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)',
                borderRadius: 4, color: 'var(--text-primary)',
              }}
            />
            <button
              onClick={() => setHeaders((arr) => arr.filter((_, j) => j !== i))}
              aria-label="remove header"
              style={{
                background: 'none', border: '1px solid var(--border-subtle)',
                borderRadius: 4, padding: '0 8px', cursor: 'pointer',
              }}
            >
              <IconTrash size={12} />
            </button>
          </div>
        ))}
        <button
          onClick={() => setHeaders((arr) => [...arr, { k: '', v: '' }])}
          style={{
            marginTop: 4, fontSize: 11,
            background: 'none', border: '1px dashed var(--border-subtle)',
            borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
            color: 'var(--text-secondary)',
            display: 'inline-flex', alignItems: 'center', gap: 4,
          }}
        >
          <IconPlus size={12} stroke={1.8} /> 新增 Header
        </button>
        {preview?.resolved_headers && Object.keys(preview.resolved_headers).length > 0 && (
          <div style={{
            marginTop: 8, padding: 8,
            background: 'var(--bg-tertiary)', borderRadius: 4,
            fontSize: 11, color: 'var(--text-tertiary)',
          }}>
            <div style={{ marginBottom: 4, color: 'var(--text-secondary)' }}>↳ 解析后实际发送：</div>
            {Object.entries(preview.resolved_headers).map(([k, v]) => (
              <div key={k}><code style={{ color: 'var(--text-secondary)' }}>{k}: {v}</code></div>
            ))}
          </div>
        )}
      </div>
      <PlaceholderField
        label="System Prompt Block (追加到 LLM 系统提示词)"
        value={systemPrompt}
        onChange={setSystemPrompt}
        placeholder="You are talking to ${agent.name} on behalf of ${tenant.id}."
        resolved={preview?.resolved_prompt}
        multiline
        helperHint="占位符限 ${agent.*} ${tenant.*}; ${user.*} 在 prompt 中不允许。"
      />
      {err && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 4 }}>{err}</div>}
      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
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
