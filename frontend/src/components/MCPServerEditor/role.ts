import type { EditorRole } from './types';

/** Map auth store user.role to the editor's role taxonomy. */
export function effectiveEditorRole(user: { role: string } | null | undefined): EditorRole {
  if (!user) return 'member';
  if (user.role === 'platform_admin') return 'platform_admin';
  if (user.role === 'org_admin') return 'org_admin';
  if (user.role === 'agent_admin') return 'agent_admin';
  return 'member';
}
