import React, { useEffect, useRef, useState } from 'react';
import PlaceholderField from './PlaceholderField';

interface Props {
  /** Current key-value mapping. */
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  /** Label shown above the editor. */
  label: string;
  /** Placeholder for the key column. */
  keyPlaceholder?: string;
  /** Placeholder for the value column. */
  valuePlaceholder?: string;
  /** Button label for adding a new row. */
  addLabel?: string;
  /**
   * When a key name matches this predicate, the value field is rendered as a
   * password input (via PlaceholderField's `password` prop).
   */
  isSecretKey?: (key: string) => boolean;
  /** Helper hint shown beneath the editor. */
  helperHint?: string;
}

type Pair = { key: string; value: string };

const recordToPairs = (r: Record<string, string>): Pair[] =>
  Object.entries(r).map(([key, value]) => ({ key, value }));

const pairsToRecord = (pairs: Pair[]): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const { key, value } of pairs) {
    if (key) out[key] = value;
  }
  return out;
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '6px 8px',
  fontSize: 12,
  background: 'var(--bg-secondary)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 4,
  color: 'var(--text-primary)',
  fontFamily: 'ui-monospace, monospace',
};

/**
 * Generic key-value editor used for HTTP headers templates and env var
 * templates. Supports placeholder syntax in values and optional password
 * masking for secret-looking keys.
 */
export default function KeyValueEditor({
  value,
  onChange,
  label,
  keyPlaceholder = 'Key',
  valuePlaceholder = 'Value (支持 ${...} 占位符)',
  addLabel = '+ 添加',
  isSecretKey,
  helperHint,
}: Props) {
  // Maintain a stable-ordered array so React can reconcile rows without
  // clobbering focus mid-typing (same approach as OverrideFieldsEditor).
  const [pairs, setPairs] = useState<Pair[]>(() => recordToPairs(value));
  const lastEmittedRef = useRef<string>(JSON.stringify(pairsToRecord(pairs)));

  // Sync from parent only when the incoming value differs from what we last
  // emitted, so we don't clobber the user's mid-edit state.
  useEffect(() => {
    const incoming = JSON.stringify(value ?? {});
    if (incoming !== lastEmittedRef.current) {
      setPairs(recordToPairs(value ?? {}));
      lastEmittedRef.current = incoming;
    }
  }, [value]);

  const commit = (next: Pair[]) => {
    setPairs(next);
    const record = pairsToRecord(next);
    lastEmittedRef.current = JSON.stringify(record);
    onChange(record);
  };

  const updatePair = (idx: number, nextKey: string, nextValue: string) => {
    commit(pairs.map((p, i) => (i === idx ? { key: nextKey, value: nextValue } : p)));
  };

  const removePair = (idx: number) => {
    commit(pairs.filter((_, i) => i !== idx));
  };

  const addPair = () => {
    commit([...pairs, { key: '', value: '' }]);
  };

  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{
        display: 'block', fontSize: 12,
        color: 'var(--text-secondary)', marginBottom: 6,
      }}>
        {label}
      </label>

      {pairs.map((p, idx) => {
        const secret = isSecretKey ? isSecretKey(p.key) : false;
        return (
          <div key={idx} style={{ display: 'flex', gap: 6, marginBottom: 6, alignItems: 'flex-start' }}>
            <input
              value={p.key}
              placeholder={keyPlaceholder}
              onChange={(e) => updatePair(idx, e.target.value, p.value)}
              style={{ ...inputStyle, flex: '0 0 35%' }}
            />
            {secret ? (
              /* Use PlaceholderField for password masking, but we need to strip
                 the label wrapper — so we replicate just the input part here. */
              <input
                type="password"
                value={p.value}
                placeholder={valuePlaceholder}
                onChange={(e) => updatePair(idx, p.key, e.target.value)}
                autoComplete="new-password"
                style={{ ...inputStyle, flex: 1 }}
              />
            ) : (
              <input
                value={p.value}
                placeholder={valuePlaceholder}
                onChange={(e) => updatePair(idx, p.key, e.target.value)}
                style={{ ...inputStyle, flex: 1 }}
              />
            )}
            <button
              type="button"
              onClick={() => removePair(idx)}
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
        );
      })}

      <button
        type="button"
        onClick={addPair}
        style={{
          marginTop: 4, fontSize: 11,
          background: 'none', border: '1px dashed var(--border-subtle)',
          borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
          color: 'var(--text-secondary)',
        }}
      >
        {addLabel}
      </button>

      {helperHint && (
        <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-tertiary)' }}>
          {helperHint}
        </div>
      )}
    </div>
  );
}
