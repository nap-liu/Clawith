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

// Keys that likely hold secrets — rendered as password inputs.
const SECRET_KEY_RE = /TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH/i;
const isSecretKey = (k: string) => SECRET_KEY_RE.test(k);

export default function BasicTab({ server, role, agentId, onSaved }: Props) {
  // Transport selector — default to 'http' when the server doesn't have a
  // stored transport value (backward-compat with existing servers).
  const [transport, setTransport] = useState<'http' | 'stdio'>(
    server.transport ?? 'http'
  );

  // HTTP fields
  const [baseUrl, setBaseUrl] = useState(server.base_url_template || '');
  const [credential, setCredential] = useState('');

  // stdio fields
  const [command, setCommand] = useState(server.command_template || '');
  const [args, setArgs] = useState<string[]>(server.args_template ?? []);
  const [env, setEnv] = useState<Record<string, string>>(server.env_template ?? {});

  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // canEdit: anyone who reaches this tab may save (role gate is in the shell).
  const canEdit = true;

  const { data: preview } = useResolvedPreview({
    serverId: server.id,
    agentId,
    draftOverrides:
      transport === 'http'
        ? {
            base_url_template: baseUrl !== server.base_url_template ? baseUrl : null,
            credential_template: credential || null,
          }
        : {},
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      if (transport === 'http') {
        const payload: any = { transport };
        if (baseUrl !== server.base_url_template) payload.base_url_template = baseUrl;
        if (credential !== '') payload.credential_template = credential;
        await mcpServersApi.update(server.id, payload);
        setCredential('');
      } else {
        // stdio
        const payload: any = {
          transport,
          command_template: command,
          args_template: args,
          env_template: env,
        };
        await mcpServersApi.update(server.id, payload);
      }
      onSaved();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  };

  const isValid =
    transport === 'http'
      ? baseUrl.trim().length > 0
      : command.trim().length > 0;

  // --- Args editor helpers ---
  const updateArg = (idx: number, v: string) =>
    setArgs((arr) => arr.map((a, i) => (i === idx ? v : a)));
  const removeArg = (idx: number) =>
    setArgs((arr) => arr.filter((_, i) => i !== idx));
  const addArg = () => setArgs((arr) => [...arr, '']);

  return (
    <div>
      {/* Transport selector */}
      <div style={{ marginBottom: 16 }}>
        <label style={{ display: 'block', marginBottom: 6, fontSize: 12, color: 'var(--text-secondary)' }}>
          传输方式 (Transport)
        </label>
        <div style={{ display: 'flex', gap: 10 }}>
          {(['http', 'stdio'] as const).map((t) => (
            <label
              key={t}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                fontSize: 12, cursor: 'pointer',
                color: transport === t ? 'var(--text-primary)' : 'var(--text-secondary)',
              }}
            >
              <input
                type="radio"
                name="transport"
                value={t}
                checked={transport === t}
                onChange={() => setTransport(t)}
              />
              {t === 'http' ? 'HTTP / SSE' : 'Stdio (npx / uvx)'}
            </label>
          ))}
        </div>
      </div>

      {/* HTTP fields */}
      {transport === 'http' && (
        <>
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
        </>
      )}

      {/* Stdio fields */}
      {transport === 'stdio' && (
        <>
          <div style={{ marginBottom: 14 }}>
            <label style={{ display: 'block', marginBottom: 4, fontSize: 12, color: 'var(--text-secondary)' }}>
              Command <span style={{ color: '#ef4444' }}>*</span>
            </label>
            <input
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder="npx"
              style={{
                width: '100%', boxSizing: 'border-box',
                padding: 8, fontSize: 12,
                background: 'var(--bg-secondary)',
                border: `1px solid ${command.trim() ? 'var(--border-subtle)' : '#ef4444'}`,
                borderRadius: 6, color: 'var(--text-primary)',
              }}
            />
            {!command.trim() && (
              <div style={{ marginTop: 4, fontSize: 11, color: '#ef4444' }}>
                Command 为必填项。
              </div>
            )}
            <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-tertiary)' }}>
              可执行命令,如 <code>npx</code> 或 <code>uvx</code>。
            </div>
          </div>

          {/* Args editor */}
          <div style={{ marginBottom: 14 }}>
            <label style={{ display: 'block', marginBottom: 6, fontSize: 12, color: 'var(--text-secondary)' }}>
              Args (按顺序)
            </label>
            {args.map((a, idx) => (
              <div key={idx} style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
                <input
                  value={a}
                  onChange={(e) => updateArg(idx, e.target.value)}
                  placeholder={idx === 0 ? '-y' : 'alibabacloud-devops-mcp-server'}
                  style={{
                    flex: 1, padding: '6px 8px', fontSize: 12,
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: 4, color: 'var(--text-primary)',
                    fontFamily: 'ui-monospace, monospace',
                    boxSizing: 'border-box',
                  }}
                />
                <button
                  type="button"
                  onClick={() => removeArg(idx)}
                  style={{
                    padding: '4px 8px', fontSize: 11,
                    background: 'transparent',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: 4, color: 'var(--text-tertiary)',
                    cursor: 'pointer', flexShrink: 0,
                  }}
                >
                  删除
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={addArg}
              style={{
                marginTop: 4, fontSize: 11,
                background: 'none', border: '1px dashed var(--border-subtle)',
                borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
                color: 'var(--text-secondary)',
              }}
            >
              + 添加 Arg
            </button>
            <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-tertiary)' }}>
              例如: <code>-y</code>、<code>alibabacloud-devops-mcp-server</code>
            </div>
          </div>

          {/* Env key-value editor */}
          <KeyValueEditor
            label="环境变量 (Env)"
            value={env}
            onChange={setEnv}
            keyPlaceholder="VARIABLE_NAME"
            valuePlaceholder="value 或 ${user.xxx}"
            addLabel="+ 添加环境变量"
            isSecretKey={isSecretKey}
            helperHint="密钥类变量(含 TOKEN / KEY / SECRET 等)自动遮蔽显示。支持占位符 ${agent.*} ${tenant.*} ${user.*}。"
          />
        </>
      )}

      {err && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 4 }}>{err}</div>}
      {canEdit && (
        <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
          <button
            onClick={save}
            disabled={saving || !isValid}
            title={!isValid ? (transport === 'http' ? 'Server URL 为必填项' : 'Command 为必填项') : undefined}
            style={{
              padding: '6px 14px', fontSize: 12,
              background: isValid ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
              color: isValid ? '#fff' : 'var(--text-tertiary)',
              border: 'none', borderRadius: 6,
              cursor: saving || !isValid ? 'not-allowed' : 'pointer',
            }}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      )}
    </div>
  );
}
