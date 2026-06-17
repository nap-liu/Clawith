import React, { useState } from 'react';
import { mcpServersApi } from '../../services/mcpServers';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorRole } from './types';
import PlaceholderField from './PlaceholderField';
import KeyValueEditor from './KeyValueEditor';
import { useResolvedPreview } from './useResolvedPreview';

interface Props {
  server: MCPServer;
  role: EditorRole;
  agentId?: string;
  onSaved: () => void;
}

export default function AdvancedTab({ server, agentId, onSaved }: Props) {
  const [headers, setHeaders] = useState<Record<string, string>>(
    server.headers_template || {}
  );
  const [systemPrompt, setSystemPrompt] = useState(server.system_prompt_block || '');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const { data: preview } = useResolvedPreview({
    serverId: server.id,
    agentId,
    draftOverrides: {
      headers_template: headers,
      system_prompt_block: systemPrompt !== (server.system_prompt_block || '') ? systemPrompt : null,
    },
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      await mcpServersApi.update(server.id, {
        headers_template: headers,
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
      <KeyValueEditor
        label="Headers Template"
        value={headers}
        onChange={setHeaders}
        keyPlaceholder="Header-Name"
        valuePlaceholder="value or ${user.email}"
        addLabel="+ 新增 Header"
      />

      {preview?.resolved_headers && Object.keys(preview.resolved_headers).length > 0 && (
        <div style={{
          marginTop: -8, marginBottom: 14,
          padding: 8,
          background: 'var(--bg-tertiary)', borderRadius: 4,
          fontSize: 11, color: 'var(--text-tertiary)',
        }}>
          <div style={{ marginBottom: 4, color: 'var(--text-secondary)' }}>↳ 解析后实际发送：</div>
          {Object.entries(preview.resolved_headers).map(([k, v]) => (
            <div key={k}><code style={{ color: 'var(--text-secondary)' }}>{k}: {v}</code></div>
          ))}
        </div>
      )}

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
