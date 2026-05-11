import { useState, useEffect } from 'react';
import type { MCPServer, MCPServerUpdatePayload } from '../../types/mcpServer';

interface Props {
  server: MCPServer;
  onSave: (patch: MCPServerUpdatePayload) => void;
  onTestConnection: () => void;
  saving: boolean;
  testing: boolean;
  testResult: { success: boolean; message: string } | null;
}

export function McpServerForm({ server, onSave, onTestConnection, saving, testing, testResult }: Props) {
  const [displayName, setDisplayName] = useState(server.display_name);
  const [baseUrl, setBaseUrl] = useState(server.base_url_template);
  const [headersText, setHeadersText] = useState(JSON.stringify(server.headers_template || {}, null, 2));
  const [headersError, setHeadersError] = useState<string | null>(null);
  const [credential, setCredential] = useState('');  // empty = don't change
  const [systemPrompt, setSystemPrompt] = useState(server.system_prompt_block || '');

  useEffect(() => {
    setDisplayName(server.display_name);
    setBaseUrl(server.base_url_template);
    setHeadersText(JSON.stringify(server.headers_template || {}, null, 2));
    setSystemPrompt(server.system_prompt_block || '');
    setCredential('');
  }, [server.id]);

  const handleSave = () => {
    let headers: Record<string, string>;
    try {
      headers = JSON.parse(headersText);
      setHeadersError(null);
    } catch (e) {
      setHeadersError(`Headers JSON 解析失败：${(e as Error).message}`);
      return;
    }
    const patch: MCPServerUpdatePayload = {};
    if (displayName !== server.display_name) patch.display_name = displayName;
    if (baseUrl !== server.base_url_template) patch.base_url_template = baseUrl;
    if (JSON.stringify(headers) !== JSON.stringify(server.headers_template)) patch.headers_template = headers;
    if (credential.trim()) patch.credential_template = credential;
    if (systemPrompt !== (server.system_prompt_block || '')) patch.system_prompt_block = systemPrompt;
    if (Object.keys(patch).length === 0) return;
    onSave(patch);
  };

  return (
    <div className="space-y-4 p-4">
      <Field label="名称（不可改）">
        <input disabled value={server.name} className="w-full border rounded px-2 py-1 bg-gray-50 text-gray-500" />
      </Field>
      <Field label="显示名">
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)}
               className="w-full border rounded px-2 py-1" />
      </Field>
      <Field label="Base URL Template" hint="可含 ${user.id} / ${tenant.id} 等占位符（P3 启用渲染）">
        <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
               className="w-full border rounded px-2 py-1 font-mono text-sm" />
      </Field>
      <Field label="Headers Template (JSON)" hint='示例：{"X-User": "${user.email}"}'>
        <textarea value={headersText} onChange={(e) => setHeadersText(e.target.value)}
                  rows={4}
                  className="w-full border rounded px-2 py-1 font-mono text-xs" />
        {headersError && <div className="text-xs text-red-600 mt-1">{headersError}</div>}
      </Field>
      <Field label={`凭证 / API Key（当前：${server.credential_state === 'set' ? '已设置' : '未设置'}）`}
             hint="留空 = 保留原值；输入 = 替换；明确清空，请输入空格再删（暂不支持清空）">
        <input type="password" value={credential} onChange={(e) => setCredential(e.target.value)}
               placeholder={server.credential_state === 'set' ? '••••（已设置）' : '未设置'}
               className="w-full border rounded px-2 py-1" />
      </Field>
      <Field label="System Prompt Block" hint="拼到 LLM context；prompt 文本不允许 ${user.*} 占位符（被 placeholder_engine 拒绝）">
        <textarea value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)}
                  rows={6}
                  className="w-full border rounded px-2 py-1 font-mono text-xs" />
      </Field>

      {server.instructions && (
        <Field label="Server Instructions（来自 initialize 握手）">
          <pre className="text-xs bg-gray-50 p-2 rounded overflow-auto max-h-48">{server.instructions}</pre>
          <div className="text-xs text-gray-500 mt-1">
            上次握手时间：{server.instructions_captured_at || '—'}
          </div>
        </Field>
      )}

      <div className="flex items-center gap-3 pt-3 border-t">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-4 py-1.5 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {saving ? '保存中…' : '保存'}
        </button>
        <button
          onClick={onTestConnection}
          disabled={testing}
          className="px-4 py-1.5 border rounded disabled:opacity-50"
        >
          {testing ? '握手中…' : 'Test Connection'}
        </button>
        {testResult && (
          <span className={testResult.success ? 'text-green-600 text-sm' : 'text-red-600 text-sm'}>
            {testResult.success ? '✓ ' : '✗ '}{testResult.message}
          </span>
        )}
      </div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-medium mb-1">{label}</label>
      {children}
      {hint && <div className="text-xs text-gray-500 mt-1">{hint}</div>}
    </div>
  );
}
