// Mirrors the CliToolOut / CliToolConfig schema from the backend.
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
  // Values returned by the backend are redacted to "***"; on save the
  // frontend sends plaintext and the backend encrypts.
  env_inject: Record<string, string>;
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

export function defaultCliToolConfig(): CliToolConfig {
  return {
    binary_sha256: null,
    binary_size: null,
    binary_original_name: null,
    binary_uploaded_at: null,
    args_template: [],
    env_inject: {},
    timeout_seconds: 30,
    sandbox: {
      cpu_limit: '1.0',
      memory_limit: '512m',
      network: false,
      readonly_fs: true,
      image: null,
    },
  };
}
