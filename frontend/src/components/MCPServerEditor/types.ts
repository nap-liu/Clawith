export type EditorMode = 'server-admin' | 'agent';
export type EditorTab = 'basic' | 'advanced' | 'override' | 'tenant' | 'test';
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

/** Which tabs to render given role + mode. */
export function visibleTabs(role: EditorRole, mode: EditorMode): EditorTab[] {
  const tabs: EditorTab[] = [];
  if (role !== 'member') tabs.push('basic');
  if (role === 'platform_admin' || role === 'org_admin' || role === 'agent_admin') tabs.push('advanced');
  if (mode === 'agent') tabs.push('override');
  if (role === 'platform_admin' && mode === 'server-admin') tabs.push('tenant');
  if (role === 'platform_admin' || role === 'org_admin' || role === 'agent_admin') tabs.push('test');
  return tabs;
}
