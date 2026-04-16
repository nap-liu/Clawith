import { useState } from 'react';
import type { CliTool } from '../types';

export function BasicInfoStep({
  tool,
  onNext,
  onCancel,
}: {
  tool: CliTool | null;
  onNext: (values: Partial<CliTool>) => Promise<void>;
  onCancel: () => void;
}) {
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

  return (
    <div className="wizard-body" style={{ padding: 12 }}>
      <label style={{ display: 'block', marginBottom: 10 }}>
        Name <span style={{ color: '#ff3b30' }}>*</span>
        <input
          className="form-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={!!tool?.id}
          style={{ width: '100%' }}
        />
        {tool?.id && (
          <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
            Locked after creation.
          </div>
        )}
      </label>
      <label style={{ display: 'block', marginBottom: 10 }}>
        Display name <span style={{ color: '#ff3b30' }}>*</span>
        <input
          className="form-input"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          style={{ width: '100%' }}
        />
      </label>
      <label style={{ display: 'block', marginBottom: 10 }}>
        Description
        <textarea
          className="form-input"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          style={{ width: '100%' }}
        />
      </label>
      {err && <div style={{ color: '#ff3b30', marginBottom: 8 }}>{err}</div>}
      <div className="wizard-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button onClick={onCancel}>Cancel</button>
        <button
          className="btn btn-primary"
          disabled={submitting || !name.trim() || !displayName.trim()}
          onClick={submit}
        >
          {submitting ? 'Saving…' : 'Next'}
        </button>
      </div>
    </div>
  );
}
