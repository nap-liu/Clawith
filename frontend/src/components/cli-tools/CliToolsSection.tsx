// Self-contained CLI Tools admin panel.
//
// This component owns the full lifecycle for CLI tools: list, enable/disable,
// edit (via wizard), delete. It is dropped into EnterpriseSettings' tools tab
// alongside (but independent of) the MCP servers section.
//
// Why self-contained: previous fork put CLI tool state on the giant
// EnterpriseSettings page. After a take-theirs upstream merge that page no
// longer carries the state, so we restore the feature here without re-coupling.
//
// Backend endpoints used (all rooted at /api/tools/cli, see api.ts):
//   GET    /api/tools/cli           list
//   GET    /api/tools/cli/{id}      detail (used to seed the wizard on edit)
//   PATCH  /api/tools/cli/{id}      update (is_active toggle)
//   DELETE /api/tools/cli/{id}      delete

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CliToolWizard } from './CliToolWizard';
import { cliToolsApi } from './api';
import type { CliTool } from './types';

export interface CliToolsSectionProps {
  // Optional tenant filter passed by the parent. Currently the backend
  // returns the full list and tenant filtering is done elsewhere; the prop
  // is reserved for future per-tenant scoping without forcing a re-export.
  tenantId?: string | null;
}

export function CliToolsSection({ tenantId: _tenantId }: CliToolsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [showWizard, setShowWizard] = useState(false);
  const [editingTool, setEditingTool] = useState<CliTool | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const listQuery = useQuery<CliTool[], Error>({
    queryKey: ['cli-tools'],
    queryFn: () => cliToolsApi.list(),
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['cli-tools'] });

  const toggleMutation = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) =>
      cliToolsApi.update(id, { is_active }),
    onSuccess: () => refresh(),
    onError: (err: Error) => setErrorMsg(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => cliToolsApi.delete(id),
    onSuccess: () => refresh(),
    onError: (err: Error) => setErrorMsg(err.message),
  });

  const openEdit = async (tool: CliTool) => {
    try {
      // Re-fetch to get the freshest config (binary metadata, env, etc.)
      // before handing it to the wizard.
      const full = await cliToolsApi.get(tool.id);
      setEditingTool(full);
      setShowWizard(true);
    } catch (err) {
      setErrorMsg((err as Error).message);
    }
  };

  const openCreate = () => {
    setEditingTool(null);
    setShowWizard(true);
  };

  const closeWizard = () => {
    setShowWizard(false);
    setEditingTool(null);
    refresh();
  };

  const tools = listQuery.data ?? [];

  return (
    <div style={{ marginBottom: '24px' }}>
      {/* Section header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '12px',
        }}
      >
        <div>
          <h3 style={{ margin: 0, fontSize: '15px' }}>
            {t('enterprise.cliTools.filterLabel', 'CLI Tools')}
          </h3>
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
            {t(
              'enterprise.cliTools.wizard.subtitle',
              'Upload a binary, configure env, run a test',
            )}
          </div>
        </div>
        <button className="btn btn-secondary" onClick={openCreate}>
          + {t('enterprise.cliTools.addButton', 'Add CLI Tool')}
        </button>
      </div>

      {errorMsg && (
        <div
          className="card"
          style={{
            padding: '8px 12px',
            marginBottom: '12px',
            background: 'rgba(239,68,68,0.1)',
            color: 'var(--danger, #ef4444)',
            fontSize: '12px',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <span>{errorMsg}</span>
          <button
            onClick={() => setErrorMsg(null)}
            style={{
              background: 'none',
              border: 'none',
              color: 'inherit',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            ✕
          </button>
        </div>
      )}

      {/* Loading / empty / list */}
      {listQuery.isLoading && (
        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '12px 0' }}>
          {t('common.loading', 'Loading...')}
        </div>
      )}

      {!listQuery.isLoading && tools.length === 0 && (
        <div
          className="card"
          style={{
            padding: '20px',
            textAlign: 'center',
            color: 'var(--text-tertiary)',
            fontSize: '12px',
          }}
        >
          {t('enterprise.cliTools.empty', 'No CLI tools yet. Click "Add CLI Tool" to upload one.')}
        </div>
      )}

      {!listQuery.isLoading && tools.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {tools.map((tool) => (
            <CliToolRow
              key={tool.id}
              tool={tool}
              onToggle={(checked) =>
                toggleMutation.mutate({ id: tool.id, is_active: checked })
              }
              onEdit={() => openEdit(tool)}
              onDelete={() => {
                const label = tool.display_name || tool.name;
                if (!confirm(`${t('common.delete', 'Delete')} ${label}?`)) return;
                deleteMutation.mutate(tool.id);
              }}
            />
          ))}
        </div>
      )}

      {/* Create / edit wizard */}
      {showWizard && <CliToolWizard tool={editingTool} onClose={closeWizard} />}
    </div>
  );
}

