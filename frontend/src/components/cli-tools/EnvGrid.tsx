import { useState } from 'react';

export function EnvGrid({
  env,
  onChange,
}: {
  env: Record<string, string>;
  onChange: (env: Record<string, string>) => void;
}) {
  const [newKey, setNewKey] = useState('');
  const [newValue, setNewValue] = useState('');

  const setPair = (key: string, value: string) => onChange({ ...env, [key]: value });
  const remove = (key: string) => {
    const next = { ...env };
    delete next[key];
    onChange(next);
  };

  const entries = Object.entries(env);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      {entries.length === 0 && (
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>No env vars.</div>
      )}
      {entries.map(([key, value]) => (
        <div key={key} style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          <code style={{ flex: '0 0 140px', fontSize: '12px', color: 'var(--text-secondary)' }}>{key}</code>
          <input
            className="form-input"
            value={value}
            onChange={(e) => setPair(key, e.target.value)}
            style={{ flex: 1 }}
            placeholder={value === '***' ? 'stored — type to replace' : ''}
          />
          <button
            onClick={() => remove(key)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--text-tertiary)', fontSize: '14px', padding: '4px 8px',
            }}
            title="Remove"
          >✕</button>
        </div>
      ))}
      <div style={{ display: 'flex', gap: '6px', alignItems: 'center', marginTop: '4px' }}>
        <input
          className="form-input"
          placeholder="KEY"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value.toUpperCase())}
          style={{ flex: '0 0 140px' }}
        />
        <input
          className="form-input"
          placeholder="value"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          style={{ flex: 1 }}
        />
        <button
          className="btn btn-secondary"
          style={{ padding: '4px 10px', fontSize: '12px' }}
          disabled={!newKey || newKey in env}
          onClick={() => {
            setPair(newKey, newValue);
            setNewKey('');
            setNewValue('');
          }}
        >
          Add
        </button>
      </div>
    </div>
  );
}
