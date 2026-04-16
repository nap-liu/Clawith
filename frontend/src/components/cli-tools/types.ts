// Mirrors the CliToolOut / CliToolConfig schema from the backend.
// Keep in sync with backend/app/services/cli_tools/schema.py — add-only.

export interface SandboxConfig {
  cpu_limit: string;
  memory_limit: string;
  network: boolean;
  readonly_fs: boolean;
  image: string | null;
  // Hostnames the sandbox is permitted to reach when network=true.
  // Empty = allow all (existing behavior). Non-empty = pass-through to
  // the sandbox env as CLAWITH_EGRESS_ALLOWLIST. Not kernel-enforced
  // yet — see docs/superpowers/TODO-egress-enforcement.md.
  egress_allowlist: string[];
}

export interface CliToolConfig {
  binary_sha256: string | null;
  binary_size: number | null;
  binary_original_name: string | null;
  binary_uploaded_at: string | null;
  args_template: string[];
  // Plaintext — values are literal text the operator typed or a single
  // `$user.phone`-style placeholder that the executor resolves at
  // runtime. No masking.
  env_inject: Record<string, string>;
  timeout_seconds: number;
  // When true, each (tool, user) pair keeps its own rw HOME across
  // invocations — required for tools that cache login tokens (svc, gh,
  // kubectl). Default false: stateless tools get an ephemeral /tmp HOME.
  persistent_home: boolean;
  // 0 = unlimited. Protects downstream services (reports, paid APIs)
  // from an LLM-driven runaway loop that hammers the same tool.
  rate_limit_per_minute: number;
  // Soft disk quota for the persistent HOME. Next execute is rejected
  // when usage exceeds this; admin must clear the cache. 0 = unlimited.
  home_quota_mb: number;
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

export function defaultCliToolConfig(): CliToolConfig {
  return {
    binary_sha256: null,
    binary_size: null,
    binary_original_name: null,
    binary_uploaded_at: null,
    args_template: [],
    env_inject: {},
    timeout_seconds: 30,
    persistent_home: false,
    rate_limit_per_minute: 60,
    sandbox: {
      cpu_limit: '1.0',
      memory_limit: '512m',
      network: false,
      readonly_fs: true,
      image: null,
      egress_allowlist: [],
    },
  };
}