// ─── Single row ────────────────────────────────────────────────────────────

interface CliToolRowProps {
  tool: CliTool;
  onToggle: (checked: boolean) => void;
  onEdit: () => void;
  onDelete: () => void;
}

function CliToolRow({ tool, onToggle, onEdit, onDelete }: CliToolRowProps) {
  const { t } = useTranslation();
  const enabled = tool.is_active;
  const hasBinary = !!tool.config?.binary?.sha256;
  const scopeLabel = tool.tenant_id
    ? t('enterprise.cliTools.scopeTenant', 'Tenant')
    : t('enterprise.cliTools.scopeGlobal', 'Global (all tenants)');

  return (
    <div className="card" style={{ padding: '0', overflow: 'hidden' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 14px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1, minWidth: 0 }}>
          <span style={{ fontSize: '18px' }}>🛠️</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
              <span style={{ fontWeight: 500, fontSize: '13px' }}>{tool.display_name}</span>
              <span
                style={{
                  fontSize: '10px',
                  background: 'var(--accent-color, #6366f1)',
                  color: '#fff',
                  borderRadius: '4px',
                  padding: '1px 5px',
                }}
              >
                CLI
              </span>
              <span
                style={{
                  fontSize: '10px',
                  background: 'var(--bg-tertiary)',
                  color: 'var(--text-secondary)',
                  borderRadius: '4px',
                  padding: '1px 5px',
                }}
              >
                {scopeLabel}
              </span>
              {!hasBinary && (
                <span
                  style={{
                    fontSize: '10px',
                    background: 'rgba(239,68,68,0.15)',
                    color: 'var(--danger, #ef4444)',
                    borderRadius: '4px',
                    padding: '1px 5px',
                  }}
                  title={t(
                    'enterprise.cliTools.wizard.binaryEmpty',
                    'No binary yet. Accepted: ELF / Mach-O / shebang script. Max 100 MB.',
                  )}
                >
                  {t('enterprise.cliTools.noBinary', 'No binary')}
                </span>
              )}
            </div>
            <div
              style={{
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {tool.description || tool.name}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          <button
            className="btn btn-secondary"
            style={{ padding: '4px 8px', fontSize: '11px' }}
            onClick={onEdit}
          >
            ⚙️ {t('common.edit', 'Edit')}
          </button>
          <button
            className="btn btn-danger"
            style={{ padding: '4px 8px', fontSize: '11px' }}
            onClick={onDelete}
          >
            {t('common.delete', 'Delete')}
          </button>

          {/* Enable toggle */}
          <label
            style={{
              position: 'relative',
              display: 'inline-block',
              width: '40px',
              height: '22px',
              cursor: 'pointer',
              flexShrink: 0,
            }}
            title={
              enabled
                ? t('enterprise.cliTools.statusActive', 'Active')
                : t('enterprise.cliTools.statusDisabled', 'Disabled')
            }
          >
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => onToggle(e.target.checked)}
              style={{ opacity: 0, width: 0, height: 0 }}
            />
            <span
              style={{
                position: 'absolute',
                inset: 0,
                background: enabled ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                borderRadius: '11px',
                transition: 'background 0.2s',
              }}
            >
              <span
                style={{
                  position: 'absolute',
                  left: enabled ? '20px' : '2px',
                  top: '2px',
                  width: '18px',
                  height: '18px',
                  background: '#fff',
                  borderRadius: '50%',
                  transition: 'left 0.2s',
                }}
              />
            </span>
          </label>
        </div>
      </div>
    </div>
  );
}

export default CliToolsSection;
