import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CliTool } from '../types';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

const actionsRow: React.CSSProperties = {
  display: 'flex', gap: '8px', marginTop: '4px',
  justifyContent: 'flex-end',
  borderTop: '1px solid var(--border-subtle)', paddingTop: '16px',
};

export function BasicInfoStep({
  tool, onNext, onCancel,
}: {
  tool: CliTool | null;
  onNext: (values: Partial<CliTool>) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(tool?.name ?? '');
  const [displayName, setDisplayName] = useState(tool?.display_name ?? '');
  const [description, setDescription] = useState(tool?.description ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    setErr(null);
    setSubmitting(true);
    try {
      await onNext({ name, display_name: displayName, description });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.wizard.${suffix}`, fb);

  return (
    <>
      <div>
        <label style={labelStyle}>
          {k('fieldName', 'Name')} <span style={{ color: 'var(--danger, #ff3b30)' }}>*</span>
        </label>
        <input
          className="form-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={!!tool?.id}
          placeholder={k('fieldNamePlaceholder', 'unique_identifier')}
        />
        {tool?.id && (
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
            {k('fieldNameLocked', 'Locked after creation.')}
          </div>
        )}
      </div>

      <div>
        <label style={labelStyle}>
          {k('fieldDisplayName', 'Display name')} <span style={{ color: 'var(--danger, #ff3b30)' }}>*</span>
        </label>
        <input
          className="form-input"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder={k('fieldDisplayNamePlaceholder', 'User-facing name')}
        />
      </div>

      <div>
        <label style={labelStyle}>{k('fieldDescription', 'Description')}</label>
        <textarea
          className="form-input"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          style={{ resize: 'vertical' }}
          placeholder={k('fieldDescriptionPlaceholder', 'What this tool does and when to call it')}
        />
      </div>

      {err && (
        <div style={{ color: 'var(--danger, #ff3b30)', fontSize: '12px' }}>{err}</div>
      )}

      <div style={actionsRow}>
        <button className="btn btn-secondary" onClick={onCancel}>{t('common.cancel', 'Cancel')}</button>
        <button
          className="btn btn-primary"
          disabled={submitting || !name.trim() || !displayName.trim()}
          onClick={submit}
        >
          {submitting ? t('common.saving', 'Saving…') : t('common.next', 'Next')}
        </button>
      </div>
    </>
  );
}
