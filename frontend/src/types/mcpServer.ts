export interface MCPServer {
  id: string;
  tenant_id: string | null;
  name: string;
  display_name: string;
  base_url_template: string;
  headers_template: Record<string, string>;
  credential_state: 'set' | 'unset';
  system_prompt_block: string | null;
  instructions: string | null;
  instructions_captured_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface MCPServerCreatePayload {
  name: string;
  display_name: string;
  base_url_template: string;
  headers_template?: Record<string, string>;
  credential_template?: string | null;
  system_prompt_block?: string | null;
  tenant_id?: string | null;
}

export interface MCPServerUpdatePayload {
  display_name?: string;
  base_url_template?: string;
  headers_template?: Record<string, string>;
  credential_template?: string | null;
  system_prompt_block?: string | null;
}

export interface TestConnectionResult {
  success: boolean;
  instructions: string | null;
  server_info: Record<string, unknown> | null;
  error: string | null;
}
