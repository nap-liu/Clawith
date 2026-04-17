import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { BinaryVersion, CliTool } from '../types';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

const actionsRow: React.CSSProperties = {
  display: 'flex', gap: '8px', marginTop: '4px',
  justifyContent: 'flex-end',
  borderTop: '1px solid var(--border-subtle)', paddingTop: '16px',
};

const versionRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
  padding: '8px 10px', fontSize: '11px',
  borderBottom: '1px solid var(--border-subtle)',
};

export function BinaryStep({
  tool, onReplaced, onBack, onNext,
}: {
  tool: CliTool;
  onReplaced: (updated: CliTool) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const { t } = useTranslation();
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [versions, setVersions] = useState<BinaryVersion[] | null>(null);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [rollingBack, setRollingBack] = useState<string | null>(null);

  const sha = tool.config.binary.sha256;

  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.wizard.${suffix}`, fb);

  // Load versions lazily — only when the admin opens the history panel.
  // Reload on every successful upload / rollback so the list stays fresh
  // without a page refresh.
  const refreshVersions = async () => {
    try {
      const rows = await cliToolsApi.listVersions(tool.id);
      setVersions(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (versionsOpen && versions === null) {
      void refreshVersions();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [versionsOpen]);

  const upload = async (file: File) => {
    const warn = t(
      'enterprise.cliTools.wizard.replaceWarning',
      'Replacing the binary affects every agent using this tool. New version takes effect on the next execute.',
    );
    if (sha && !confirm(warn)) return;
    setError(null);
    setUploading(true);
    try {
      const updated = await cliToolsApi.uploadBinary(tool.id, file);
      onReplaced(updated);
      if (versionsOpen) await refreshVersions();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  const rollback = async (version: BinaryVersion) => {
    if (version.is_current) return;
    const msg = k('rollbackConfirm', 'Roll back to this version? Takes effect on next execution.');
    if (!confirm(msg)) return;
    setError(null);
    setRollingBack(version.id);
    try {
      const updated = await cliToolsApi.rollback(tool.id, version.id);
      onReplaced(updated);
      await refreshVersions();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRollingBack(null);
    }
  };

  return (
    <>
      <div>
        <label style={labelStyle}>{k('fieldBinary', 'Binary')}</label>
        {sha ? (
          <div className="card" style={{ padding: '10px 12px', fontSize: '12px' }}>
            <div><strong>{tool.config.binary.original_name}</strong></div>
            <div style={{ color: 'var(--text-secondary)' }}>
              {tool.config.binary.size?.toLocaleString()} bytes
              {tool.config.binary.uploaded_at && (
                <> · {new Date(tool.config.binary.uploaded_at).toLocaleString()}</>
              )}
            </div>
            <div style={{ color: 'var(--text-tertiary)', wordBreak: 'break-all', marginTop: '4px' }}>
              SHA-256: <code style={{ fontSize: '10px' }}>{sha}</code>
            </div>
          </div>
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {k('binaryEmpty', 'No binary yet. Accepted: ELF / Mach-O / shebang script. Max 100 MB.')}
          </div>
        )}
      </div>

      <div>
        <label
          className="btn btn-secondary"
          style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', cursor: uploading ? 'not-allowed' : 'pointer' }}
        >
          {uploading
            ? `⏳ ${k('btnUploading', 'Uploading…')}`
            : sha
              ? `🔄 ${k('btnReplace', 'Replace')}`
              : `📤 ${k('btnUpload', 'Upload')}`}
          <input
            type="file"
            disabled={uploading}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload(f);
              e.target.value = '';
            }}
            style={{ display: 'none' }}
          />
        </label>
      </div>

      {/* Version history — only meaningful once there's a binary. */}
      {sha && (
        <div>
          <button
            type="button"
            className="btn btn-secondary"
            style={{ fontSize: '11px', padding: '4px 8px' }}
            onClick={() => setVersionsOpen(!versionsOpen)}
          >
            {versionsOpen ? '▼' : '▶'} {k('binaryVersions', 'Version history')}
          </button>
          {versionsOpen && (
            <div className="card" style={{ marginTop: '8px', padding: 0 }}>
              {versions === null ? (
                <div style={{ padding: '10px 12px', fontSize: '11px', color: 'var(--text-secondary)' }}>
                  {k('btnUploading', 'Loading…')}
                </div>
              ) : versions.length === 0 ? (
                <div style={{ padding: '10px 12px', fontSize: '11px', color: 'var(--text-secondary)' }}>
                  {k('noVersions', 'No previous versions yet')}
                </div>
              ) : (
                versions.map((v) => (
                  <div key={v.id} style={versionRowStyle}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px', minWidth: 0, flex: 1 }}>
                      <div>
                        <code style={{ fontSize: '10px' }}>{v.sha256.slice(0, 12)}</code>
                        {' · '}
                        <span style={{ fontWeight: v.is_current ? 600 : 400 }}>{v.original_name}</span>
                        {v.is_current && (
                          <span style={{ marginLeft: '6px', color: 'var(--success, #34c759)' }}>
                            ● {k('versionCurrent', 'current')}
                          </span>
                        )}
                      </div>
                      <div style={{ color: 'var(--text-tertiary)', fontSize: '10px' }}>
                        {v.size.toLocaleString()} bytes · {new Date(v.uploaded_at).toLocaleString()}
                      </div>
                    </div>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      style={{ fontSize: '11px', padding: '3px 8px' }}
                      disabled={v.is_current || rollingBack !== null}
                      onClick={() => rollback(v)}
                    >
                      {rollingBack === v.id
                        ? `⏳ ${k('btnUploading', 'Loading…')}`
                        : k('btnRollback', 'Rollback')}
                    </button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}

      {error && (
        <div style={{ color: 'var(--danger, #ff3b30)', fontSize: '12px' }}>{error}</div>
      )}

      <div style={actionsRow}>
        <button className="btn btn-secondary" onClick={onBack}>{t('common.back', 'Back')}</button>
        <button className="btn btn-primary" disabled={!sha} onClick={onNext}>
          {t('common.next', 'Next')}
        </button>
      </div>
    </>
  );
}
