# CLI Tools — M3: Frontend `tools` Tab Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a full CLI-tools management UI to the existing `EnterpriseSettings > tools` tab: filter, list, 3-step wizard (basic info / binary upload / configuration & test), scope tag, status toggle.

**Architecture:** Extract the CLI-tool UI into a focused component tree rooted at `CliToolsSection.tsx` and mount it inside `EnterpriseSettings.tsx` only when the `CLI` filter is active, so that the existing MCP / builtin list code keeps working unchanged. The backend contract is the API from M2 — this milestone does not change any backend code.

**Tech Stack:** React · TypeScript · `fetch` (existing `fetchJson` helper) · CSS classes already present in `frontend/src/index.css`.

**Spec:** `docs/superpowers/specs/2026-04-16-cli-tools-management-design.md` — §6 UI, §11.4 UI signalling.

**Depends on:** M2 merged (plan `2026-04-16-cli-tools-m2-backend-api.md`).

**Frontend baseline note:** the project has no automated frontend test harness. Each task ends with a manual check list the engineer runs against a running local stack.

---

## File structure

| Path | Purpose |
|---|---|
| `frontend/src/components/cli-tools/types.ts` | Shared TS types mirroring M2 API response shapes |
| `frontend/src/components/cli-tools/api.ts` | Thin fetch wrappers for the 7 endpoints |
| `frontend/src/components/cli-tools/CliToolsSection.tsx` | Top-level — list + "Add CLI tool" button |
| `frontend/src/components/cli-tools/CliToolRow.tsx` | One row in the list (name / scope / status / actions) |
| `frontend/src/components/cli-tools/CliToolWizard.tsx` | 3-step wizard orchestrator (create or edit) |
| `frontend/src/components/cli-tools/steps/BasicInfoStep.tsx` | Step 1 |
| `frontend/src/components/cli-tools/steps/BinaryStep.tsx` | Step 2 — upload + SHA display + replace confirm |
| `frontend/src/components/cli-tools/steps/ConfigStep.tsx` | Step 3 — env grid, args, resources, schema, test-run |
| `frontend/src/components/cli-tools/EnvGrid.tsx` | Env key-value grid with mock-override toggle per row |
| `frontend/src/components/cli-tools/TestRunPanel.tsx` | Test-run UI — params JSON input + stdout/stderr output |
| `frontend/src/pages/EnterpriseSettings.tsx` | Mount `CliToolsSection` when filter is CLI |
| `frontend/src/i18n/en.json` · `zh.json` | Labels |

Total: 10 new, 3 modified.

---

## Task 1: Shared types + API wrapper

**Files:**
- Create: `frontend/src/components/cli-tools/types.ts`
- Create: `frontend/src/components/cli-tools/api.ts`

- [ ] **Step 1: Write the types**

```ts
// Mirrors the CliToolOut / CliToolConfig schema from M2 (see spec §5.1).
// Keep in sync with backend/app/services/cli_tools/schema.py — add-only.

export interface SandboxConfig {
  cpu_limit: string;
  memory_limit: string;
  network: boolean;
  readonly_fs: boolean;
  image: string | null;
}

export interface CliToolConfig {
  binary_sha256: string | null;
  binary_size: number | null;
  binary_original_name: string | null;
  binary_uploaded_at: string | null;
  args_template: string[];
  env_inject: Record<string, string>; // values are "***" when read via list/detail
  timeout_seconds: number;
  sandbox: SandboxConfig;
}

export interface CliTool {
  id: string;
  name: string;
  display_name: string;
  description: string;
  type: 'cli';
  tenant_id: string | null;
  is_active: boolean;
  parameters_schema: Record<string, unknown>;
  config: CliToolConfig;
}

export interface TestRunRequest {
  params: Record<string, unknown>;
  mock_env?: Record<string, string>;
}

export interface TestRunResponse {
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_ms: number;
  error_class?: string;
  error_message?: string;
}
```

- [ ] **Step 2: Write the API wrapper**

