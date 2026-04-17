import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { CliTool, CliToolConfig, RuntimeConfig, SandboxConfig } from '../types';
import {
  defaultCliToolConfig,
  defaultRuntimeConfig,
  defaultSandboxConfig,
} from '../types';
import { EnvGrid } from '../EnvGrid';
import { TestRunPanel } from '../TestRunPanel';

/**
 * Merge a partial config with defaults so freshly-rendered forms always
 * have every field defined. Legacy rows that still carry the pre-split
 * flat shape get normalised by the backend, but during the rolling
 * upgrade window we may briefly see partial nested shapes too.
 */
function mergeWithDefaults(partial: Partial<CliToolConfig> | null | undefined): CliToolConfig {
  const d = defaultCliToolConfig();
  const c = partial ?? {};
  const mergedSandbox = { ...d.sandbox, ...(c.sandbox ?? {}) };
  const mergedRuntime = {
    ...d.runtime,
    ...(c.runtime ?? {}),
    args_template: c.runtime?.args_template ?? d.runtime.args_template,
    env_inject: c.runtime?.env_inject ?? d.runtime.env_inject,
  };
  return {
    binary: { ...d.binary, ...(c.binary ?? {}) },
    runtime: mergedRuntime,
    sandbox: mergedSandbox,
  };
}

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px',
};

const hintStyle: React.CSSProperties = {
  fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px',
};

const actionsRow: React.CSSProperties = {
  display: 'flex', gap: '8px', marginTop: '4px',
  justifyContent: 'flex-end',
  borderTop: '1px solid var(--border-subtle)', paddingTop: '16px',
};

export function ConfigStep({
  tool, onUpdated, onBack, onDone,
}: {
  tool: CliTool;
  onUpdated: (updated: CliTool) => void;
  onBack: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<CliToolConfig>(() => mergeWithDefaults(tool.config));
  const [argsText, setArgsText] = useState(
    () => JSON.stringify(mergeWithDefaults(tool.config).runtime.args_template),
  );
  const [paramsSchemaText, setParamsSchemaText] = useState(
    JSON.stringify(tool.parameters_schema || {}, null, 2),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateRuntime = (patch: Partial<RuntimeConfig>) =>
    setConfig({ ...config, runtime: { ...config.runtime, ...patch } });
  const updateSandbox = (patch: Partial<SandboxConfig>) =>
    setConfig({ ...config, sandbox: { ...config.sandbox, ...patch } });

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      const parsedArgs = JSON.parse(argsText);
      if (!Array.isArray(parsedArgs)) throw new Error('args_template must be a JSON array');
      const parsedSchema = JSON.parse(paramsSchemaText);

      // The update endpoint refuses any top-level `binary` or `config`
      // key (extra=forbid) — we send only the two admin-editable
      // subtrees. Binary metadata stays whatever the server already
      // has for this tool.
      const runtime: RuntimeConfig = {
        ...defaultRuntimeConfig(),
        ...config.runtime,
        args_template: parsedArgs,
      };
      const sandbox: SandboxConfig = { ...defaultSandboxConfig(), ...config.sandbox };

      const updated = await cliToolsApi.update(tool.id, {
        parameters_schema: parsedSchema,
        runtime,
        sandbox,
      });
      onUpdated(updated);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const k = (suffix: string, fb: string) => t(`enterprise.cliTools.wizard.${suffix}`, fb);

  return (
    <>
      <div>
        <label style={labelStyle}>{k('fieldArgsTemplate', 'Args template (JSON array)')}</label>
        <textarea
          className="form-input"
          value={argsText}
          onChange={(e) => setArgsText(e.target.value)}
          rows={2}
          style={{ fontFamily: 'monospace', resize: 'vertical' }}
        />
        <div style={hintStyle}>
          {k('argsHint', 'Placeholders:')} <code>$user.id</code> <code>$user.phone</code>{' '}
          <code>$user.email</code> <code>$agent.id</code>{' '}
          <code>$tenant.id</code> <code>$params.xxx</code>
        </div>
      </div>

      <div>
        <label style={labelStyle}>{k('fieldEnvVars', 'Env vars')}</label>
        <EnvGrid env={config.runtime.env_inject} onChange={(env) => updateRuntime({ env_inject: env })} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '8px' }}>
        <div>
          <label style={labelStyle}>{k('fieldTimeout', 'Timeout (s)')}</label>
          <input
            type="number"
            className="form-input"
            value={config.runtime.timeout_seconds}
            onChange={(e) => updateRuntime({ timeout_seconds: Number(e.target.value) || 30 })}
          />
        </div>
        <div>
          <label style={labelStyle}>{k('fieldCpu', 'CPU')}</label>
          <input
            className="form-input"
            value={config.sandbox.cpu_limit}
            onChange={(e) => updateSandbox({ cpu_limit: e.target.value })}
          />
        </div>
        <div>
          <label style={labelStyle}>{k('fieldMemory', 'Memory')}</label>
          <input
            className="form-input"
            value={config.sandbox.memory_limit}
            onChange={(e) => updateSandbox({ memory_limit: e.target.value })}
          />
        </div>
      </div>

      <div>
        <label style={labelStyle}>{k('fieldRateLimit', 'Calls per minute')}</label>
        <input
          type="number"
          className="form-input"
          min={0}
          max={10000}
          value={config.runtime.rate_limit_per_minute}
          onChange={(e) => updateRuntime({ rate_limit_per_minute: Math.max(0, Number(e.target.value) || 0) })}
        />
        <div style={hintStyle}>
          {k('rateLimitHint', '0 = unlimited. Protects against runaway agent loops.')}
        </div>
      </div>

      <div>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '13px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={config.runtime.persistent_home}
            onChange={(e) => updateRuntime({ persistent_home: e.target.checked })}
          />
          {k('fieldPersistentHome', 'Persistent HOME per user')}
        </label>
        <div style={hintStyle}>
          {k('persistentHomeHint', 'Each (tool, user) keeps its own rw HOME across calls. Needed for tools that cache login tokens (svc, gh, kubectl). Off = ephemeral /tmp each run.')}
        </div>
      </div>

      <div>
        <label style={labelStyle}>{k('fieldHomeQuota', 'HOME quota (MB)')}</label>
        <input
          type="number"
          className="form-input"
          min={0}
          max={100000}
          value={config.runtime.home_quota_mb}
          disabled={!config.runtime.persistent_home}
          onChange={(e) => updateRuntime({ home_quota_mb: Math.max(0, Number(e.target.value) || 0) })}
        />
        <div style={hintStyle}>
          {k('homeQuotaHint', 'Subsequent calls are rejected when exceeded. 0 = unlimited.')}
        </div>
      </div>

      <div>
        <label style={labelStyle}>{k('fieldParametersSchema', 'Parameters schema (JSON Schema)')}</label>
        <textarea
          className="form-input"
          value={paramsSchemaText}
          onChange={(e) => setParamsSchemaText(e.target.value)}
          rows={6}
          style={{ fontFamily: 'monospace', resize: 'vertical' }}
        />
      </div>

      <TestRunPanel tool={tool} />

      {error && (
        <div style={{ color: 'var(--danger, #ff3b30)', fontSize: '12px' }}>{error}</div>
      )}

      <div style={actionsRow}>
        <button className="btn btn-secondary" onClick={onBack}>{t('common.back', 'Back')}</button>
        <button className="btn btn-primary" disabled={saving} onClick={save}>
          {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
        </button>
      </div>
    </>
  );
}
