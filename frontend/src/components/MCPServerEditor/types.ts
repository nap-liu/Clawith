export type EditorMode = 'server-admin' | 'agent';
export type EditorTab = 'basic' | 'advanced' | 'override' | 'test';
export type EditorRole = 'platform_admin' | 'org_admin' | 'agent_admin' | 'member';

export interface MCPServerEditorProps {
  serverId: string;
  mode: EditorMode;
  defaultTab?: EditorTab;
  agentId?: string;
  role: EditorRole;
  titleSuffix?: string;
  onClose: () => void;
  onSaved?: () => void;
}

/** Which tabs to render given role + mode.
 *
 * Two-layer model enforcement: when the editor is opened from an agent
 * context (mode='agent'), we never surface the server-default tabs (basic /
 * advanced) — those edit the company-wide value and would let an admin
 * accidentally mutate the platform default while only intending to tweak
 * one agent's override. The agent flow stays focused on Override.
 */
export function visibleTabs(role: EditorRole, mode: EditorMode): EditorTab[] {
  const tabs: EditorTab[] = [];
  const isAdmin = role === 'platform_admin' || role === 'org_admin' || role === 'agent_admin';

  if (mode === 'agent') {
    // Agent flow: only the override surface (+ test, so admins can probe the
    // resulting connection without leaving the dialog).
    tabs.push('override');
    if (isAdmin) tabs.push('test');
    return tabs;
  }

  // server-admin flow: edit the company default. No 'override' here.
  if (role !== 'member') tabs.push('basic');
  if (isAdmin) tabs.push('advanced');
  if (isAdmin) tabs.push('test');
  return tabs;
}
