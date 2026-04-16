import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { CliTool, CliToolConfig } from '../types';
import { EnvGrid } from '../EnvGrid';
import { TestRunPanel } from '../TestRunPanel';

export function ConfigStep({
  tool,
  onUpdated,
  onBack,
  onDone,
}: {
  tool: CliTool;
  onUpdated: (updated: CliTool) => void;
  onBack: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<CliToolConfig>(tool.config);
  const [argsText, setArgsText] = useState(JSON.stringify(tool.config.args_template));
  const [paramsSchemaText, setParamsSchemaText] = useState(
    JSON.stringify(tool.parameters_schema, null, 2),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      const parsedArgs = JSON.parse(argsText);
      if (!Array.isArray(parsedArgs)) throw new Error('args_template must be a JSON array');
      const parsedSchema = JSON.parse(paramsSchemaText);

      // Env values equal to "***" mean "keep the stored value" — drop those
      // so PATCH doesn't overwrite encrypted state with the mask literal.
      const cleanedEnv: Record<string, string> = {};
      for (const [k, v] of Object.entries(config.env_inject)) {
        if (v !== '***') cleanedEnv[k] = v;
      }

      const updated = await cliToolsApi.update(tool.id, {
        parameters_schema: parsedSchema,
        config: { ...config, args_template: parsedArgs, env_inject: cleanedEnv },
      });
      onUpdated(updated);
      onDone();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="wizard-body" style={{ padding: 12 }}>
      <label style={{ display: 'block', marginBottom: 10 }}>
        Args template (JSON array)
        <textarea
          className="form-input"
          value={argsText}
          onChange={(e) => setArgsText(e.target.value)}
          rows={3}
          style={{ width: '100%', fontFamily: 'monospace' }}
        />
        <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
          Supported placeholders: {'{user.id}'} {'{user.phone}'} {'{user.email}'} {'{agent.id}'} {'{tenant.id}'} {'{params.xxx}'}
        </div>
      </label>

      <div style={{ marginBottom: 10 }}>
        <div style={{ fontWeight: 500, marginBottom: 4 }}>Env vars</div>
        <EnvGrid env={config.env_inject} onChange={(env) => setConfig({ ...config, env_inject: env })} />
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
        <label style={{ flex: 1 }}>
          Timeout (s)
          <input
            className="form-input"
            type="number"
            value={config.timeout_seconds}
            onChange={(e) => setConfig({ ...config, timeout_seconds: Number(e.target.value) || 30 })}
            style={{ width: '100%' }}
          />
        </label>
        <label style={{ flex: 1 }}>
          CPU
          <input
            className="form-input"
            value={config.sandbox.cpu_limit}
            onChange={(e) =>
              setConfig({ ...config, sandbox: { ...config.sandbox, cpu_limit: e.target.value } })
            }
            style={{ width: '100%' }}
          />
        </label>
        <label style={{ flex: 1 }}>
          Memory
          <input
            className="form-input"
            value={config.sandbox.memory_limit}
            onChange={(e) =>
              setConfig({ ...config, sandbox: { ...config.sandbox, memory_limit: e.target.value } })
            }
            style={{ width: '100%' }}
          />
        </label>
      </div>

      <label style={{ display: 'block', marginBottom: 4 }}>
        <input
          type="checkbox"
          checked={config.sandbox.network}
          onChange={(e) =>
            setConfig({ ...config, sandbox: { ...config.sandbox, network: e.target.checked } })
          }
        />{' '}
        Allow network
      </label>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 10 }}>
        {t('enterprise.cliTools.sandbox.networkHint', 'Enable only if the tool needs to call external APIs or download resources.')}
      </div>

      <label style={{ display: 'block', marginBottom: 10 }}>
        Sandbox image (leave blank to follow platform default)
        <input
          className="form-input"
          value={config.sandbox.image ?? ''}
          placeholder="clawith-cli-sandbox:stable (default)"
          onChange={(e) =>
            setConfig({
              ...config,
              sandbox: { ...config.sandbox, image: e.target.value.trim() || null },
            })
          }
          style={{ width: '100%' }}
        />
      </label>

      <label style={{ display: 'block', marginBottom: 10 }}>
        Parameters schema (JSON Schema)
        <textarea
          className="form-input"
          value={paramsSchemaText}
          onChange={(e) => setParamsSchemaText(e.target.value)}
          rows={8}
          style={{ width: '100%', fontFamily: 'monospace' }}
        />
      </label>

      <TestRunPanel tool={tool} />

      {error && <div style={{ color: '#ff3b30', marginTop: 8 }}>{error}</div>}
      <div className="wizard-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
        <button onClick={onBack}>Back</button>
        <button className="btn btn-primary" disabled={saving} onClick={save}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}