```ts
import { fetchJson } from '../../utils/fetchJson';
import type { CliTool, TestRunRequest, TestRunResponse } from './types';

export const cliToolsApi = {
  list: () => fetchJson<CliTool[]>('/tools?type=cli'),
  get: (id: string) => fetchJson<CliTool>(`/tools/${id}`),
  create: (body: Partial<CliTool>) => fetchJson<CliTool>('/tools/cli', { method: 'POST', body }),
  update: (id: string, body: Partial<CliTool>) =>
    fetchJson<CliTool>(`/tools/${id}/cli`, { method: 'PATCH', body }),
  delete: (id: string) => fetchJson<void>(`/tools/${id}`, { method: 'DELETE' }),
  testRun: (id: string, req: TestRunRequest) =>
    fetchJson<TestRunResponse>(`/tools/${id}/test-run`, { method: 'POST', body: req }),
  uploadBinary: async (id: string, file: File): Promise<CliTool> => {
    const fd = new FormData();
    fd.append('file', file);
    const token = localStorage.getItem('token') || '';
    const res = await fetch(`${import.meta.env.VITE_API_URL || ''}/api/tools/${id}/binary`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: fd,
    });
    if (!res.ok) {
      throw new Error(`upload failed: ${res.status} ${await res.text()}`);
    }
    return res.json();
  },
};
```

Locate the real `fetchJson` helper first — `grep -n 'fetchJson' frontend/src/utils/*.ts frontend/src/**/*.ts | head -3` — and adapt the import and the upload token read to match project conventions.

- [ ] **Step 3: Verify build**

```
cd frontend && npm run build 2>&1 | tail -20
```

Expected: no TypeScript errors.

- [ ] **Step 4: Commit**

```
git add frontend/src/components/cli-tools/types.ts frontend/src/components/cli-tools/api.ts
git commit -m "feat(cli-tools): frontend types + API wrapper"
```

---

## Task 2: i18n labels

**Files:**
- Modify: `frontend/src/i18n/en.json`
- Modify: `frontend/src/i18n/zh.json`

- [ ] **Step 1: Add the labels**

Find the existing `enterprise` or `tools` namespace in both files. Append a `cliTools` subtree:

```json
"cliTools": {
  "filterLabel": "CLI",
  "addButton": "Add CLI Tool",
  "columnScope": "Scope",
  "columnStatus": "Status",
  "columnUpdated": "Updated",
  "scopeGlobal": "Global (all tenants)",
  "scopeTenant": "Tenant",
  "statusActive": "Active",
  "statusDisabled": "Disabled",
  "wizard": {
    "stepBasic": "Basic info",
    "stepBinary": "Binary",
    "stepConfig": "Configuration & test",
    "replaceWarning": "Replacing the binary will affect every agent that uses this tool. The new version takes effect on the next execute."
  },
  "env": {
    "key": "Name",
    "value": "Value",
    "useMockForTest": "Use mock value for Test Run"
  },
  "sandbox": {
    "pinImage": "Pin sandbox image",
    "followStable": "Follow platform default (stable)",
    "networkHint": "Enable only if the tool needs to call external APIs or download resources."
  },
  "disableConfirm": "Disable {{name}}? {{count}} agents are currently using it. They will see an error on next invocation until it is re-enabled."
}
```

Chinese version uses the same keys with translated values. Use the project's existing tone — check other enterprise-tab strings.

- [ ] **Step 2: Commit**

```
git add frontend/src/i18n/en.json frontend/src/i18n/zh.json
git commit -m "i18n(cli-tools): labels for the CLI-tools section"
```

---

## Task 3: `CliToolsSection` skeleton with filter + add button

**Files:**
- Create: `frontend/src/components/cli-tools/CliToolsSection.tsx`

- [ ] **Step 1: Write the component**

```tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from './api';
import type { CliTool } from './types';
import { CliToolRow } from './CliToolRow';
import { CliToolWizard } from './CliToolWizard';

export function CliToolsSection() {
  const { t } = useTranslation();
  const [tools, setTools] = useState<CliTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<CliTool | null>(null);
  const [creating, setCreating] = useState(false);

  const reload = async () => {
    setLoading(true);
    try {
      setTools(await cliToolsApi.list());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
  }, []);

  return (
    <div className="cli-tools-section">
      <div className="toolbar" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <h3>{t('enterprise.cliTools.filterLabel', 'CLI')}</h3>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          {t('enterprise.cliTools.addButton', 'Add CLI Tool')}
        </button>
      </div>

      {loading && <div>Loading…</div>}
      {!loading && tools.length === 0 && <div className="empty">No CLI tools yet.</div>}

      {!loading && tools.length > 0 && (
        <table className="tool-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>{t('enterprise.cliTools.columnScope')}</th>
              <th>{t('enterprise.cliTools.columnStatus')}</th>
              <th>{t('enterprise.cliTools.columnUpdated')}</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <CliToolRow
                key={tool.id}
                tool={tool}
                onEdit={() => setEditing(tool)}
                onChange={reload}
              />
            ))}
          </tbody>
        </table>
      )}

      {(creating || editing) && (
        <CliToolWizard
          tool={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
            reload();
          }}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify build**

```
cd frontend && npm run build 2>&1 | tail -10
```

Expected: TypeScript happy (missing imports for `CliToolRow` / `CliToolWizard` will error — placeholder files will be created in the next tasks).

As a placeholder to keep the build green, add `.tsx` stubs now with:

```tsx
// CliToolRow.tsx
import type { CliTool } from './types';
export function CliToolRow({ tool }: { tool: CliTool; onEdit: () => void; onChange: () => void }) {
  return <tr><td>{tool.name}</td><td /><td /><td /><td /></tr>;
}
```

```tsx
// CliToolWizard.tsx
import type { CliTool } from './types';
export function CliToolWizard({ onClose }: { tool: CliTool | null; onClose: () => void }) {
  return <div>wizard placeholder <button onClick={onClose}>close</button></div>;
}
```

The stubs are replaced in Tasks 4 and 5.

- [ ] **Step 3: Commit**

```
git add frontend/src/components/cli-tools/CliToolsSection.tsx frontend/src/components/cli-tools/CliToolRow.tsx frontend/src/components/cli-tools/CliToolWizard.tsx
git commit -m "feat(cli-tools): section skeleton + row/wizard stubs"
```

---

## Task 4: `CliToolRow` — full row with scope tag + disable/enable + delete

**Files:**
- Modify: `frontend/src/components/cli-tools/CliToolRow.tsx`

- [ ] **Step 1: Replace the stub with the real row**

```tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cliToolsApi } from './api';
import type { CliTool } from './types';

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString();
}

