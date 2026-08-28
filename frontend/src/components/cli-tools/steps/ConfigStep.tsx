import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconBraces } from '@tabler/icons-react';
import { cliToolsApi } from '../api';
import type { CliTool } from '../types';
import { EnvGrid } from '../EnvGrid';
import { TestRunPanel } from '../TestRunPanel';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
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
        <div
          style={{
            marginTop: '10px', padding: '10px 12px', borderRadius: '8px',
            border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)',
            display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '11px',
          }}
        >
          <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
            <IconBraces size={15} style={{ color: 'var(--text-secondary)', flexShrink: 0 }} />
            <strong style={{ color: 'var(--text-primary)' }}>
              {k('envHintTitle', 'Available placeholders')}
            </strong>
            <span style={{ color: 'var(--text-tertiary)' }}>
              {k('envHintDescription', 'Automatically replaced at runtime')}
            </span>
          </div>
          {[
            [k('envGroupUser', 'User'), ['$user.id', '$user.phone', '$user.email']],
            [k('envGroupContext', 'Context'), ['$agent.id', '$tenant.id']],
            [k('envGroupStorage', 'Storage'), ['$state.dir']],
          ].map(([label, values]) => (
            <div key={label as string} style={{ display: 'grid', gridTemplateColumns: '54px minmax(0, 1fr)', gap: '8px', alignItems: 'center' }}>
              <span style={{ color: 'var(--text-tertiary)' }}>{label as string}</span>
              <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                {(values as string[]).map((value) => (
                  <code
                    key={value}
                    title={value === '$state.dir' ? k('envStateDirHint', 'Persistent directory for the current user') : undefined}
                    style={{
                      padding: '2px 6px', borderRadius: '4px', background: 'var(--bg-tertiary)',
                      color: 'var(--text-secondary)', whiteSpace: 'nowrap',
                    }}
                  >
                    {value}
                  </code>
                ))}
                {(values as string[]).includes('$state.dir') && (
                  <span style={{ color: 'var(--text-tertiary)', alignSelf: 'center' }}>
                    {k('envStateDirHint', 'Persistent directory for the current user')}
                  </span>
                )}
              </div>
            </div>
          ))}
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
