import type { MCPServerEditorDraftOverride } from '../../../../types/mcpServer';

export type ToolsManagerProps = {
    agentId: string;
    agentName?: string;
    canManage?: boolean;
    canConfigure?: boolean;
    scope?: 'agent' | 'project';
    projectContext?: { projectId: string; memberId: string };
    draftTools?: any[];
    onDraftToolsChange?: (tools: any[]) => void;
    draftMcpOverrides?: Record<string, MCPServerEditorDraftOverride>;
    onDraftMcpOverridesChange?: (value: Record<string, MCPServerEditorDraftOverride>) => void;
};