export function CliToolRow({
  tool,
  onEdit,
  onChange,
}: {
  tool: CliTool;
  onEdit: () => void;
  onChange: () => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  const isGlobal = tool.tenant_id === null;
  const scopeLabel = isGlobal
    ? t('enterprise.cliTools.scopeGlobal')
    : t('enterprise.cliTools.scopeTenant');

  const toggleActive = async () => {
    setBusy(true);
    try {
      await cliToolsApi.update(tool.id, { is_active: !tool.is_active });
      onChange();
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirm(`Delete ${tool.display_name}?`)) return;
    setBusy(true);
    try {
      await cliToolsApi.delete(tool.id);
      onChange();
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr>
      <td>
        <strong>{tool.display_name}</strong>
        <div className="muted">{tool.name}</div>
      </td>
      <td>
        <span className={`scope-tag ${isGlobal ? 'global' : 'tenant'}`}>{scopeLabel}</span>
      </td>
      <td>
        <span className={`status-dot ${tool.is_active ? 'active' : 'disabled'}`} />{' '}
        {tool.is_active
          ? t('enterprise.cliTools.statusActive')
          : t('enterprise.cliTools.statusDisabled')}
      </td>
      <td>{formatDate(tool.config?.binary_uploaded_at ?? null)}</td>
      <td className="row-actions">
        <button disabled={busy} onClick={onEdit}>Edit</button>
        <button disabled={busy} onClick={toggleActive}>
          {tool.is_active ? 'Disable' : 'Enable'}
        </button>
        <button disabled={busy} onClick={remove} className="danger">Delete</button>
      </td>
    </tr>
  );
}
```

- [ ] **Step 2: Manual verify**

Boot local stack (M1 + M2 merged already). Open the EnterpriseSettings → tools tab; if M4's list API returns empty, use curl to create one tool for display:

```
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/tools/cli \
  -d '{"name":"smoke","display_name":"Smoke","config":{}}'
```

Reload the page. Row should show scope = Tenant, status = Active, actions responsive.

- [ ] **Step 3: Commit**

```
git add frontend/src/components/cli-tools/CliToolRow.tsx
git commit -m "feat(cli-tools): full list row with disable/enable/delete"
```

---

## Task 5: `CliToolWizard` — 3-step orchestrator

**Files:**
- Modify: `frontend/src/components/cli-tools/CliToolWizard.tsx`
- Create: `frontend/src/components/cli-tools/steps/BasicInfoStep.tsx`
- Create: `frontend/src/components/cli-tools/steps/BinaryStep.tsx`
- Create: `frontend/src/components/cli-tools/steps/ConfigStep.tsx`

- [ ] **Step 1: Orchestrator**

```tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CliTool } from './types';
import { cliToolsApi } from './api';
import { BasicInfoStep } from './steps/BasicInfoStep';
import { BinaryStep } from './steps/BinaryStep';
import { ConfigStep } from './steps/ConfigStep';

type Step = 1 | 2 | 3;

export function CliToolWizard({ tool, onClose }: { tool: CliTool | null; onClose: () => void }) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>(1);
  const [draft, setDraft] = useState<CliTool | null>(tool);

  // Create flow: persist metadata at end of step 1, then step 2 operates on a real tool_id.
  const ensurePersisted = async (partial: Partial<CliTool>) => {
    if (draft?.id) {
      const updated = await cliToolsApi.update(draft.id, partial);
      setDraft(updated);
      return updated;
    }
    const created = await cliToolsApi.create(partial);
    setDraft(created);
    return created;
  };

  return (
    <div className="modal">
      <div className="modal-content wide">
        <div className="wizard-tabs">
          {(['stepBasic', 'stepBinary', 'stepConfig'] as const).map((key, idx) => {
            const n = (idx + 1) as Step;
            return (
              <div key={key} className={`wizard-tab ${step === n ? 'active' : ''}`}>
                {n}. {t(`enterprise.cliTools.wizard.${key}`)}
              </div>
            );
          })}
        </div>

        {step === 1 && (
          <BasicInfoStep
            tool={draft}
            onNext={async (values) => {
              await ensurePersisted(values);
              setStep(2);
            }}
            onCancel={onClose}
          />
        )}
        {step === 2 && draft && (
          <BinaryStep
            tool={draft}
            onReplaced={(updated) => setDraft(updated)}
            onBack={() => setStep(1)}
            onNext={() => setStep(3)}
          />
        )}
        {step === 3 && draft && (
          <ConfigStep
            tool={draft}
            onUpdated={(updated) => setDraft(updated)}
            onBack={() => setStep(2)}
            onDone={onClose}
          />
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Basic info step**

```tsx
import { useState } from 'react';
import type { CliTool } from '../types';

export function BasicInfoStep({
  tool,
  onNext,
  onCancel,
}: {
  tool: CliTool | null;
  onNext: (values: Partial<CliTool>) => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(tool?.name ?? '');
  const [displayName, setDisplayName] = useState(tool?.display_name ?? '');
  const [description, setDescription] = useState(tool?.description ?? '');
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      await onNext({ name, display_name: displayName, description });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="wizard-body">
      <label>Name <input value={name} onChange={(e) => setName(e.target.value)} disabled={!!tool?.id} /></label>
      <label>Display name <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></label>
      <label>Description <textarea value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      <div className="wizard-actions">
        <button onClick={onCancel}>Cancel</button>
        <button
          className="btn-primary"
          disabled={submitting || !name.trim() || !displayName.trim()}
          onClick={submit}
        >
          Next
        </button>
      </div>
    </div>
  );
}
```

`disabled={!!tool?.id}` on `name` enforces that renaming the machine-readable key isn't allowed post-create.

- [ ] **Step 3: Binary step**

```tsx
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
    if (sha && !confirm(t('enterprise.cliTools.wizard.replaceWarning'))) return;
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
    <div className="wizard-body">
      {sha ? (
        <div className="binary-current">
          <strong>Current binary</strong>
          <div>Name: {tool.config.binary_original_name}</div>
          <div>Size: {tool.config.binary_size} bytes</div>
          <div>SHA-256: <code>{sha}</code></div>
        </div>
      ) : (
        <div className="binary-empty">No binary uploaded yet.</div>
      )}

      <label className="file-picker">
        {sha ? 'Replace binary' : 'Upload binary'}
        <input
          type="file"
          disabled={uploading}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload(f);
          }}
        />
      </label>
      {uploading && <div>Uploading…</div>}
      {error && <div className="error">{error}</div>}

      <div className="wizard-actions">
        <button onClick={onBack}>Back</button>
        <button className="btn-primary" disabled={!sha} onClick={onNext}>Next</button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Config step (leaves env grid and test-run to their own components)**

```tsx
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
  const [paramsSchemaText, setParamsSchemaText] = useState(
    JSON.stringify(tool.parameters_schema, null, 2),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      const parsedSchema = JSON.parse(paramsSchemaText);
      const updated = await cliToolsApi.update(tool.id, {
        parameters_schema: parsedSchema,
        config,
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
    <div className="wizard-body">
      <label>
        Args template (JSON array)
        <textarea
          value={JSON.stringify(config.args_template)}
          onChange={(e) => {
            try {
              setConfig({ ...config, args_template: JSON.parse(e.target.value) });
            } catch {/* ignore parse mid-typing */}
          }}
        />
      </label>

      <EnvGrid
        env={config.env_inject}
        onChange={(env) => setConfig({ ...config, env_inject: env })}
      />

      <label>Timeout (s) <input type="number" value={config.timeout_seconds}
        onChange={(e) => setConfig({ ...config, timeout_seconds: Number(e.target.value) })} /></label>

      <label>CPU limit <input value={config.sandbox.cpu_limit}
        onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, cpu_limit: e.target.value } })} /></label>
      <label>Memory limit <input value={config.sandbox.memory_limit}
        onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, memory_limit: e.target.value } })} /></label>

      <label>
        <input type="checkbox" checked={config.sandbox.network}
          onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, network: e.target.checked } })} />
        Allow network
      </label>
      <div className="hint">{t('enterprise.cliTools.sandbox.networkHint')}</div>

      <label>
        Sandbox image
        <input value={config.sandbox.image ?? ''}
          placeholder={t('enterprise.cliTools.sandbox.followStable')}
          onChange={(e) => setConfig({ ...config, sandbox: { ...config.sandbox, image: e.target.value.trim() || null } })} />
      </label>

      <label>
        Parameters schema (JSON)
        <textarea value={paramsSchemaText} onChange={(e) => setParamsSchemaText(e.target.value)} rows={10} />
      </label>

      <TestRunPanel tool={tool} />

      {error && <div className="error">{error}</div>}
      <div className="wizard-actions">
        <button onClick={onBack}>Back</button>
        <button className="btn-primary" disabled={saving} onClick={save}>Save</button>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Build + manual click-through**

