import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { CliTool } from '../types';

export function BinaryStep({
  tool,
  onReplaced,
  onBack,
  onNext,
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
      'This replaces the binary for all agents using this tool. The new version takes effect on the next execute.',
    );
    if (sha && !confirm(warn)) return;
    setError(null);
    setUploading(true);
    try {
      const updated = await cliToolsApi.uploadBinary(tool.id, file);
      onReplaced(updated);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="wizard-body" style={{ padding: 12 }}>
      {sha ? (
        <div
          className="binary-current"
          style={{
            padding: 10,
            background: 'var(--bg-secondary)',
            borderRadius: 6,
            marginBottom: 12,
          }}
        >
          <strong>Current binary</strong>
          <div>Name: <code>{tool.config.binary_original_name}</code></div>
          <div>Size: {tool.config.binary_size} bytes</div>
          <div style={{ wordBreak: 'break-all' }}>SHA-256: <code>{sha}</code></div>
          {tool.config.binary_uploaded_at && (
            <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
              Uploaded {new Date(tool.config.binary_uploaded_at).toLocaleString()}
            </div>
          )}
        </div>
      ) : (
        <div className="binary-empty" style={{ marginBottom: 12, color: 'var(--text-secondary)' }}>
          No binary uploaded yet. Supported: ELF / Mach-O / shebang scripts. Max 100 MB.
        </div>
      )}

      <label
        className="file-picker"
        style={{
          display: 'inline-block',
          padding: '8px 14px',
          border: '1px dashed var(--border)',
          borderRadius: 6,
          cursor: uploading ? 'not-allowed' : 'pointer',
        }}
      >
        {sha ? '📤 Replace binary' : '📤 Upload binary'}
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
      {uploading && <div style={{ marginTop: 6 }}>Uploading…</div>}
      {error && <div style={{ color: '#ff3b30', marginTop: 6 }}>{error}</div>}

      <div className="wizard-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
        <button onClick={onBack}>Back</button>
        <button className="btn btn-primary" disabled={!sha} onClick={onNext}>Next</button>
      </div>
    </div>
  );
}
