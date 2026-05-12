import React, { useState } from 'react';
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

export default function BasicTab({ server, role, agentId, onSaved }: Props) {
  const [baseUrl, setBaseUrl] = useState(server.base_url_template || '');
  const [credential, setCredential] = useState('');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Anyone who can see the basic tab can attempt to edit it (visibility is
  // gated by role in shell; backend enforces actual ownership).
  const canEdit = true;

  const { data: preview } = useResolvedPreview({
    serverId: server.id,
    agentId,
    draftOverrides: {
      base_url_template: baseUrl !== server.base_url_template ? baseUrl : null,
      credential_template: credential || null,
    },
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      const payload: any = {};
      if (baseUrl !== server.base_url_template) payload.base_url_template = baseUrl;
      // Only PATCH credential when user typed something. Empty string is
      // treated by the backend as "clear it"; null/omitted as "don't touch".
      if (credential !== '') payload.credential_template = credential;
      await mcpServersApi.update(server.id, payload);
      setCredential('');  // clear after save so it's not re-applied
      onSaved();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <PlaceholderField
        label="Server URL"
        value={baseUrl}
        onChange={setBaseUrl}
        placeholder="https://api.example.com/mcp"
        resolved={preview?.resolved_url}
        disabled={!canEdit}
      />
      <PlaceholderField
        label={`API Key / Credential ${server.credential_state === 'set' ? '(已设置;保留为空 = 不修改)' : '(未设置)'}`}
        value={credential}
        onChange={setCredential}
        placeholder="bearer-token-or-${user.id}"
        disabled={!canEdit}
        password
        helperHint={`后端永不返回明文。当前状态:${preview?.resolved_credential_state ?? server.credential_state}。`}
      />
      {err && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 4 }}>{err}</div>}
      {canEdit && (
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
      )}
    </div>
  );
}
