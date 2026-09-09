import { useTranslation } from 'react-i18next';
import KeyValueEditor from './KeyValueEditor';
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
  const { t } = useTranslation();
  const [env, setEnv] = useState<Record<string, string>>({});
  const isDraftMode = Boolean(onDraftOverrideChange);
  const shared = server.is_shared !== false;
  const [draft, setDraft] = useState<OverrideDraft>(emptyDraft());
  const [savedCredentialState, setSavedCredentialState] = useState<'set' | 'unset'>('unset');
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (isDraftMode) {
      setEnv(draftOverride?.env_template ?? {});
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
        setEnv(existing?.env_template ?? {});
        if (existing) {
          setDraft({
            system_prompt_block: existing.system_prompt_block ?? '',
            headers_template: existing.headers_template ?? {},
            credential_input: '',
          });
          setSavedCredentialState(existing.credential_state);
        } else {
          setEnv({});
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
      shared ? { ...draft, system_prompt_block: '' } : draft,
      isDraftMode ? draftOverride?.credential_template : null,
    ),
  });

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      if (onDraftOverrideChange) {
        const payload = { ...draftToPayload(draft), env_template: env };
        if (shared) delete payload.system_prompt_block;
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
      const payload = { ...draftToPayload(draft), env_template: env };
      if (shared) delete payload.system_prompt_block;
      const saved = await mcpOverridesApi.putAgent(server.id, agentId, payload);
      // After save, clear the password input so we don't re-submit the same
      // credential on the next save click, and surface the server's freshest state.
      setDraft({
        system_prompt_block: saved.system_prompt_block ?? '',
        headers_template: saved.headers_template ?? {},
        credential_input: '',
      });
      setEnv(saved.env_template ?? {});
      setSavedCredentialState(saved.credential_state);
      onSaved();
    } catch {
      setErr(t('mcpConfig.saveFailed'));
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
        setEnv({});
        setDraft(emptyDraft());
        setSavedCredentialState('unset');
        onSaved();
        return;
      }
      await mcpOverridesApi.deleteAgent(server.id, agentId);
      setEnv({});
      setDraft(emptyDraft());
      setSavedCredentialState('unset');
      onSaved();
    } catch {
      setErr(t('mcpConfig.resetFailed'));
    } finally {
      setSaving(false);
    }
  };

  if (!loaded) return <div style={{ color: 'var(--text-secondary)' }}>{t('mcpConfig.loading')}</div>;

  return (
    <div>
      <div style={{
        padding: 10, background: 'var(--bg-tertiary)', borderRadius: 6,
        marginBottom: 14, fontSize: 11, color: 'var(--text-tertiary)',
      }}>
        <div style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>{t('mcpConfig.preview')}</div>
        <div>{t('mcpConfig.address')}: <code style={{ color: 'var(--text-secondary)' }}>{preview?.resolved_url ?? '…'}</code></div>
        {preview?.resolved_headers && Object.entries(preview.resolved_headers).map(([k, v]) => (
          <div key={k}><code>{k}: {v}</code></div>
        ))}
        {preview?.resolved_credential_state && (
          <div style={{ marginTop: 2 }}>{t('mcpConfig.credential')}: <code>{preview.resolved_credential_state === 'set' ? t('mcpConfig.set') : t('mcpConfig.unset')}</code></div>
        )}
        {preview?.resolved_prompt && (
          <div style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>
            {t('mcpConfig.instructions')}: <code style={{ color: 'var(--text-secondary)' }}>{preview.resolved_prompt}</code>
          </div>
        )}
      </div>

      <OverrideFieldsEditor
        draft={draft}
        onChange={setDraft}
        savedCredentialState={savedCredentialState}
        hide={{ prompt: shared }}
        placeholdersHint={t('mcpConfig.instructionsHint')}
      />

      {server.transport === 'stdio' && <KeyValueEditor
        value={env} onChange={setEnv} label={t('mcpConfig.environment')}
        keyPlaceholder={t('mcpConfig.name')} valuePlaceholder={t('mcpConfig.value')}
        addLabel={t('mcpConfig.addEnvironment')}
        isSecretKey={(key) => /TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL/i.test(key)}
      />}

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
          {t('mcpConfig.reset')}
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
          {saving ? t('mcpConfig.saving') : t('mcpConfig.save')}
        </button>
      </div>
    </div>
  );
}
