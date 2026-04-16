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

  return (
    <div className="env-grid" style={{ marginTop: 8 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={{ textAlign: 'left', padding: '4px 6px' }}>Name</th>
            <th style={{ textAlign: 'left', padding: '4px 6px' }}>Value</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {Object.entries(env).map(([key, value]) => (
            <tr key={key}>
              <td style={{ padding: '4px 6px' }}><code>{key}</code></td>
              <td style={{ padding: '4px 6px' }}>
                <input
                  className="form-input"
                  value={value}
                  onChange={(e) => setPair(key, e.target.value)}
                  style={{ width: '100%' }}
                />
                {value === '***' && (
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                    (stored — type to replace)
                  </div>
                )}
              </td>
              <td style={{ padding: '4px 6px' }}>
                <button onClick={() => remove(key)}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="env-add" style={{ display: 'flex', gap: 6, marginTop: 6 }}>
        <input
          className="form-input"
          placeholder="KEY"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value.toUpperCase())}
          style={{ flex: 1 }}
        />
        <input
          className="form-input"
          placeholder="value"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          style={{ flex: 2 }}
        />
        <button
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
