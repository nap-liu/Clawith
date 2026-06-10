import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { CliTool } from '../types';
import { EnvGrid } from '../EnvGrid';
import { TestRunPanel } from '../TestRunPanel';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

const hintStyle: React.CSSProperties = {
  fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px',
};

const actionsRow: React.CSSProperties = {
  display: 'flex', gap: '8px', marginTop: '4px',
  justifyContent: 'flex-end',
  borderTop: '1px solid var(--border-subtle)', paddingTop: '16px',
};

export function ConfigStep({
  tool, onUpdated, onBack, onDone,
}: {
  tool: CliTool;
  onUpdated: (updated: CliTool) => void;
  onBack: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [env, setEnv] = useState<Record<string, string>>(() => tool.config?.env ?? {});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      const updated = await cliToolsApi.update(tool.id, { env });
      onUpdated(updated);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.wizard.${suffix}`, fb);

  return (
    <>
      <div>
        <label style={labelStyle}>{k('fieldEnvVars', 'Env vars')}</label>
        <EnvGrid env={env} onChange={setEnv} />
        <div style={hintStyle}>
          {k('envHint', 'Placeholders:')} <code>$user.id</code> <code>$user.phone</code>{' '}
          <code>$user.email</code> <code>$agent.id</code>{' '}
          <code>$tenant.id</code> <code>$state.dir</code>{' '}
          <span style={{ color: 'var(--text-tertiary)' }}>
            ({k('envStateDirHint', 'per-user persistent state directory')})
          </span>
        </div>
      </div>

      <TestRunPanel tool={tool} />

      {error && (
        <div style={{ color: 'var(--danger, #ff3b30)', fontSize: '12px' }}>{error}</div>
      )}

      <div style={actionsRow}>
        <button className="btn btn-secondary" onClick={onBack}>{t('common.back', 'Back')}</button>
        <button className="btn btn-primary" disabled={saving} onClick={save}>
          {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
        </button>
      </div>
    </>
  );
}
