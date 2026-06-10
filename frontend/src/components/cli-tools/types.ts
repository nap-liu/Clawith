// Mirrors the CliToolOut / CliToolConfig schema from the backend.
// Keep in sync with backend/app/services/cli_tools/schema.py — add-only.
//
// Two-layer model:
//   - binary: system-written, updated only by POST /tools/cli/{id}/binary.
//   - env: admin-editable env injection map.
//
// The backend refuses any PATCH body carrying a top-level `binary` key,
// so the frontend must never put one in update payloads either.

export interface BinaryMetadata {
  sha256: string | null;
  size: number | null;
  original_name: string | null;
  uploaded_at: string | null;
}

export interface CliToolConfig {
  binary: BinaryMetadata;
  env: Record<string, string>;
}

export interface CliTool {
  id: string;
  name: string;
  display_name: string;
  description: string;
  type: 'cli';
  tenant_id: string | null;
  is_active: boolean;
  config: CliToolConfig;
}

export interface BinaryVersion {
  id: string;
  tool_id: string;
  sha256: string;
  size: number;
  original_name: string;
  uploaded_at: string;
  uploaded_by_user_id: string | null;
  is_current: boolean;
  notes: string | null;
}

export interface TestRunRequest {
  command: string;
}

export interface TestRunResponse {
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_ms: number;
  error_message?: string;
}

export function defaultBinaryMetadata(): BinaryMetadata {
  return {
    sha256: null,
    size: null,
    original_name: null,
    uploaded_at: null,
  };
}

export function defaultCliToolConfig(): CliToolConfig {
  return {
    binary: defaultBinaryMetadata(),
    env: {},
  };
}
