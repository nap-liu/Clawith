import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from '../api';
import type { CliTool, CliToolConfig } from '../types';
import { defaultCliToolConfig } from '../types';
import { EnvGrid } from '../EnvGrid';
import { TestRunPanel } from '../TestRunPanel';

function mergeWithDefaults(partial: Partial<CliToolConfig> | null | undefined): CliToolConfig {
  const d = defaultCliToolConfig();
  const c = partial ?? {};
  const mergedSandbox = { ...d.sandbox, ...(c.sandbox ?? {}) };
  // Array fields inside nested sandbox need explicit default fallback —
  // a stored config from before the egress_allowlist field existed will
  // spread `undefined` and blow up the textarea join() below.
  if (!Array.isArray(mergedSandbox.egress_allowlist)) {
    mergedSandbox.egress_allowlist = [];
  }
  return {
    ...d,
    ...c,
    args_template: c.args_template ?? d.args_template,
    env_inject: c.env_inject ?? d.env_inject,
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
  const [argsText, setArgsText] = useState(() => JSON.stringify(mergeWithDefaults(tool.config).args_template));
  const [paramsSchemaText, setParamsSchemaText] = useState(
    JSON.stringify(tool.parameters_schema || {}, null, 2),
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

      const updated = await cliToolsApi.update(tool.id, {
        parameters_schema: parsedSchema,
        config: { ...config, args_template: parsedArgs },
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
        <EnvGrid env={config.env_inject} onChange={(env) => setConfig({ ...config, env_inject: env })} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '8px' }}>
        <div>
          <label style={labelStyle}>{k('fieldTimeout', 'Timeout (s)')}</label>
          <input
            type="number"
            className="form-input"
            value={config.timeout_seconds}
            onChange={(e) => setConfig({ ...config, timeout_seconds: Number(e.target.value) || 30 })}
          />
        </div>
        <div>
          <label style={labelStyle}>{k('fieldCpu', 'CPU')}</label>
          <input
            className="form-input"
            value={config.sandbox.cpu_limit}
            onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, cpu_limit: e.target.value } })}
          />
        </div>
        <div>
          <label style={labelStyle}>{k('fieldMemory', 'Memory')}</label>
          <input
            className="form-input"
            value={config.sandbox.memory_limit}
            onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, memory_limit: e.target.value } })}
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
          value={config.rate_limit_per_minute}
          onChange={(e) => setConfig({ ...config, rate_limit_per_minute: Math.max(0, Number(e.target.value) || 0) })}
        />
        <div style={hintStyle}>
          {k('rateLimitHint', '0 = unlimited. Protects against runaway agent loops.')}
        </div>
      </div>

      <div>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '13px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={config.sandbox.network}
            onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, network: e.target.checked } })}
          />
          {k('fieldAllowNetwork', 'Allow network')}
        </label>
        <div style={hintStyle}>
          {t('enterprise.cliTools.sandbox.networkHint', 'Enable only if the tool needs external APIs / downloads.')}
        </div>
      </div>

      {config.sandbox.network && (
        <div>
          <label style={labelStyle}>{k('fieldEgressAllowlist', 'Egress allowlist')}</label>
          <textarea
            className="form-input"
            value={(config.sandbox.egress_allowlist ?? []).join('\n')}
            onChange={(e) => setConfig({
              ...config,
              sandbox: {
                ...config.sandbox,
                // Split on any run of newline/whitespace, trim, drop empty —
                // operator-friendly: pasted comma- or space-separated lists
                // also work, and trailing blank lines never produce a "".
                egress_allowlist: e.target.value
                  .split(/[\s,]+/)
                  .map((h) => h.trim())
                  .filter((h) => h.length > 0),
              },
            })}
            rows={3}
            placeholder="api.yeyecha.com&#10;registry.example.com"
            style={{ fontFamily: 'monospace', resize: 'vertical' }}
          />
          <div style={hintStyle}>
            {k('egressAllowlistHint', 'One hostname per line. Empty = allow all (default). Current release: only forwarded to the tool via CLAWITH_EGRESS_ALLOWLIST env var — actual enforcement is a phase-2 infra PR.')}
          </div>
        </div>
      )}

      <div>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '13px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={config.persistent_home}
            onChange={(e) => setConfig({ ...config, persistent_home: e.target.checked })}
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
          value={config.home_quota_mb}
          disabled={!config.persistent_home}
          onChange={(e) => setConfig({ ...config, home_quota_mb: Math.max(0, Number(e.target.value) || 0) })}
        />
        <div style={hintStyle}>
          {k('homeQuotaHint', 'Subsequent calls are rejected when exceeded. 0 = unlimited.')}
        </div>
      </div>

      <div>
        <label style={labelStyle}>{k('fieldSandboxImage', 'Sandbox image')}</label>
        <input
          className="form-input"
          value={config.sandbox.image ?? ''}
          placeholder={k('sandboxImagePlaceholder', '(blank = follow platform default stable)')}
          onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, image: e.target.value.trim() || null } })}
        />
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
