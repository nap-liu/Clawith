import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from './api';
import type { CliTool } from './types';
import { CliToolRow } from './CliToolRow';
import { CliToolWizard } from './CliToolWizard';

export function CliToolsSection() {
  const { t } = useTranslation();
  const [tools, setTools] = useState<CliTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<CliTool | null>(null);
  const [creating, setCreating] = useState(false);

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      setTools(await cliToolsApi.list());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
  }, []);

  return (
    <div className="cli-tools-section">
      <div
        className="toolbar"
        style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}
      >
        <h3 style={{ margin: 0 }}>{t('enterprise.cliTools.filterLabel', 'CLI Tools')}</h3>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          + {t('enterprise.cliTools.addButton', 'Add CLI Tool')}
        </button>
      </div>

      {loading && <div>Loading…</div>}
      {error && (
        <div style={{ color: '#ff3b30', padding: 8 }}>Failed to load: {error}</div>
      )}
      {!loading && !error && tools.length === 0 && (
        <div className="empty" style={{ padding: 24, color: 'var(--text-secondary)', textAlign: 'center' }}>
          No CLI tools yet. Click "Add CLI Tool" to create one.
        </div>
      )}

      {!loading && tools.length > 0 && (
        <table
          className="tool-table"
          style={{ width: '100%', borderCollapse: 'collapse' }}
        >
          <thead>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              <th style={{ textAlign: 'left', padding: '8px 6px' }}>Name</th>
              <th style={{ textAlign: 'left', padding: '8px 6px' }}>
                {t('enterprise.cliTools.columnScope', 'Scope')}
              </th>
              <th style={{ textAlign: 'left', padding: '8px 6px' }}>
                {t('enterprise.cliTools.columnStatus', 'Status')}
              </th>
              <th style={{ textAlign: 'left', padding: '8px 6px' }}>
                {t('enterprise.cliTools.columnUpdated', 'Updated')}
              </th>
              <th style={{ textAlign: 'left', padding: '8px 6px' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <CliToolRow
                key={tool.id}
                tool={tool}
                onEdit={() => setEditing(tool)}
                onChange={reload}
              />
            ))}
          </tbody>
        </table>
      )}

      {(creating || editing) && (
        <CliToolWizard
          tool={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
            reload();
          }}
        />
      )}
    </div>
  );
}
