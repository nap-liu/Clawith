import React from 'react';

interface Props {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  resolved?: string;
  multiline?: boolean;
  disabled?: boolean;
  password?: boolean;
  helperHint?: string;
}

export default function PlaceholderField({
  label, value, onChange, placeholder, resolved,
  multiline, disabled, password, helperHint,
}: Props) {
  const showResolved = !!value && !!resolved && resolved !== value;
  const inputType = password ? 'password' : 'text';
  const commonStyle: React.CSSProperties = {
    width: '100%', boxSizing: 'border-box',
    padding: 8, fontSize: 12,
    background: disabled ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
    border: '1px solid var(--border-subtle)', borderRadius: 6,
    color: 'var(--text-primary)',
    fontFamily: multiline ? 'ui-monospace, SF Mono, monospace' : undefined,
    resize: multiline ? 'vertical' : undefined,
  };
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: 'block', marginBottom: 4, fontSize: 12, color: 'var(--text-secondary)' }}>{label}</label>
      {multiline ? (
        <textarea
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          rows={4}
          style={commonStyle}
        />
      ) : (
        <input
          type={inputType}
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          style={commonStyle}
        />
      )}
      {showResolved && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-tertiary)' }}>
          ↳ 解析后：<code style={{ color: 'var(--text-secondary)' }}>{resolved}</code>
        </div>
      )}
      {helperHint && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-tertiary)' }}>{helperHint}</div>
      )}
    </div>
  );
}
