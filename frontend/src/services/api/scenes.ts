import { request } from "./core";
import type { MCPServerEditorDraftOverride } from '../../types/mcpServer';

export type SceneSystemPrompt = {
  id: string;
  name: string;
  content: string;
  enabled: boolean;
};

export type SceneQuickActionStyle = {
  bold: boolean;
  italic: boolean;
  color?: string | null;
  font: "default" | "sans" | "serif" | "monospace";
};

export type SceneQuickAction = {
  id: string;
  label: string;
  type: "open_uri" | "send_message";
  menu_visible: boolean;
  ai_visible: boolean;
  ai_context: string;
  enabled?: boolean;
  uri?: string | null;
  message?: string | null;
  style?: SceneQuickActionStyle | null;
};

export type Scene = {
  include_soul?: boolean;
  include_memory?: boolean;
  tools?: Array<{ tool_id: string; enabled: boolean; config: Record<string, any> }> | null;
  mcp_server_overrides?: Array<MCPServerEditorDraftOverride & { server_id: string }>;
  auto_activation?: SceneAutoActivation;
  id?: string;
  scene_key: string;
  name: string;
  enabled: boolean;
  revision: number;
  has_unpublished_changes: boolean;
  welcome_message: string;
  system_prompts: SceneSystemPrompt[];
  quick_actions: SceneQuickAction[];
  updated_at?: string | null;
  revisions?: Array<{
    revision: number;
    created_at: string | null;
    created_by_user_id?: string | null;
    created_by_agent_id?: string | null;
  }>;
};

type SceneManifestQuickActionBase = Pick<
  SceneQuickAction,
  "id" | "label" | "menu_visible" | "enabled" | "style"
>;

export type SceneManifestQuickAction = SceneManifestQuickActionBase &
  (
    | { type: "send_message"; message: string; uri?: never }
    | { type: "open_uri"; uri: string; message?: never }
  );

export type SceneManifest = Omit<Scene, "system_prompts" | "quick_actions" | "auto_activation" | "include_memory" | "include_soul" | "tools" | "mcp_server_overrides"> & {
  system_prompts: [];
  quick_actions: SceneManifestQuickAction[];
};

export type SceneConversationTarget = {
  target_ref: string;
  label: string;
  source_channel: string;
  is_group: boolean;
};

export type SceneAutoActivation = {
  enabled: boolean;
  targets: SceneConversationTarget[];
};

export const sceneApi = {
  conversationOptions: (agentId: string, q = '', offset = 0) =>
    request<{ items: SceneConversationTarget[]; next_offset: number | null }>(
      `/agents/${agentId}/scenes/conversation-options?${new URLSearchParams({ q, offset: String(offset) })}`,
    ),
  list: (agentId: string) => request<Scene[]>(`/agents/${agentId}/scenes`),
  get: (agentId: string, sceneKey: string) =>
    request<Scene>(`/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}`),
  revision: (agentId: string, sceneKey: string, revision: number) =>
    request<Scene>(
      `/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}/revisions/${revision}`,
    ),
  manifest: (agentId: string, sceneKey: string) =>
    request<SceneManifest>(
      `/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}/manifest`,
    ),
  save: (
    agentId: string,
    sceneKey: string,
    data: Omit<
      Scene,
      | "id"
      | "scene_key"
      | "revision"
      | "has_unpublished_changes"
      | "updated_at"
      | "revisions"
    > & { expected_revision: number },
  ) =>
    request<Scene>(
      `/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      },
    ),
  publish: (agentId: string, sceneKey: string, expectedRevision: number) =>
    request<Scene>(
      `/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}/publish`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
    ),
  delete: (agentId: string, sceneKey: string) =>
    request<{ ok: boolean }>(
      `/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}`,
      { method: "DELETE" },
    ),
  rollback: (
    agentId: string,
    sceneKey: string,
    targetRevision: number,
    expectedRevision: number,
  ) =>
    request<Scene>(
      `/agents/${agentId}/scenes/${encodeURIComponent(sceneKey)}/rollback`,
      {
        method: "POST",
        body: JSON.stringify({
          target_revision: targetRevision,
          expected_revision: expectedRevision,
        }),
      },
    ),
};

export const speechApi = {
  createTicket: () =>
    request<{ ticket: string; expires_in: number }>("/speech/ticket", {
      method: "POST",
    }),
};
