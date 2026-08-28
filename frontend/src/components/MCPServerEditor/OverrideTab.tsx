import React, { useEffect, useState } from 'react';
import { mcpOverridesApi } from '../../services/mcpServers';
import type {
  MCPServer,
  DraftOverrides,
  MCPServerEditorDraftOverride,
} from '../../types/mcpServer';
import type { EditorRole } from './types';
import { useResolvedPreview } from './useResolvedPreview';
import OverrideFieldsEditor, {
  type OverrideDraft, emptyDraft, draftToPayload,
} from '../mcp-servers/OverrideFieldsEditor';

interface Props {
  server: MCPServer;
  agentId: string;
  role: EditorRole;
  onSaved: () => void;
  draftOverride?: MCPServerEditorDraftOverride | null;
  onDraftOverrideChange?: (value: MCPServerEditorDraftOverride | null) => void;
}

function draftToDraftOverrides(
  d: OverrideDraft,
  savedCredentialTemplate?: string | null,
): DraftOverrides {
  const dr: DraftOverrides = {
    system_prompt_block: d.system_prompt_block || null,
    headers_template: d.headers_template,
  };
  if (d.credential_input !== '') {
    dr.credential_template = d.credential_input;
  } else if (savedCredentialTemplate) {
    dr.credential_template = savedCredentialTemplate;
  }
  return dr;
}

export default function OverrideTab({
  server,
  agentId,
  onSaved,
  draftOverride,
  onDraftOverrideChange,
}: Props) {
  const isDraftMode = Boolean(onDraftOverrideChange);
  const [draft, setDraft] = useState<OverrideDraft>(emptyDraft());
  const [savedCredentialState, setSavedCredentialState] = useState<'set' | 'unset'>('unset');
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (isDraftMode) {
      setDraft({
        system_prompt_block: draftOverride?.system_prompt_block ?? '',
        headers_template: draftOverride?.headers_template ?? {},
        credential_input: '',
      });
      setSavedCredentialState(draftOverride?.credential_state ?? (
        draftOverride?.credential_template ? 'set' : 'unset'
      ));
      setLoaded(true);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const groups = await mcpOverridesApi.list(server.id, agentId);
        if (cancelled) return;
        const existing = groups.agent.find((o) => o.scope_id === agentId);
        if (existing) {
          setDraft({
            system_prompt_block: existing.system_prompt_block ?? '',
            headers_template: existing.headers_template ?? {},
            credential_input: '',
          });
          setSavedCredentialState(existing.credential_state);
        } else {
          setDraft(emptyDraft());
          setSavedCredentialState('unset');
        }
      } catch (e: any) {
        if (!cancelled) setErr(e?.message ?? String(e));
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, [server.id, agentId, isDraftMode]);

  const { data: preview } = useResolvedPreview({
    serverId: server.id,
    agentId,
    draftOverrides: draftToDraftOverrides(
      draft,
      isDraftMode ? draftOverride?.credential_template : null,
    ),
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      if (onDraftOverrideChange) {
        const payload = draftToPayload(draft);
        const credentialTemplate = draft.credential_input || draftOverride?.credential_template;
        onDraftOverrideChange({
          ...payload,
          ...(credentialTemplate ? { credential_template: credentialTemplate } : {}),
          credential_state: credentialTemplate ? 'set' : 'unset',
        });
        setDraft((current) => ({ ...current, credential_input: '' }));
        setSavedCredentialState(credentialTemplate ? 'set' : 'unset');
        onSaved();
        return;
      }
      const saved = await mcpOverridesApi.putAgent(server.id, agentId, draftToPayload(draft));
      // After save, clear the password input so we don't re-submit the same
      // credential on the next save click, and surface the server's freshest state.
      setDraft({
        system_prompt_block: saved.system_prompt_block ?? '',
        headers_template: saved.headers_template ?? {},
        credential_input: '',
      });
      setSavedCredentialState(saved.credential_state);
      onSaved();
    } catch {
      setErr('保存失败，请稍后重试。');
    } finally {
      setSaving(false);
    }
  };

  const clearOverride = async () => {
    setSaving(true);
    setErr(null);
    try {
      if (onDraftOverrideChange) {
        onDraftOverrideChange(null);
        setDraft(emptyDraft());
        setSavedCredentialState('unset');
        onSaved();
        return;
      }
      await mcpOverridesApi.deleteAgent(server.id, agentId);
      setDraft(emptyDraft());
      setSavedCredentialState('unset');
      onSaved();
    } catch {
      setErr('恢复失败，请稍后重试。');
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
        <div style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>配置预览</div>
        <div>服务地址：<code style={{ color: 'var(--text-secondary)' }}>{preview?.resolved_url ?? '…'}</code></div>
        {preview?.resolved_headers && Object.entries(preview.resolved_headers).map(([k, v]) => (
          <div key={k}><code>{k}: {v}</code></div>
        ))}
        {preview?.resolved_credential_state && (
          <div style={{ marginTop: 2 }}>访问凭证：<code>{preview.resolved_credential_state === 'set' ? '已设置' : '未设置'}</code></div>
        )}
        {preview?.resolved_prompt && (
          <div style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>
            执行说明：<code style={{ color: 'var(--text-secondary)' }}>{preview.resolved_prompt}</code>
          </div>
        )}
      </div>

      <OverrideFieldsEditor
        draft={draft}
        onChange={setDraft}
        savedCredentialState={savedCredentialState}
        placeholdersHint="可按需补充当前数字员工使用此服务时的说明。"
      />

      {err && <div style={{ color: '#ef4444', fontSize: 12, marginTop: 10 }}>{err}</div>}
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 14 }}>
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
          恢复默认配置
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
