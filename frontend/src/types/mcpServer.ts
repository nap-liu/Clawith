export interface MCPServer {
  id: string;
  tenant_id: string | null;
  name: string;
  display_name: string;
  transport?: 'http' | 'stdio';
  base_url_template: string;
  headers_template: Record<string, string>;
  credential_state: 'set' | 'unset';
  system_prompt_block: string | null;
  instructions: string | null;
  instructions_captured_at: string | null;
  command_template?: string | null;
  args_template?: string[] | null;
  env_template?: Record<string, string> | null;
  created_at: string;
  updated_at: string;
}

export interface MCPServerCreatePayload {
  name: string;
  display_name: string;
  transport?: 'http' | 'stdio';
  base_url_template?: string;
  headers_template?: Record<string, string>;
  credential_template?: string | null;
  system_prompt_block?: string | null;
  tenant_id?: string | null;
  command_template?: string | null;
  args_template?: string[] | null;
  env_template?: Record<string, string> | null;
}

export interface MCPServerUpdatePayload {
  display_name?: string;
  transport?: 'http' | 'stdio';
  base_url_template?: string;
  headers_template?: Record<string, string>;
  credential_template?: string | null;
  system_prompt_block?: string | null;
  command_template?: string | null;
  args_template?: string[] | null;
  env_template?: Record<string, string> | null;
}

export interface TestConnectionResult {
  success: boolean;
  instructions: string | null;
  server_info: Record<string, unknown> | null;
  error: string | null;
}

export interface MCPServerOverride {
  id: string;
  mcp_server_id: string;
  scope_type: 'tenant' | 'agent';
  scope_id: string;
  system_prompt_block: string | null;
  url_template: string | null;
  headers_template: Record<string, string> | null;
  credential_state: 'set' | 'unset';
  last_modified_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface OverridesGrouped {
  tenant: MCPServerOverride[];
  agent: MCPServerOverride[];
}

export interface MCPServerOverridePutPayload {
  system_prompt_block?: string | null;
  url_template?: string | null;
  headers_template?: Record<string, string> | null;
  credential_template?: string | null;
  command_template?: string | null;
  args_template?: string[] | null;
  env_template?: Record<string, string> | null;
}

export interface DraftOverrides {
  base_url_template?: string | null;
  headers_template?: Record<string, string> | null;
  credential_template?: string | null;
  system_prompt_block?: string | null;
}

export interface DryRunRequest {
  identity: 'current_user' | 'synthetic';
  scope: 'platform' | 'tenant' | 'agent';
  tenant_id?: string | null;
  agent_id?: string | null;
  draft_overrides?: DraftOverrides | null;
}

export interface DryRunResponse {
  resolved_url: string;
  resolved_headers: Record<string, string>;
  resolved_credential_state: 'set' | 'unset';
  resolved_prompt: string;
  used_layers: ('platform' | 'tenant' | 'agent' | 'draft')[];
  errors: string[];
}
