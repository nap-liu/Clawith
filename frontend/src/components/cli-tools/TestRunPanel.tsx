import { useState } from 'react';
import { cliToolsApi } from './api';
import type { CliTool, TestRunResponse } from './types';

export function TestRunPanel({ tool }: { tool: CliTool }) {
  const [paramsText, setParamsText] = useState('{}');
  const [mockEnvText, setMockEnvText] = useState('{}');
  const [result, setResult] = useState<TestRunResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

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
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div
      className="test-run-panel"
      style={{
        padding: 12,
        border: '1px solid var(--border)',
        borderRadius: 6,
        marginTop: 12,
      }}
    >
      <h4 style={{ margin: '0 0 8px' }}>🧪 Test Run</h4>
      <label style={{ display: 'block', marginBottom: 8 }}>
        Params (JSON)
        <textarea
          className="form-input"
          value={paramsText}
          onChange={(e) => setParamsText(e.target.value)}
          rows={3}
          style={{ width: '100%', fontFamily: 'monospace' }}
        />
      </label>
      <label style={{ display: 'block', marginBottom: 8 }}>
        Mock env (JSON; leave <code>{'{}'}</code> to use stored values)
        <textarea
          className="form-input"
          value={mockEnvText}
          onChange={(e) => setMockEnvText(e.target.value)}
          rows={2}
          style={{ width: '100%', fontFamily: 'monospace' }}
        />
      </label>
      <button
        className="btn btn-primary"
        disabled={running || !tool.config.binary_sha256}
        onClick={run}
      >
        {running ? 'Running…' : 'Run'}
      </button>
      {!tool.config.binary_sha256 && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
          Upload a binary first.
        </div>
      )}

      {err && (
        <div style={{ color: '#ff3b30', marginTop: 8 }}>{err}</div>
      )}
      {result && (
        <div style={{ marginTop: 10 }}>
          <div>
            exit_code: <code>{result.exit_code}</code> · duration: {result.duration_ms} ms
          </div>
          {result.error_class && (
            <div style={{ color: '#ff3b30', marginTop: 4 }}>
              [{result.error_class}] {result.error_message}
            </div>
          )}
          {result.stdout && (
            <details open style={{ marginTop: 6 }}>
              <summary>stdout</summary>
              <pre
                style={{
                  background: 'var(--bg-secondary)',
                  padding: 8,
                  borderRadius: 4,
                  overflow: 'auto',
                }}
              >
                {result.stdout}
              </pre>
            </details>
          )}
          {result.stderr && (
            <details style={{ marginTop: 6 }}>
              <summary>stderr</summary>
              <pre
                style={{
                  background: 'var(--bg-secondary)',
                  padding: 8,
                  borderRadius: 4,
                  overflow: 'auto',
                }}
              >
                {result.stderr}
              </pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