```
cd frontend && npm run build 2>&1 | tail -10
```

Start the stack, open the tools tab in the browser, click "Add CLI Tool", walk through the wizard. The first pass will fail on `EnvGrid` / `TestRunPanel` imports — those are placeholder stubs built in Tasks 6 and 7.

- [ ] **Step 6: Commit**

```
git add frontend/src/components/cli-tools/CliToolWizard.tsx frontend/src/components/cli-tools/steps/
git commit -m "feat(cli-tools): 3-step wizard (basic info / binary / config)"
```

---

## Task 6: `EnvGrid` — key-value editor with mock-override toggle

**Files:**
- Create: `frontend/src/components/cli-tools/EnvGrid.tsx`

- [ ] **Step 1: Write the component**

```tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

export function EnvGrid({
  env,
  onChange,
}: {
  env: Record<string, string>;
  onChange: (env: Record<string, string>) => void;
}) {
  const { t } = useTranslation();
  const [newKey, setNewKey] = useState('');
  const [newValue, setNewValue] = useState('');

  const setPair = (key: string, value: string) => onChange({ ...env, [key]: value });
  const remove = (key: string) => {
    const next = { ...env };
    delete next[key];
    onChange(next);
  };

  return (
    <div className="env-grid">
      <table>
        <thead>
          <tr>
            <th>{t('enterprise.cliTools.env.key')}</th>
            <th>{t('enterprise.cliTools.env.value')}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {Object.entries(env).map(([key, value]) => (
            <tr key={key}>
              <td><code>{key}</code></td>
              <td>
                <input value={value} onChange={(e) => setPair(key, e.target.value)} />
                {value === '***' && <span className="muted"> (stored, type to replace)</span>}
              </td>
              <td><button onClick={() => remove(key)}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="env-add">
        <input placeholder="KEY" value={newKey} onChange={(e) => setNewKey(e.target.value.toUpperCase())} />
        <input placeholder="value" value={newValue} onChange={(e) => setNewValue(e.target.value)} />
        <button
          disabled={!newKey || newKey in env}
          onClick={() => {
            setPair(newKey, newValue);
            setNewKey('');
            setNewValue('');
          }}
        >
          Add
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```
git add frontend/src/components/cli-tools/EnvGrid.tsx
git commit -m "feat(cli-tools): env grid editor"
```

---

## Task 7: `TestRunPanel` — params JSON + stdout/stderr

**Files:**
- Create: `frontend/src/components/cli-tools/TestRunPanel.tsx`

- [ ] **Step 1: Write the component**

```tsx
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
    <div className="test-run-panel">
      <h4>Test Run</h4>
      <label>
        Params (JSON)
        <textarea value={paramsText} onChange={(e) => setParamsText(e.target.value)} rows={4} />
      </label>
      <label>
        Mock env (JSON, leave <code>{'{}'}</code> to use stored values)
        <textarea value={mockEnvText} onChange={(e) => setMockEnvText(e.target.value)} rows={3} />
      </label>
      <button disabled={running || !tool.config.binary_sha256} onClick={run}>Run</button>

      {err && <div className="error">{err}</div>}
      {result && (
        <div className="test-run-result">
          <div>exit_code: <code>{result.exit_code}</code> · duration: {result.duration_ms} ms</div>
          {result.error_class && (
            <div className="error">[{result.error_class}] {result.error_message}</div>
          )}
          {result.stdout && (
            <details open>
              <summary>stdout</summary>
              <pre>{result.stdout}</pre>
            </details>
          )}
          {result.stderr && (
            <details>
              <summary>stderr</summary>
              <pre>{result.stderr}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```
git add frontend/src/components/cli-tools/TestRunPanel.tsx
git commit -m "feat(cli-tools): test-run panel inside the wizard"
```

---

## Task 8: Mount `CliToolsSection` in `EnterpriseSettings`

**Files:**
- Modify: `frontend/src/pages/EnterpriseSettings.tsx`

- [ ] **Step 1: Locate the existing tools tab filter (around the `activeTab === 'tools'` block)**

```
grep -n "activeTab === 'tools'" frontend/src/pages/EnterpriseSettings.tsx
```

- [ ] **Step 2: Add a `toolTypeFilter` state and render `CliToolsSection` when `cli` is selected**

At the top of the component:

```tsx
import { CliToolsSection } from '../components/cli-tools/CliToolsSection';
// ...
const [toolTypeFilter, setToolTypeFilter] = useState<'mcp' | 'builtin' | 'cli' | 'all'>('all');
```

Inside the `activeTab === 'tools'` block, add the filter bar:

```tsx
<div className="tool-type-filter" style={{ margin: '0 0 12px' }}>
  {(['all', 'mcp', 'builtin', 'cli'] as const).map((k) => (
    <button key={k}
      className={toolTypeFilter === k ? 'active' : ''}
      onClick={() => setToolTypeFilter(k)}
    >
      {k.toUpperCase()}
    </button>
  ))}
</div>
{toolTypeFilter === 'cli' ? (
  <CliToolsSection />
) : (
  /* existing MCP/built-in list JSX remains here */
  existingToolsJsx
)}
```

If the existing JSX is inline (not extracted), leave it where it is and wrap the whole existing block in the `else` branch. Do not restructure the existing list in this milestone.

- [ ] **Step 3: Manual verification**

Boot stack, log in as org_admin, open the settings tools tab. Click CLI filter: the CliToolsSection renders. Click MCP / built-in / all: the existing list still renders unchanged.

- [ ] **Step 4: Commit**

```
git add frontend/src/pages/EnterpriseSettings.tsx
git commit -m "feat(cli-tools): wire CLI section into EnterpriseSettings tools tab"
```

---

## M3 Exit Criteria

- [ ] `frontend` builds cleanly (`npm run build`)
- [ ] Local org_admin can: create a tool, upload a shebang script, configure env + schema, test-run, see non-zero exit_code surfaced in the panel
- [ ] Switching the tools-tab filter between CLI / MCP / built-in toggles cleanly without losing the other sections
- [ ] Disable toggles the `is_active` field via PATCH; the row status reflects it after reload

## Handoff to M4

M4 (test-run UI polish + GC + M0 migration) extends the panel wiring here and adds the GC cron on the backend. No frontend changes required beyond what M3 ships.
