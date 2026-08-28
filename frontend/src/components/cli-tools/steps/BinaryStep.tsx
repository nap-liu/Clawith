import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconLoader2,
  IconPlayerPause,
  IconRefresh,
  IconUpload,
} from '@tabler/icons-react';
import { formatFileSize } from '../../../utils/formatFileSize';
import { useDialog } from '../../Dialog/DialogProvider';
import { cliToolsApi } from '../api';
import type { BinaryUploadProgress, BinaryUploadTask } from '../api';
import type { BinaryVersion, CliTool } from '../types';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

const actionsRow: React.CSSProperties = {
  display: 'flex', gap: '8px', marginTop: '4px', justifyContent: 'flex-end',
  borderTop: '1px solid var(--border-subtle)', paddingTop: '16px',
};

const versionRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
  padding: '8px 10px', fontSize: '11px', borderBottom: '1px solid var(--border-subtle)',
};

function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—';
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) return `${rounded}s`;
  return `${Math.floor(rounded / 60)}m ${rounded % 60}s`;
}

export function BinaryStep({
  tool, onReplaced, onBack, onNext,
}: {
  tool: CliTool;
  onReplaced: (updated: CliTool) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const { t } = useTranslation();
  const dialog = useDialog();
  const [uploading, setUploading] = useState(false);
  const [paused, setPaused] = useState(false);
  const [progress, setProgress] = useState<BinaryUploadProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [versions, setVersions] = useState<BinaryVersion[] | null>(null);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [rollingBack, setRollingBack] = useState<string | null>(null);
  const taskRef = useRef<BinaryUploadTask | null>(null);

  const sha = tool.config.binary.sha256;
  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.wizard.${suffix}`, fb);

  const refreshVersions = async () => {
    try {
      setVersions(await cliToolsApi.listVersions(tool.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (versionsOpen && versions === null) void refreshVersions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [versionsOpen]);

  useEffect(() => () => taskRef.current?.pause(), []);

  const upload = async (file: File) => {
    const warn = t(
      'enterprise.cliTools.wizard.replaceWarning',
      'Replacing the binary affects every agent using this tool. New version takes effect on the next execute.',
    );
    if (sha) {
      const confirmed = await dialog.confirm(warn, {
        title: k('replaceTitle', 'Replace binary'),
        confirmLabel: k('replaceAction', 'Replace'),
      });
      if (!confirmed) return;
    }
    setError(null);
    setPaused(false);
    setUploading(true);
    const task = cliToolsApi.uploadBinaryResumable(tool.id, file, setProgress);
    taskRef.current = task;
    try {
      const updated = await task.promise;
      onReplaced(updated);
      setProgress(null);
      if (versionsOpen) await refreshVersions();
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') setPaused(true);
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      taskRef.current = null;
      setUploading(false);
    }
  };

  const rollback = async (version: BinaryVersion) => {
    if (version.is_current) return;
    const confirmed = await dialog.confirm(
      k('rollbackConfirm', 'Roll back to this version? Takes effect on next execution.'),
      {
        title: k('rollbackTitle', 'Roll back binary'),
        confirmLabel: k('rollbackAction', 'Roll back'),
      },
    );
    if (!confirmed) return;
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

  const uploadLabel = uploading
    ? progress?.phase === 'finalizing' ? k('uploadFinalizing', 'Verifying…') : k('btnUploading', 'Uploading…')
    : sha ? k('btnReplace', 'Replace') : k('btnUpload', 'Upload');

  return (
    <>
      <div>
        <label style={labelStyle}>{k('fieldBinary', 'Binary')}</label>
        {sha ? (
          <div className="card" style={{ padding: '10px 12px', fontSize: '12px' }}>
            <div><strong>{tool.config.binary.original_name}</strong></div>
            <div style={{ color: 'var(--text-secondary)' }}>
              {formatFileSize(tool.config.binary.size ?? 0)}
              {tool.config.binary.uploaded_at && <> · {new Date(tool.config.binary.uploaded_at).toLocaleString()}</>}
            </div>
            <div style={{ color: 'var(--text-tertiary)', wordBreak: 'break-all', marginTop: '4px' }}>
              SHA-256: <code style={{ fontSize: '10px' }}>{sha}</code>
            </div>
          </div>
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {k('binaryEmpty', 'No binary yet. Accepted: ELF / Mach-O / shebang script.')}
          </div>
        )}
      </div>

      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <label
          className="btn btn-secondary"
          style={{
            display: 'inline-flex', alignItems: 'center', gap: '6px', position: 'relative',
            cursor: uploading ? 'not-allowed' : 'pointer', opacity: uploading ? 0.6 : 1,
          }}
        >
          {uploading ? <IconLoader2 size={16} /> : sha ? <IconRefresh size={16} /> : <IconUpload size={16} />}
          {uploadLabel}
          <input
            type="file"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void upload(file);
              event.target.value = '';
            }}
            disabled={uploading}
            aria-label={sha ? k('btnReplace', 'Replace') : k('btnUpload', 'Upload')}
            style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', opacity: 0, cursor: 'inherit' }}
          />
        </label>
        {uploading && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => taskRef.current?.pause()}
            style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
          >
            <IconPlayerPause size={16} /> {k('uploadPause', 'Pause')}
          </button>
        )}
      </div>

      {progress && (
        <div className="card" style={{ padding: '12px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', fontSize: '12px' }}>
            <span style={{ fontWeight: 600 }}>
              {paused
                ? k('uploadPaused', 'Upload paused')
                : progress.phase === 'finalizing'
                  ? k('uploadFinalizing', 'Verifying…')
                  : progress.resumed ? k('uploadResumed', 'Resuming upload') : k('btnUploading', 'Uploading…')}
            </span>
            <span>{progress.percent.toFixed(1)}%</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress.percent}
            style={{ height: '7px', borderRadius: '999px', background: 'var(--bg-tertiary)', overflow: 'hidden' }}
          >
            <div style={{
              width: `${progress.percent}%`, height: '100%', borderRadius: 'inherit',
              background: 'var(--accent-primary)', transition: 'width 120ms linear',
            }} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', color: 'var(--text-tertiary)', fontSize: '11px' }}>
            <span>{formatFileSize(progress.loaded)} / {formatFileSize(progress.total)}</span>
            <span>
              {progress.phase === 'finalizing'
                ? k('uploadFinalizingHint', 'Checking file integrity')
                : `${formatFileSize(progress.bytesPerSecond)}/s · ${k('uploadEta', 'ETA')} ${formatDuration(progress.etaSeconds)}`}
            </span>
          </div>
          {paused && (
            <div style={{ color: 'var(--text-secondary)', fontSize: '11px' }}>
              {k('uploadResumeHint', 'Select the same file again to continue from this point.')}
            </div>
          )}
        </div>
      )}

      {sha && (
        <div>
          <button
            type="button"
            className="btn btn-secondary"
            style={{ fontSize: '11px', padding: '4px 8px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
            onClick={() => setVersionsOpen(!versionsOpen)}
          >
            {versionsOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
            {k('binaryVersions', 'Version history')}
          </button>
          {versionsOpen && (
            <div className="card" style={{ marginTop: '8px', padding: 0 }}>
              {versions === null ? (
                <div style={{ padding: '10px 12px', fontSize: '11px', color: 'var(--text-secondary)' }}>
                  {k('loadingVersions', 'Loading…')}
                </div>
              ) : versions.length === 0 ? (
                <div style={{ padding: '10px 12px', fontSize: '11px', color: 'var(--text-secondary)' }}>
                  {k('noVersions', 'No previous versions yet')}
                </div>
              ) : versions.map((version) => (
                <div key={version.id} style={versionRowStyle}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '2px', minWidth: 0, flex: 1 }}>
                    <div>
                      <code style={{ fontSize: '10px' }}>{version.sha256.slice(0, 12)}</code>{' · '}
                      <span style={{ fontWeight: version.is_current ? 600 : 400 }}>{version.original_name}</span>
                      {version.is_current && (
                        <span style={{ marginLeft: '6px', color: 'var(--success, #34c759)', display: 'inline-flex', alignItems: 'center', gap: '2px' }}>
                          <IconCheck size={12} /> {k('versionCurrent', 'current')}
                        </span>
                      )}
                    </div>
                    <div style={{ color: 'var(--text-tertiary)', fontSize: '10px' }}>
                      {formatFileSize(version.size)} · {new Date(version.uploaded_at).toLocaleString()}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    style={{ fontSize: '11px', padding: '3px 8px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                    disabled={version.is_current || rollingBack !== null}
                    onClick={() => rollback(version)}
                  >
                    {rollingBack === version.id && <IconLoader2 size={13} />}
                    {rollingBack === version.id ? k('loadingVersions', 'Loading…') : k('btnRollback', 'Rollback')}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {error && <div style={{ color: 'var(--danger, #ff3b30)', fontSize: '12px' }}>{error}</div>}

      <div style={actionsRow}>
        <button className="btn btn-secondary" onClick={onBack}>{t('common.back', 'Back')}</button>
        <button className="btn btn-primary" disabled={!sha || uploading} onClick={onNext}>
          {t('common.next', 'Next')}
        </button>
      </div>
    </>
  );
}
