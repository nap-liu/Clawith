import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { CliTool } from '../types';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

const actionsRow: React.CSSProperties = {
  display: 'flex', gap: '8px', marginTop: '4px',
  justifyContent: 'flex-end',
  borderTop: '1px solid var(--border-subtle)', paddingTop: '16px',
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

  const sha = tool.config.binary_sha256;

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
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.wizard.${suffix}`, fb);

  return (
    <>
      <div>
        <label style={labelStyle}>{k('fieldBinary', 'Binary')}</label>
        {sha ? (
          <div className="card" style={{ padding: '10px 12px', fontSize: '12px' }}>
            <div><strong>{tool.config.binary_original_name}</strong></div>
            <div style={{ color: 'var(--text-secondary)' }}>
              {tool.config.binary_size?.toLocaleString()} bytes
              {tool.config.binary_uploaded_at && (
                <> · {new Date(tool.config.binary_uploaded_at).toLocaleString()}</>
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
