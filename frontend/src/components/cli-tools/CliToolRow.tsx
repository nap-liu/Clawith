import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from './api';
import type { CliTool } from './types';

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString();
}

export function CliToolRow({
  tool,
  onEdit,
  onChange,
}: {
  tool: CliTool;
  onEdit: () => void;
  onChange: () => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  const isGlobal = tool.tenant_id === null;
  const scopeLabel = isGlobal
    ? t('enterprise.cliTools.scopeGlobal', 'Global')
    : t('enterprise.cliTools.scopeTenant', 'Tenant');

  const toggleActive = async () => {
    setBusy(true);
    try {
      await cliToolsApi.update(tool.id, { is_active: !tool.is_active });
      onChange();
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirm(`Delete ${tool.display_name}?`)) return;
    setBusy(true);
    try {
      await cliToolsApi.delete(tool.id);
      onChange();
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr>
      <td>
        <strong>{tool.display_name}</strong>
        <div className="muted" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
          {tool.name}
        </div>
      </td>
      <td>
        <span
          className={`scope-tag ${isGlobal ? 'global' : 'tenant'}`}
          style={{
            padding: '2px 8px',
            borderRadius: 4,
            fontSize: 12,
            background: isGlobal ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
            color: isGlobal ? '#fff' : 'var(--text-secondary)',
          }}
        >
          {scopeLabel}
        </span>
      </td>
      <td>
        <span
          style={{
            display: 'inline-block',
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: tool.is_active ? '#34c759' : '#8e8e93',
            marginRight: 6,
          }}
        />
        {tool.is_active
          ? t('enterprise.cliTools.statusActive', 'Active')
          : t('enterprise.cliTools.statusDisabled', 'Disabled')}
      </td>
      <td>{formatDate(tool.config?.binary_uploaded_at ?? null)}</td>
      <td className="row-actions" style={{ display: 'flex', gap: 6 }}>
        <button disabled={busy} onClick={onEdit}>Edit</button>
        <button disabled={busy} onClick={toggleActive}>
          {tool.is_active ? 'Disable' : 'Enable'}
        </button>
        <button disabled={busy} onClick={remove} style={{ color: '#ff3b30' }}>Delete</button>
      </td>
    </tr>
  );
}
