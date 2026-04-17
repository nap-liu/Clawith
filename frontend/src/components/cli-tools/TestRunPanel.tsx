import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from './api';
import type { CliTool, TestRunResponse } from './types';

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

export function TestRunPanel({ tool }: { tool: CliTool }) {
  const { t } = useTranslation();
  const [paramsText, setParamsText] = useState('{}');
  const [mockEnvText, setMockEnvText] = useState('{}');
  const [result, setResult] = useState<TestRunResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.testRun.${suffix}`, fb);

  const run = async () => {
    setErr(null);
    setResult(null);
    setRunning(true);
    try {
      const params = JSON.parse(paramsText);
      const mock = JSON.parse(mockEnvText);
      const res = await cliToolsApi.testRun(tool.id, {
        params,
        mock_env: Object.keys(mock).length ? mock : undefined,
      });
      setResult(res);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="card" style={{ padding: '12px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <strong style={{ fontSize: '13px' }}>🧪 {k('title', 'Test Run')}</strong>
        <button
          className="btn btn-primary"
          style={{ padding: '4px 12px', fontSize: '12px' }}
          disabled={running || !tool.config.binary.sha256}
          onClick={run}
        >
          {running ? k('running', 'Running…') : k('run', 'Run')}
        </button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <div>
          <label style={labelStyle}>{k('paramsLabel', 'Params (JSON)')}</label>
          <textarea
            className="form-input"
            value={paramsText}
            onChange={(e) => setParamsText(e.target.value)}
            rows={2}
            style={{ fontFamily: 'monospace', resize: 'vertical' }}
          />
        </div>
        <div>
          <label style={labelStyle}>{k('mockEnvLabel', 'Mock env (JSON)')}</label>
          <textarea
            className="form-input"
            value={mockEnvText}
            onChange={(e) => setMockEnvText(e.target.value)}
            rows={2}
            style={{ fontFamily: 'monospace', resize: 'vertical' }}
            placeholder={k('mockEnvPlaceholder', '{} to use stored values')}
          />
        </div>
      </div>

      {!tool.config.binary.sha256 && (
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '6px' }}>
          {k('needBinary', 'Upload a binary first.')}
        </div>
      )}

      {err && (
        <div style={{ color: 'var(--danger, #ff3b30)', fontSize: '12px', marginTop: '8px' }}>{err}</div>
      )}

      {result && (
        <div style={{ marginTop: '10px', fontSize: '12px' }}>
          <div style={{ color: 'var(--text-secondary)' }}>
            {k('exitCode', 'exit_code')}: <code>{result.exit_code}</code> · {result.duration_ms} {k('duration', 'ms')}
          </div>
          {result.error_class && (
            <div style={{ color: 'var(--danger, #ff3b30)', marginTop: '4px' }}>
              [{result.error_class}] {result.error_message}
            </div>
          )}
          {result.stdout && (
            <details open style={{ marginTop: '6px' }}>
              <summary style={{ cursor: 'pointer' }}>{k('stdout', 'stdout')}</summary>
              <pre style={{ background: 'var(--bg-secondary)', padding: '8px', borderRadius: '4px', overflow: 'auto', margin: '4px 0 0', fontSize: '11px' }}>
                {result.stdout}
              </pre>
            </details>
          )}
          {result.stderr && (
            <details style={{ marginTop: '6px' }}>
              <summary style={{ cursor: 'pointer' }}>{k('stderr', 'stderr')}</summary>
              <pre style={{ background: 'var(--bg-secondary)', padding: '8px', borderRadius: '4px', overflow: 'auto', margin: '4px 0 0', fontSize: '11px' }}>
                {result.stderr}
              </pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
