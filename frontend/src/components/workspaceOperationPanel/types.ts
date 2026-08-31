export interface WorkspaceActivity {
    action: 'write' | 'edit' | 'move' | 'convert' | 'delete';
    path: string;
    tool?: string;
    ok?: boolean;
    pendingApproval?: boolean;
}

export interface WorkspaceLiveDraft {
    id: string;
    action: 'write' | 'edit' | 'move' | 'convert' | 'delete';
    tool: string;
    path?: string;
    content?: string;
    status: 'drafting' | 'running';
}

export interface WorkspaceFileNode {
    name: string;
    path: string;
    is_dir: boolean;
    children?: WorkspaceFileNode[];
}

export interface UploadItem {
    id: string;
    name: string;
    dir: string;
    progress: number;
    status: 'uploading' | 'processing' | 'done' | 'error';
    error?: string;
}

export interface WorkspaceOperationPanelProps {
    agentId: string;
    sessionId?: string;
    activePath?: string | null;
    activities: WorkspaceActivity[];
    liveDraft?: WorkspaceLiveDraft | null;
    locked?: boolean;
    canManageEnterpriseInfo?: boolean;
    canManageWorkspace?: boolean;
    onSelectPath: (path: string) => void;
    onToggleLock?: () => void;
    onEditingChange?: (editing: boolean) => void;
    onPathDeleted?: (path: string) => void;
    activityOpen?: boolean;
    onActivityToggle?: (open: boolean) => void;
    headerActionsTargetId?: string;
}

export type TreeScope = 'workspace' | 'all';
