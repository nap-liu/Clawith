import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { useNavigate, useParams, useLocation } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import ConfirmModal from '../../components/ConfirmModal';
import { useDialog } from '../../components/Dialog/DialogProvider';
import { useToast } from '../../components/Toast/ToastProvider';
import type { FileBrowserApi } from '../../components/FileBrowser';
import FileBrowser from '../../components/FileBrowser';
import ChatAttachmentIcon from '../../components/ChatAttachmentIcon';
import ChatImageLightbox from '../../components/ChatImageLightbox';
import SessionViewerDrawer from '../../components/SessionViewerDrawer';
import type { SubagentRunCardData } from '../../components/SubagentRunCard';
import PromptModal from '../../components/PromptModal';
import { appendLiveCodeOutput, type LivePreviewState } from '../../components/AgentBayLivePanel';
import AgentSidePanel, { SidePanelTab } from '../../components/AgentSidePanel';
import type { WorkspaceActivity, WorkspaceLiveDraft } from '../../components/WorkspaceOperationPanel';
import { activityApi, agentApi, channelApi, chatSessionApi, enterpriseApi, fileApi, focusApi, scheduleApi, taskApi, tenantApi, triggerApi, uploadFileWithProgress } from '../../services/api';
import type { FocusApiItem } from '../../services/api';
import ModelSwitcher from '../../components/ModelSwitcher';
import { getChatToolRenderType } from '../../components/ChatToolCallRenderer';
import ConversationScrollToBottomButton from '../../features/conversation/ConversationScrollToBottomButton';
import ConversationTimeline from '../../features/conversation/web/ConversationTimeline';
import {
    bufferResumeEvent,
    createOrReuseResumeEventGate,
    drainResumeEventGate,
    prepareMessagesForActiveTurnResume,
    resolveVisibleTerminalRecoveryAction,
    shouldCompleteRecoveryPolling,
    shouldScheduleResumeReconnect,
    type ResumeEventGate,
} from '../../features/conversation/core/resumeRecovery';
import { buildConversationEntries, getConversationScrollAnchor, isA2AMessageLeft } from '../../features/conversation/core/chatTimeline';
import {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    conversationTurnIsStreaming,
    conversationTurnIsWaiting,
    conversationTurnEventClosesStream,
    conversationTurnEventShouldBeHandled,
    reduceConversationTurnEvent,
    type ConversationTurnRuntime,
} from '../../features/conversation/core/conversationTurnLifecycle';
import { useConversationAutoFollow } from '../../features/conversation/useConversationAutoFollow';
import {
    createConversationHistoryPageParams,
    resolveConversationHistoryHasMore,
} from '../../features/conversation/historyPagination';
import OrgMemberAccessPicker, {
    type AgentAccessDepartment,
    type AgentAccessUser,
} from '../../components/OrgMemberAccessPicker';
import { useAppStore } from '../../stores';
import './AccessPermissionsPanel.css';

// A confirmation card is just a `request_confirmation` tool_call rendered specially —
// the left/right perspective logic stays unaware of it; the renderer keys off the tool name.
const isConfirmationToolCall = (msg: any): boolean => {
    return getChatToolRenderType(msg) === 'confirmation';
};
const isPendingConfirmationToolCall = (msg: any): boolean => {
    if (!isConfirmationToolCall(msg)) return false;
    const parsed = (() => { try { return JSON.parse(msg.content || '{}'); } catch { return {}; } })();
    const status = msg.toolStatus || parsed.status;
    const args = msg.toolArgs || parsed.args || {};
    return (status === 'running' || status === 'pending')
        && args.force_confirmation !== false;
};
import { useAuthStore } from '../../stores';
import { copyToClipboard } from '../../utils/clipboard';
import { formatFileSize } from '../../utils/formatFileSize';
import {
    buildChatAttachmentPayload,
    buildPreviewImagesFromAttachments,
    downloadChatAttachment,
    extractChatImageDataMarkers,
    normalizeChatAttachmentFields,
    type ChatAttachedFile,
    type ChatMessageAttachment,
    type ChatPreviewImage,
    type ChatQuotedMessage,
} from '../../utils/chatAttachments';
import { createClientId } from '../../utils/clientId';
import {
    applyAssistantDoneMessage,
    applyAssistantMessageCommitted,
    applyAssistantStreamMessage,
    applyConfirmationRequiredEvent,
    applyUserMessageCommitted,
    foldConversationTimelineEvent,
    latestHistoryWindowOverlaps,
    normalizeChatTimelineMessages,
    reconcileLatestHistoryWindow,
    toolCallMessageFromEvent,
    upsertToolCallMessage as mergeToolCallMessage,
} from '../../features/conversation/core/chatTimeline';
import { parseChatSessionId, writeChatSessionIdToHref } from '../../utils/chatUrlParams';
import {
    IconBrain,
    IconBuilding,
    IconCheck,
    IconClock,
    IconChevronDown,
    IconDna,
    IconDownload,
    IconEye,
    IconFileText,
    IconFolder,
    IconHeartbeat,
    IconLock,
    IconMailForward,
    IconMessageCircle,
    IconPaperclip,
    IconPlugConnected,
    IconRobot,
    IconSend,
    IconSettings,
    IconUser,
    IconWorld,
    IconBolt,
    IconAlertTriangle,
} from '@tabler/icons-react';
import { useDropZone } from '../../hooks/useDropZone';
import {
    useOnboardingKickoff,
    type OnboardingKickoffRequest,
} from '../../hooks/useOnboardingKickoff';
import ApprovalsTab from './tabs/ApprovalsTab';
import { AGENT_DETAIL_TABS, type AgentDetailTab } from './agentDetailTabs';
import MindTab from './tabs/MindTab';
import SettingsTab from './tabs/SettingsTab';
import SceneConfigTab from './tabs/SceneConfigTab';
import { useUnsavedChangesGuard } from '../../hooks/useUnsavedChangesGuard';
import SkillsTab from './tabs/SkillsTab';
import ToolsTab from './tabs/ToolsTab';
import VirtualSessionList from './components/VirtualSessionList';
import { useAgentDetailRoute } from './hooks/useAgentDetailRoute';
import { fetchAuth } from './utils/fetchAuth';

const WORKSPACE_TOOLS = new Set([
    'write_file',
    'edit_file',
    'move_file',
    'delete_file',
    'convert_markdown_to_docx',
    'convert_csv_to_xlsx',
    'convert_markdown_to_pdf',
    'convert_html_to_pdf',
    'convert_html_to_pptx',
]);

const AWARE_TOOLS = new Set(['set_trigger', 'update_trigger', 'cancel_trigger', 'list_triggers', 'list_focus_items', 'upsert_focus_item', 'complete_focus_item']);
const SESSION_PAGE_SIZE = 40;
const mergeSessionsById = (first: any[], second: any[]) => {
    const seen = new Set<string>();
    return [...first, ...second].filter((session) => {
        const sessionId = String(session.id);
        if (seen.has(sessionId)) return false;
        seen.add(sessionId);
        return true;
    });
};
const trimLeadingPictograph = (value: string) => value.replace(/^\p{Extended_Pictographic}\s*/u, '');
const formatReflectionTitle = (value: string | undefined, isZh: boolean) => {
    const clean = trimLeadingPictograph(value || 'Trigger execution').trim();
    const legacyMatch = clean.match(/^内心独白[:：]\s*(.*)$/);
    if (legacyMatch) return isZh ? `内心独白：${legacyMatch[1]}` : `Reflection: ${legacyMatch[1]}`;
    return clean;
};
// React Router unmounts this page while an agent turn can keep running on the
// server. Keep only the affected runtime keys long enough for the next mount to
// close the durable-history gap; ordinary completed sessions never enter here.
const pendingPcRouteRecoveryRuntimeKeys = new Set<string>();

type FocusItem = {
    id: string;
    name: string;
    title?: string | null;
    description: string;
    done: boolean;
    inProgress: boolean;
    section: 'active' | 'system' | 'completed';
    synthetic?: boolean;
    system?: boolean;
};

type ExecutionUserOption = {
    id: string;
    display_name?: string | null;
    username?: string | null;
    email?: string | null;
};

type ExecutionIdentityRailProps = {
    creatorId?: string | null;
    creatorName?: string | null;
    executionUserId?: string | null;
    executionUserName?: string | null;
    users: ExecutionUserOption[];
    canReassign: boolean;
    isPending: boolean;
    onChoose: () => void;
};

const shortIdentity = (userId?: string | null) => userId ? userId.slice(0, 8) : '—';

function ExecutionIdentityRail({
    creatorId,
    creatorName,
    executionUserId,
    executionUserName,
    users,
    canReassign,
    isPending,
    onChoose,
}: ExecutionIdentityRailProps) {
    const { t } = useTranslation();
    const effectiveExecutionUserId = executionUserId || creatorId || '';
    const labelFor = (userId?: string | null, preferredName?: string | null) => {
        if (preferredName) return preferredName;
        const user = users.find((item) => item.id === userId);
        return user?.display_name || user?.username || user?.email || shortIdentity(userId);
    };
    const creatorLabel = labelFor(creatorId, creatorName);
    const executionLabel = labelFor(effectiveExecutionUserId, executionUserName);

    return (
        <div className="execution-identity-rail" onClick={(event) => event.stopPropagation()}>
            <span className="execution-identity-person" title={creatorId || undefined}>
                <span className="execution-identity-label">{t('agent.aware.executionIdentity.createdBy')}</span>
                <span className="execution-identity-value">{creatorLabel}</span>
            </span>
            <span className="execution-identity-arrow" aria-hidden="true">→</span>
            <span className="execution-identity-person">
                <span className="execution-identity-label">{t('agent.aware.executionIdentity.runsAs')}</span>
                {canReassign ? (
                    <button
                        type="button"
                        className="execution-identity-picker-trigger"
                        aria-label={t('agent.aware.executionIdentity.executionUser')}
                        title={t('agent.aware.executionIdentity.chooseExecutionUser')}
                        disabled={isPending || !effectiveExecutionUserId}
                        onClick={onChoose}
                    >
                        <span>{executionLabel}</span>
                        <IconChevronDown size={13} stroke={1.8} aria-hidden="true" />
                    </button>
                ) : (
                    <span className="execution-identity-value" title={effectiveExecutionUserId || undefined}>
                        {executionLabel}
                    </span>
                )}
            </span>
        </div>
    );
}

function focusItemFromApi(item: FocusApiItem): FocusItem {
    const done = item.status === 'completed';
    const system = item.kind === 'system';
    return {
        id: item.id,
        name: item.key,
        title: item.title,
        description: item.description || item.key,
        done,
        inProgress: !done,
        section: done ? 'completed' : (system ? 'system' : 'active'),
        system,
    };
}

function isFocusPath(path?: string | null): boolean {
    if (!path) return false;
    const normalized = path.replace(/^\/+/, '').toLowerCase();
    return normalized === 'focus.md' || normalized.endsWith('/focus.md');
}

function workspaceActionForTool(tool: string): WorkspaceLiveDraft['action'] {
    if (tool === 'edit_file') return 'edit';
    if (tool === 'move_file') return 'move';
    if (tool === 'delete_file') return 'delete';
    if (tool.startsWith('convert_')) return 'convert';
    return 'write';
}

function decodeJsonStringFragment(value: string): string {
    try {
        return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
    } catch {
        return value.replace(/\\n/g, '\n').replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
}

function readPartialJsonString(raw: string, key: string): string | undefined {
    const marker = `"${key}"`;
    const markerIdx = raw.indexOf(marker);
    if (markerIdx < 0) return undefined;
    const colonIdx = raw.indexOf(':', markerIdx + marker.length);
    if (colonIdx < 0) return undefined;
    const firstQuote = raw.indexOf('"', colonIdx + 1);
    if (firstQuote < 0) return undefined;
    let escaped = false;
    let value = '';
    for (let i = firstQuote + 1; i < raw.length; i += 1) {
        const ch = raw[i];
        if (escaped) {
            value += `\\${ch}`;
            escaped = false;
            continue;
        }
        if (ch === '\\') {
            escaped = true;
            continue;
        }
        if (ch === '"') break;
        value += ch;
    }
    return decodeJsonStringFragment(value);
}

function parseWorkspaceDraftArgs(tool: string, raw: string): Pick<WorkspaceLiveDraft, 'path' | 'content'> {
    let parsed: any = null;
    try {
        parsed = JSON.parse(raw || '{}');
    } catch {
        parsed = null;
    }
    const getString = (key: string) => {
        const parsedValue = parsed?.[key];
        if (typeof parsedValue === 'string') return parsedValue;
        return readPartialJsonString(raw || '', key);
    };
    const sourcePath = getString('source_path');
    const destinationPath = getString('destination_path');
    const path = destinationPath || getString('path') || getString('target_path') || sourcePath;
    let content = getString('content');
    if (tool === 'edit_file') content = getString('new_string') || content;
    return { path, content };
}

function parseFocusItems(raw: string): FocusItem[] {
    const lines = raw.split('\n');
    const focusItems: FocusItem[] = [];
    let currentItem: FocusItem | null = null;
    let currentSection: FocusItem['section'] = 'active';
    for (const line of lines) {
        const heading = line.match(/^##\s+(.+?)\s*$/);
        if (heading) {
            const title = heading[1].trim().toLowerCase();
            if (title === '已完成' || title === 'completed') currentSection = 'completed';
            else if (title === '系统 focus' || title === 'system focus' || title === 'system') currentSection = 'system';
            else if (title === '进行中' || title === 'in progress' || title === 'active') currentSection = 'active';
            continue;
        }
        const match = line.match(/^\s*-\s*\[([ x/])\]\s*(.+)/i);
        if (match) {
            if (currentItem) focusItems.push(currentItem);
            const marker = match[1];
            const fullText = match[2].trim();
            const systemKeyMatch = fullText.match(/^(system:[^:]+)\s*:\s*(.*)$/);
            const colonIdx = systemKeyMatch ? -1 : fullText.indexOf(':');
            const itemName = colonIdx > 0 ? fullText.substring(0, colonIdx).trim() : fullText;
            const itemDesc = colonIdx > 0 ? fullText.substring(colonIdx + 1).trim() : '';
            currentItem = {
                id: systemKeyMatch ? systemKeyMatch[1] : itemName,
                name: systemKeyMatch ? systemKeyMatch[1] : itemName,
                description: systemKeyMatch ? systemKeyMatch[2] : itemDesc,
                done: marker.toLowerCase() === 'x' || currentSection === 'completed',
                inProgress: marker === '/',
                section: systemKeyMatch ? 'system' : currentSection,
                system: currentSection === 'system' || !!systemKeyMatch,
            };
        } else if (currentItem && line.trim() && /^\s{2,}/.test(line)) {
            currentItem.description = currentItem.description
                ? `${currentItem.description} ${line.trim()}`
                : line.trim();
        }
    }
    if (currentItem) focusItems.push(currentItem);
    return focusItems;
}

function isOkrSystemTrigger(trig: any): boolean {
    if (!trig?.is_system) return false;
    const name = String(trig.name || '');
    return /(^|_)(okr|daily_okr|weekly_okr|biweekly_okr|monthly_okr|okr_collection|okr_report)/i.test(name);
}

function focusKeyFromTrigger(trig: any): string {
    if (trig?.focus_ref) return String(trig.focus_ref);
    if (isOkrSystemTrigger(trig)) return 'system:okr_reports';
    if (trig?.is_system) return `system:${String(trig.name || 'trigger')}`;
    return String(trig.name || trig.reason || 'trigger_focus');
}

function synthesizeFocusForTrigger(trig: any): FocusItem {
    const key = focusKeyFromTrigger(trig);
    const isSystem = !!trig.is_system || key.startsWith('system:');
    return {
        id: `synthetic:${key}`,
        name: key,
        description: key === 'system:okr_reports'
            ? 'OKR 自动汇总、日报收集与周期报告'
            : trig.reason || trig.name || key,
        done: !trig.is_enabled && !isSystem,
        inProgress: false,
        section: isSystem ? 'system' : 'active',
        synthetic: true,
        system: isSystem,
    };
}

function parseAgentBayTransferArgs(rawArgs: any): NonNullable<LivePreviewState['transfer']> {
    const parsed = typeof rawArgs === 'string'
        ? (() => {
            try { return JSON.parse(rawArgs || '{}'); } catch { return {}; }
        })()
        : (rawArgs || {});
    return {
        fromType: typeof parsed.from_type === 'string' ? parsed.from_type : undefined,
        fromPath: typeof parsed.from_path === 'string' ? parsed.from_path : undefined,
        toType: typeof parsed.to_type === 'string' ? parsed.to_type : undefined,
        toPath: typeof parsed.to_path === 'string' ? parsed.to_path : undefined,
        updatedAt: Date.now(),
    };
}

function workspaceFileName(path: string): string {
    return path.replace(/^workspace\//, '') || path;
}

// Format large token numbers with K/M suffixes
const formatTokens = (n: number) => {
    if (!n) return '0';
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return String(n);
};

const formatTokensParts = (n: number): { value: string; unit: string } => {
    if (!n) return { value: '0', unit: '' };
    if (n >= 1000000) return { value: (n / 1000000).toFixed(1), unit: 'M' };
    if (n >= 1000) return { value: (n / 1000).toFixed(1), unit: 'K' };
    return { value: String(n), unit: '' };
};

/** Convert rich schedule JSON to cron expression */
function schedToCron(sched: { freq: string; interval: number; time: string; weekdays?: number[] }): string {
    const [h, m] = (sched.time || '09:00').split(':').map(Number);
    if (sched.freq === 'weekly') {
        const days = (sched.weekdays || [1, 2, 3, 4, 5]).join(',');
        return sched.interval > 1 ? `${m} ${h} * * ${days}` : `${m} ${h} * * ${days}`;
    }
    // daily
    if (sched.interval === 1) return `${m} ${h} * * *`;
    return `${m} ${h} */${sched.interval} * *`;
}

const getRelationOptions = (t: any) => [
    { value: 'supervisor', label: t('agent.detail.supervisor') },
    { value: 'subordinate', label: t('agent.detail.subordinate') },
    { value: 'collaborator', label: t('agent.detail.collaborator') },
    { value: 'peer', label: t('agent.detail.peer') },
    { value: 'mentor', label: t('agent.detail.mentor') },
    { value: 'stakeholder', label: t('agent.detail.stakeholder') },
    { value: 'other', label: t('agent.detail.other') },
];

const getAgentRelationOptions = getRelationOptions;

type AccessUser = AgentAccessUser;
type AccessDepartment = AgentAccessDepartment;

function AccessPermissionsPanel({
    agentId,
    permData,
    canManage,
    queryClient,
}: {
    agentId: string;
    permData: any;
    canManage: boolean;
    queryClient: any;
}) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const canManagePermissions = permData?.can_manage ?? canManage;
    const isOwner = permData?.is_owner ?? false;
    const currentScope = permData?.scope_type === 'user' ? 'private' : (permData?.scope_type || 'company');
    const currentAccessLevel = permData?.access_level || 'use';
    const [localScope, setLocalScope] = useState(currentScope);
    const [localAccessLevel, setLocalAccessLevel] = useState(currentAccessLevel);
    const [savingScope, setSavingScope] = useState<string | null>(null);
    const [permissionError, setPermissionError] = useState<string | null>(null);
    const [showMemberPicker, setShowMemberPicker] = useState(false);
    const userAccess: AccessUser[] = useMemo(
        () => (permData?.user_access || []).map((u: any) => ({
            id: u.id,
            name: u.name,
            username: u.username,
            email: u.email,
            title: u.title,
            avatar_url: u.avatar_url,
            department_path: u.department_path,
            access_level: u.access_level === 'manage' ? 'manage' : 'use',
            is_required: !!u.is_required,
            required_reason: u.required_reason || null,
        })),
        [permData?.user_access],
    );
    const departmentAccess: AccessDepartment[] = useMemo(
        () => (permData?.department_access || []).map((department: any) => ({
            id: department.id,
            name: department.name,
            path: department.path,
            access_level: department.access_level === 'manage' ? 'manage' : 'use',
            include_descendants: true,
        })),
        [permData?.department_access],
    );
    const toPermissionPayloadScope = (scope: string) => scope === 'private' ? 'user' : scope;
    const businessUsers = userAccess.filter(user => !user.is_required);
    const requiredUsers = userAccess.filter(user => user.is_required);

    useEffect(() => {
        setLocalScope(currentScope);
        setLocalAccessLevel(currentAccessLevel);
    }, [currentScope, currentAccessLevel]);

    const savePermissions = async (payload: any) => {
        setPermissionError(null);
        await fetchAuth(`/agents/${agentId}/permissions`, {
            method: 'PUT',
            body: JSON.stringify(payload),
        });
        queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] });
        queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
        queryClient.invalidateQueries({ queryKey: ['agents'] });
    };

    const scopeOptions = [
        {
            value: 'company',
            icon: <IconBuilding size={14} stroke={1.8} />,
            label: t('agent.settings.perm.companyWide', 'Company-wide'),
            desc: isChinese ? '所有平台用户和数字员工都可以访问。' : 'All platform users and digital employees can access it.',
        },
        {
            value: 'private',
            icon: <IconUser size={14} stroke={1.8} />,
            label: t('agent.settings.perm.onlyMe', 'Only Me'),
            desc: isChinese ? '只有创建者可以使用和管理。' : 'Only the creator can use and manage it.',
        },
        {
            value: 'custom',
            icon: <IconLock size={14} stroke={1.8} />,
            label: isChinese ? '指定访问' : 'Custom',
            desc: isChinese ? '指定可访问的部门或成员；数字员工关系请在“关系”里配置。' : 'Choose departments or members. Digital employee relationships are configured in Relationships.',
        },
    ] as const;

    const accessLevels = [
        { val: 'use', label: <><IconEye size={13} stroke={1.8} /> {t('agent.settings.perm.useAccess', 'Use')}</>, desc: t('agent.settings.perm.useAccessDesc', 'Task, Chat, Tools, Skills, Workspace') },
        { val: 'manage', label: <><IconSettings size={13} stroke={1.8} /> {t('agent.settings.perm.manageAccess', 'Manage')}</>, desc: t('agent.settings.perm.manageAccessDesc', 'Full access including Settings, Mind, Relationships') },
    ];

    const setScope = async (scope: string) => {
        if (!canManagePermissions) return;
        if (scope === 'private' && !isOwner) {
            setPermissionError(isChinese ? '仅创建者可以切换为“仅我可见”，否则管理员会立即失去管理入口。' : 'Only the creator can switch to Only Me, otherwise the manager would lose access immediately.');
            return;
        }
        const previousScope = localScope;
        setLocalScope(scope);
        setSavingScope(scope);
        try {
            await savePermissions({
                scope_type: toPermissionPayloadScope(scope),
                access_level: localAccessLevel,
                user_access: userAccess,
                department_access: departmentAccess,
            });
        } catch (e) {
            setLocalScope(previousScope);
            setPermissionError(e instanceof Error ? e.message : String(e));
            console.error('Failed to update permissions', e);
        } finally {
            setSavingScope(null);
        }
    };

    const setCompanyAccessLevel = async (level: string) => {
        const previousLevel = localAccessLevel;
        setLocalAccessLevel(level);
        setSavingScope(`level:${level}`);
        try {
            await savePermissions({
                scope_type: toPermissionPayloadScope(localScope),
                access_level: level,
                user_access: userAccess,
                department_access: departmentAccess,
            });
        } catch (e) {
            setLocalAccessLevel(previousLevel);
            setPermissionError(e instanceof Error ? e.message : String(e));
            console.error('Failed to update access level', e);
        } finally {
            setSavingScope(null);
        }
    };

    const saveCustomAccess = async (
        nextBusinessUsers: AccessUser[],
        nextDepartments: AccessDepartment[],
    ) => {
        try {
            await savePermissions({
                scope_type: 'custom',
                access_level: localAccessLevel,
                user_access: nextBusinessUsers,
                department_access: nextDepartments,
            });
        } catch (error) {
            setPermissionError(error instanceof Error ? error.message : String(error));
            throw error;
        }
    };

    return (
        <div className="card" style={{ marginBottom: '12px' }}>
            <h4 style={{ marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <IconLock size={16} stroke={1.8} /> {t('agent.settings.perm.title', 'Access Permissions')}
            </h4>
            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
                {t('agent.settings.perm.description', 'Control who can see and interact with this agent. Only the creator or admin can change this.')}
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '16px' }}>
                {scopeOptions.map((scope) => {
                    const disabled = !canManagePermissions || (scope.value === 'private' && !isOwner);
                    const selected = localScope === scope.value;
                    return (
                    <button
                        key={scope.value}
                        type="button"
                        disabled={!canManagePermissions}
                        onClick={() => setScope(scope.value)}
                        style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '10px',
                            width: '100%',
                            textAlign: 'left',
                            padding: '12px 14px',
                            borderRadius: '8px',
                            cursor: disabled ? 'not-allowed' : 'pointer',
                            border: selected ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                            background: selected ? 'rgba(99,102,241,0.06)' : 'transparent',
                            opacity: disabled ? 0.55 : 1,
                            transition: 'all 0.15s',
                        }}
                    >
                        <input
                            type="radio"
                            name="perm_scope"
                            checked={selected}
                            disabled={disabled}
                            readOnly
                            style={{ accentColor: 'var(--accent-primary)' }}
                        />
                        <div>
                            <div style={{ fontWeight: 500, fontSize: '13px', display: 'flex', alignItems: 'center', gap: '5px' }}>
                                {scope.icon} {scope.label}
                                {savingScope === scope.value && <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 400 }}>{isChinese ? '保存中...' : 'Saving...'}</span>}
                            </div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{scope.desc}</div>
                        </div>
                    </button>
                );})}
            </div>

            {permissionError && (
                <div style={{ margin: '-4px 0 12px', fontSize: '12px', color: 'var(--error)' }}>
                    {permissionError}
                </div>
            )}

            {localScope === 'company' && canManagePermissions && (
                <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
                    <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '8px' }}>
                        {t('agent.settings.perm.defaultAccess', 'Default Access Level')}
                    </label>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        {accessLevels.map(opt => (
                            <label key={opt.val}
                                style={{
                                    flex: 1,
                                    padding: '10px 12px',
                                    borderRadius: '8px',
                                    cursor: 'pointer',
                                    border: localAccessLevel === opt.val ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                                    background: localAccessLevel === opt.val ? 'rgba(99,102,241,0.06)' : 'transparent',
                                    transition: 'all 0.15s',
                                }}
                            >
                                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                    <input
                                        type="radio"
                                        name="access_level"
                                        checked={localAccessLevel === opt.val}
                                        onChange={() => setCompanyAccessLevel(opt.val)}
                                        style={{ accentColor: 'var(--accent-primary)' }}
                                    />
                                    <span style={{ fontWeight: 500, fontSize: '13px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>{opt.label}</span>
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', marginLeft: '20px' }}>{opt.desc}</div>
                            </label>
                        ))}
                    </div>
                </div>
            )}

            {localScope === 'custom' && canManagePermissions && (
                <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px' }}>
                        <div style={{ minWidth: 0 }}>
                            <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                                <IconBuilding size={14} stroke={1.8} />
                                {isChinese
                                    ? `已指定 ${departmentAccess.length} 个部门节点、${businessUsers.length} 名成员`
                                    : `${departmentAccess.length} departments and ${businessUsers.length} members selected`}
                            </div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                {isChinese
                                    ? `创建者及 ${Math.max(requiredUsers.length - 1, 0)} 名公司管理员保留管理权限`
                                    : `The creator and ${Math.max(requiredUsers.length - 1, 0)} company administrators retain manage access`}
                            </div>
                        </div>
                        <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            onClick={() => setShowMemberPicker(true)}
                            disabled={savingScope !== null}
                        >
                            {isChinese ? '选择部门或成员' : 'Choose Departments or Members'}
                        </button>
                    </div>
                    {(departmentAccess.length > 0 || businessUsers.length > 0) && (
                        <div className="agent-access-summary">
                            {departmentAccess.length > 0 && (
                                <div className="agent-access-summary__group">
                                    <div className="agent-access-summary__group-label">
                                        <IconBuilding size={13} stroke={1.8} />
                                        <span>{isChinese ? '部门权限' : 'Departments'}</span>
                                    </div>
                                    <div className="agent-access-summary__items">
                                        {departmentAccess.slice(0, 4).map(department => (
                                            <span key={department.id} className="agent-access-summary__item agent-access-summary__item--department" title={department.path}>
                                                <strong>{department.name}</strong>
                                                <span className="agent-access-summary__scope">{isChinese ? '含下级' : 'Descendants'}</span>
                                                <span className={`agent-access-summary__level is-${department.access_level}`}>
                                                    {department.access_level === 'manage' ? (isChinese ? '管理' : 'Manage') : (isChinese ? '使用' : 'Use')}
                                                </span>
                                            </span>
                                        ))}
                                        {departmentAccess.length > 4 && <span className="agent-access-summary__more">+{departmentAccess.length - 4}</span>}
                                    </div>
                                </div>
                            )}
                            {businessUsers.length > 0 && (
                                <div className="agent-access-summary__group">
                                    <div className="agent-access-summary__group-label">
                                        <IconUser size={13} stroke={1.8} />
                                        <span>{isChinese ? '成员权限' : 'Members'}</span>
                                    </div>
                                    <div className="agent-access-summary__items">
                                        {businessUsers.slice(0, 6).map(user => (
                                            <span key={user.id} className="agent-access-summary__item" title={user.department_path || user.email || user.name}>
                                                <strong>{user.name}</strong>
                                                <span className={`agent-access-summary__level is-${user.access_level}`}>
                                                    {user.access_level === 'manage' ? (isChinese ? '管理' : 'Manage') : (isChinese ? '使用' : 'Use')}
                                                </span>
                                            </span>
                                        ))}
                                        {businessUsers.length > 6 && <span className="agent-access-summary__more">+{businessUsers.length - 6}</span>}
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                    <OrgMemberAccessPicker
                        open={showMemberPicker}
                        agentId={agentId}
                        users={userAccess}
                        departments={departmentAccess}
                        onClose={() => setShowMemberPicker(false)}
                        onSave={saveCustomAccess}
                    />
                </div>
            )}

            {localScope !== 'company' && (
                <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    {isChinese ? '访问范围仅影响谁可以查看和使用该数字员工。' : 'Access scope controls who can view and use this digital employee.'}
                </div>
            )}

            {!canManagePermissions && (
                <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-tertiary)', fontStyle: 'italic' }}>
                    {t('agent.settings.perm.readOnly', 'Only the creator or admin can change permissions')}
                </div>
            )}
        </div>
    );
}

function RelationshipEditor({ agentId, readOnly = false }: { agentId: string; readOnly?: boolean }) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const humanSearchRef = useRef<HTMLDivElement>(null);
    const agentSearchRef = useRef<HTMLDivElement>(null);
    const getHumanMemberSourceLabel = useCallback((member: any) => {
        const providerName = (member?.provider_name || '').trim();
        const providerType = (member?.provider_type || '').trim().toLowerCase();
        if (!providerName || providerType === 'platform' || providerType === 'web' || providerName.toLowerCase() === 'web') {
            return isChinese ? '平台用户' : 'Platform User';
        }
        return providerName;
    }, [isChinese]);

    const renderHumanMemberSourceBadge = useCallback((member: any) => {
        const providerName = (member?.provider_name || '').trim();
        const providerType = (member?.provider_type || '').trim().toLowerCase();
        const isPlatformUser = !providerName || providerType === 'platform' || providerType === 'web' || providerName.toLowerCase() === 'web';
        const showPlatformBadge = Boolean(member?.is_platform_user) && !isPlatformUser;
        const badgeStyle = (platform: boolean): React.CSSProperties => ({
            display: 'inline-flex',
            alignItems: 'center',
            padding: '1px 6px',
            borderRadius: '999px',
            fontSize: '10px',
            fontWeight: 600,
            marginRight: '6px',
            background: platform ? 'rgba(99,102,241,0.10)' : 'rgba(16,185,129,0.10)',
            color: platform ? 'rgb(79,70,229)' : 'rgb(16,185,129)',
            border: platform ? '1px solid rgba(99,102,241,0.18)' : '1px solid rgba(16,185,129,0.18)',
        });
        return (
            <>
                <span style={badgeStyle(isPlatformUser)}>
                    {getHumanMemberSourceLabel(member)}
                </span>
                {showPlatformBadge && (
                    <span style={badgeStyle(true)}>
                        {isChinese ? '平台用户' : 'Platform User'}
                    </span>
                )}
            </>
        );
    }, [getHumanMemberSourceLabel, isChinese]);

    const getRestrictedTitle = useCallback((reason?: string | null) => {
        const reasonText = reason ? ` (${reason})` : '';
        return isChinese
            ? `当关系目标不存在、停用/过期，或当前访问权限不再允许这个 Agent 与该用户/Agent 互动时，会显示为 restricted。关系记录会保留，但运行时不会使用。${reasonText}`
            : `Restricted means the target is missing, inactive/expired, or current access permissions no longer allow this agent to interact with that user/agent. The record is kept, but runtime use is blocked.${reasonText}`;
    }, [isChinese]);

    const [restrictedTooltip, setRestrictedTooltip] = useState<{ text: string; x: number; y: number } | null>(null);
    const showRestrictedTooltip = useCallback((event: React.SyntheticEvent<HTMLElement>, reason?: string | null) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const tooltipWidth = Math.min(320, Math.max(220, window.innerWidth - 32));
        const x = Math.min(
            Math.max(rect.left + rect.width / 2, 16 + tooltipWidth / 2),
            window.innerWidth - 16 - tooltipWidth / 2,
        );
        setRestrictedTooltip({
            text: getRestrictedTitle(reason),
            x,
            y: rect.top - 8,
        });
    }, [getRestrictedTitle]);
    const hideRestrictedTooltip = useCallback(() => setRestrictedTooltip(null), []);

    const [search, setSearch] = useState('');
    const [showHumanForm, setShowHumanForm] = useState(false);
    const [searchResults, setSearchResults] = useState<any[]>([]);
    const [showMemberDropdown, setShowMemberDropdown] = useState(false);
    const [selectedMembers, setSelectedMembers] = useState<any[]>([]);
    const [relation, setRelation] = useState('collaborator');
    const [description, setDescription] = useState('');
    const [agentSearch, setAgentSearch] = useState('');
    const [showAgentForm, setShowAgentForm] = useState(false);
    const [agentSearchResults, setAgentSearchResults] = useState<any[]>([]);
    const [showAgentDropdown, setShowAgentDropdown] = useState(false);
    const [selectedAgents, setSelectedAgents] = useState<any[]>([]);
    const [agentRelation, setAgentRelation] = useState('collaborator');
    const [agentDescription, setAgentDescription] = useState('');
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editRelation, setEditRelation] = useState('');
    const [editDescription, setEditDescription] = useState('');
    const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
    const [editAgentRelation, setEditAgentRelation] = useState('');
    const [editAgentDescription, setEditAgentDescription] = useState('');
    const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

    const { data: relationships = [], refetch } = useQuery({
        queryKey: ['relationships', agentId],
        queryFn: () => fetchAuth<any[]>(`/agents/${agentId}/relationships/`),
    });
    const { data: agentRelationships = [], refetch: refetchAgentRels } = useQuery({
        queryKey: ['agent-relationships', agentId],
        queryFn: () => fetchAuth<any[]>(`/agents/${agentId}/relationships/agents`),
    });

    const relatedMemberIds = useMemo(() => new Set(relationships.map((r: any) => r.user_id)), [relationships]);
    const relatedAgentIds = useMemo(() => new Set(agentRelationships.map((r: any) => r.agent_id)), [agentRelationships]);
    const selectedMemberIds = useMemo(() => new Set(selectedMembers.map((m: any) => m.user_id)), [selectedMembers]);
    const selectedAgentIds = useMemo(() => new Set(selectedAgents.map((a: any) => a.agent_id)), [selectedAgents]);
    const relatedMemberById = useMemo(() => {
        const map = new Map<string, any>();
        relationships.forEach((r: any) => {
            if (r.user_id) map.set(r.user_id, r);
        });
        return map;
    }, [relationships]);

    const visibleMemberResults = useMemo(
        () => searchResults,
        [searchResults],
    );
    const visibleAgentResults = useMemo(
        () => agentSearchResults.filter((a: any) => !relatedAgentIds.has(a.agent_id)),
        [agentSearchResults, relatedAgentIds],
    );

    const loadOrgMembers = async (keyword = '') => {
        const query = keyword.trim() ? `?search=${encodeURIComponent(keyword.trim())}` : '';
        const results = await fetchAuth<any[]>(`/agents/${agentId}/relationships/member-candidates${query}`);
        setSearchResults(results);
    };

    const loadAgentCandidates = async (keyword = '') => {
        const query = keyword.trim() ? `?search=${encodeURIComponent(keyword.trim())}` : '';
        const results = await fetchAuth<any[]>(`/agents/${agentId}/relationships/agent-candidates${query}`);
        setAgentSearchResults(results);
    };

    useEffect(() => {
        if (!search || search.length < 1) { setSearchResults([]); return; }
        const timer = setTimeout(() => {
            loadOrgMembers(search);
        }, 300);
        return () => clearTimeout(timer);
    }, [search]);

    useEffect(() => {
        if (!agentSearch || agentSearch.length < 1) { setAgentSearchResults([]); return; }
        const timer = setTimeout(() => {
            loadAgentCandidates(agentSearch);
        }, 300);
        return () => clearTimeout(timer);
    }, [agentId, agentSearch]);

    useEffect(() => {
        const handleClickOutside = (e: MouseEvent) => {
            const target = e.target as Node;
            if (showMemberDropdown && humanSearchRef.current && !humanSearchRef.current.contains(target)) {
                setShowMemberDropdown(false);
            }
            if (showAgentDropdown && agentSearchRef.current && !agentSearchRef.current.contains(target)) {
                setShowAgentDropdown(false);
            }
        };
        if (showMemberDropdown || showAgentDropdown) {
            document.addEventListener('mousedown', handleClickOutside);
        }
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [showMemberDropdown, showAgentDropdown]);

    const resetHumanDraft = () => {
        setShowHumanForm(false);
        setSearch('');
        setSearchResults([]);
        setShowMemberDropdown(false);
        setSelectedMembers([]);
        setRelation('collaborator');
        setDescription('');
    };

    const resetAgentDraft = () => {
        setShowAgentForm(false);
        setAgentSearch('');
        setAgentSearchResults([]);
        setShowAgentDropdown(false);
        setSelectedAgents([]);
        setAgentRelation('collaborator');
        setAgentDescription('');
    };

    const toggleMemberSelection = (member: any) => {
        setSelectedMembers(prev =>
            prev.some((item: any) => item.user_id === member.user_id)
                ? prev.filter((item: any) => item.user_id !== member.user_id)
                : [...prev, member]
        );
    };

    const toggleAgentSelection = (agent: any) => {
        setSelectedAgents(prev =>
            prev.some((item: any) => item.agent_id === agent.agent_id)
                ? prev.filter((item: any) => item.agent_id !== agent.agent_id)
                : [...prev, agent]
        );
    };

    const addRelationship = async () => {
        if (!selectedMembers.length) return;
        const existing = new Map(
            relationships.map((r: any) => [r.user_id, { user_id: r.user_id, relation: r.relation, description: r.description }])
        );
        selectedMembers.forEach((member: any) => {
            existing.set(member.user_id, { user_id: member.user_id, relation, description });
        });
        await fetchAuth(`/agents/${agentId}/relationships/`, { method: 'PUT', body: JSON.stringify({ relationships: Array.from(existing.values()) }) });
        resetHumanDraft();
        refetch();
    };

    const removeRelationship = async (relId: string) => {
        setDeletingIds(prev => new Set(prev).add(relId));
        try {
            await fetchAuth(`/agents/${agentId}/relationships/${relId}`, { method: 'DELETE' });
            refetch();
        } catch {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
            refetch();
        } finally {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
        }
    };

    const startEditRelationship = (r: any) => {
        setEditingId(r.id);
        setEditRelation(r.relation || 'collaborator');
        setEditDescription(r.description || '');
    };

    const saveEditRelationship = async (targetId: string) => {
        const updated = relationships.map((r: any) => ({
            user_id: r.user_id,
            relation: r.id === targetId ? editRelation : r.relation,
            description: r.id === targetId ? editDescription : r.description,
        }));
        await fetchAuth(`/agents/${agentId}/relationships/`, { method: 'PUT', body: JSON.stringify({ relationships: updated }) });
        setEditingId(null);
        refetch();
    };

    const addAgentRelationship = async () => {
        if (!selectedAgents.length) return;
        const existing = new Map(
            agentRelationships.map((r: any) => [r.agent_id, { agent_id: r.agent_id, relation: r.relation, description: r.description }])
        );
        selectedAgents.forEach((agent: any) => {
            existing.set(agent.agent_id, { agent_id: agent.agent_id, relation: agentRelation, description: agentDescription });
        });
        await fetchAuth(`/agents/${agentId}/relationships/agents`, { method: 'PUT', body: JSON.stringify({ relationships: Array.from(existing.values()) }) });
        resetAgentDraft();
        refetchAgentRels();
    };

    const removeAgentRelationship = async (relId: string) => {
        setDeletingIds(prev => new Set(prev).add(relId));
        try {
            await fetchAuth(`/agents/${agentId}/relationships/agents/${relId}`, { method: 'DELETE' });
            refetchAgentRels();
        } catch {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
            refetchAgentRels();
        } finally {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
        }
    };

    const startEditAgentRelationship = (r: any) => {
        setEditingAgentId(r.id);
        setEditAgentRelation(r.relation || 'collaborator');
        setEditAgentDescription(r.description || '');
    };

    const saveEditAgentRelationship = async (targetId: string) => {
        const updated = agentRelationships.map((r: any) => ({
            agent_id: r.agent_id,
            relation: r.id === targetId ? editAgentRelation : r.relation,
            description: r.id === targetId ? editAgentDescription : r.description,
        }));
        await fetchAuth(`/agents/${agentId}/relationships/agents`, { method: 'PUT', body: JSON.stringify({ relationships: updated }) });
        setEditingAgentId(null);
        refetchAgentRels();
    };

    return (
        <div>
            {restrictedTooltip && (
                <div
                    style={{
                        position: 'fixed',
                        left: restrictedTooltip.x,
                        top: restrictedTooltip.y,
                        transform: 'translate(-50%, -100%)',
                        zIndex: 10000,
                        width: 'max-content',
                        maxWidth: 'min(320px, calc(100vw - 32px))',
                        padding: '8px 10px',
                        borderRadius: '8px',
                        border: '1px solid var(--border-subtle)',
                        background: 'var(--bg-primary)',
                        color: 'var(--text-primary)',
                        boxShadow: '0 10px 30px rgba(0,0,0,0.16)',
                        fontSize: '12px',
                        lineHeight: 1.45,
                        whiteSpace: 'normal',
                        overflowWrap: 'anywhere',
                        wordBreak: 'break-word',
                        pointerEvents: 'none',
                    }}
                >
                    {restrictedTooltip.text}
                </div>
            )}
            <div className="card" style={{ marginBottom: '12px' }}>
                <h4 style={{ marginBottom: '12px' }}>{t('agent.detail.humanRelationships')}</h4>
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>{t('agent.detail.humanRelationships')}</p>
                {relationships.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
                        {relationships.map((r: any) => (
                            <div key={r.id} style={{
                                borderRadius: '8px', border: '1px solid var(--border-subtle)',
                                overflow: 'hidden',
                                opacity: deletingIds.has(r.id) ? 0.4 : 1,
                                transition: 'opacity 0.2s ease',
                                pointerEvents: deletingIds.has(r.id) ? 'none' : 'auto',
                            }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px' }}>
                                    <div style={{ width: '36px', height: '36px', borderRadius: '50%', background: 'rgba(224,238,238,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '16px', fontWeight: 600, flexShrink: 0 }}>{r.member?.name?.[0] || '?'}</div>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div style={{ fontWeight: 600, fontSize: '13px' }}>
                                            {r.member?.name || '?'} <span className="badge" style={{ fontSize: '10px', marginLeft: '4px' }}>{r.relation_label}</span>
                                            {r.access_status && r.access_status !== 'active' && (
                                                <span
                                                    className="badge"
                                                    onMouseEnter={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onMouseLeave={hideRestrictedTooltip}
                                                    onFocus={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onBlur={hideRestrictedTooltip}
                                                    tabIndex={0}
                                                    style={{ fontSize: '10px', marginLeft: '4px', color: 'var(--warning)', background: 'rgba(245,158,11,0.12)', cursor: 'help' }}
                                                >
                                                    {r.access_status}
                                                </span>
                                            )}
                                        </div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {renderHumanMemberSourceBadge(r.member)}
                                            {r.member?.department_path || ''} · {r.member?.email || ''}
                                        </div>
                                        {r.description && editingId !== r.id && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>{r.description}</div>}
                                    </div>
                                    {!readOnly && editingId !== r.id && (
                                        <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
                                            <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={() => startEditRelationship(r)}>{t('common.edit', 'Edit')}</button>
                                            <button
                                                className="btn btn-ghost"
                                                style={{ color: deletingIds.has(r.id) ? 'var(--text-tertiary)' : 'var(--error)', fontSize: '12px' }}
                                                disabled={deletingIds.has(r.id)}
                                                onClick={() => removeRelationship(r.id)}
                                            >
                                                {deletingIds.has(r.id) ? t('common.deleting', 'Deleting...') : t('common.delete')}
                                            </button>
                                        </div>
                                    )}
                                </div>
                                {editingId === r.id && (
                                    <div style={{ padding: '0 10px 10px', borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}>
                                        <div style={{ display: 'flex', gap: '8px', marginTop: '8px', marginBottom: '8px' }}>
                                            <select className="input" value={editRelation} onChange={e => setEditRelation(e.target.value)} style={{ width: '140px', fontSize: '12px' }}>
                                                {getRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                            </select>
                                        </div>
                                        <textarea className="input" value={editDescription} onChange={e => setEditDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px', width: '100%' }} placeholder={t('agent.detail.descriptionPlaceholder', 'Description...')} />
                                        <div style={{ display: 'flex', gap: '8px' }}>
                                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={() => saveEditRelationship(r.id)}>{t('common.save', 'Save')}</button>
                                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditingId(null)}>{t('common.cancel')}</button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
                {!readOnly && !showHumanForm && (
                    <button className="btn btn-secondary" type="button" onClick={() => setShowHumanForm(true)}>
                        {t('agent.detail.addRelationship', 'Add Relationship')}
                    </button>
                )}
                {!readOnly && showHumanForm && (
                    <div
                        style={{ border: '1px solid var(--border-subtle)', borderRadius: '8px', padding: '12px', background: 'var(--bg-elevated)' }}
                        onMouseDownCapture={(e) => {
                            const target = e.target as Node;
                            if (humanSearchRef.current && !humanSearchRef.current.contains(target)) {
                                setShowMemberDropdown(false);
                            }
                        }}
                    >
                        <div ref={humanSearchRef} style={{ position: 'relative', marginBottom: '8px' }}>
                            <input
                                className="input"
                                placeholder={t('agent.detail.searchMembers')}
                                value={search}
                                onChange={e => {
                                    setSearch(e.target.value);
                                    setShowMemberDropdown(true);
                                }}
                                onFocus={() => {
                                    setShowMemberDropdown(true);
                                    if (!search.trim() && searchResults.length === 0) {
                                        loadOrgMembers();
                                    }
                                }}
                                style={{ fontSize: '13px' }}
                            />
                            {showMemberDropdown && visibleMemberResults.length > 0 && (
                                <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', marginTop: '4px', maxHeight: '200px', overflowY: 'auto', zIndex: 10, boxShadow: '0 4px 12px rgba(0,0,0,0.15)' }}>
                                    {visibleMemberResults.map((m: any) => {
                                        const existingRelationship = relatedMemberById.get(m.user_id);
                                        const alreadyAdded = Boolean(existingRelationship);
                                        const checked = alreadyAdded || selectedMemberIds.has(m.user_id);
                                        return (
                                            <div
                                                key={m.user_id}
                                                style={{
                                                    padding: '8px 12px',
                                                    cursor: alreadyAdded ? 'default' : 'pointer',
                                                    fontSize: '13px',
                                                    borderBottom: '1px solid var(--border-subtle)',
                                                    display: 'flex',
                                                    alignItems: 'flex-start',
                                                    gap: '8px',
                                                    opacity: alreadyAdded ? 0.72 : 1,
                                                }}
                                                onClick={() => {
                                                    if (!alreadyAdded) toggleMemberSelection(m);
                                                }}
                                                onMouseEnter={e => (e.currentTarget.style.background = alreadyAdded ? 'transparent' : 'var(--bg-elevated)')}
                                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                                                <input type="checkbox" checked={checked} disabled={alreadyAdded} readOnly style={{ marginTop: '2px' }} />
                                                <div style={{ minWidth: 0, flex: 1 }}>
                                                    <div style={{ fontWeight: 500 }}>
                                                        {m.name}
                                                        {alreadyAdded && (
                                                            <span className="badge" style={{ fontSize: '10px', marginLeft: '6px', color: 'var(--text-tertiary)', background: 'var(--bg-elevated)' }}>
                                                                {isChinese ? '已添加' : 'Added'}
                                                            </span>
                                                        )}
                                                        {alreadyAdded && existingRelationship?.relation_label && (
                                                            <span className="badge" style={{ fontSize: '10px', marginLeft: '4px' }}>
                                                                {existingRelationship.relation_label}
                                                            </span>
                                                        )}
                                                    </div>
                                                    {m.nickname && m.nickname !== m.name && (
                                                        <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                                                            {isChinese ? '昵称' : 'Nickname'}: {m.nickname}
                                                        </div>
                                                    )}
                                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                        {renderHumanMemberSourceBadge(m)}
                                                        {m.department_path} · {m.email}
                                                    </div>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                        {showMemberDropdown && search && visibleMemberResults.length === 0 && (
                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                {t('agent.detail.noSearchResults', 'No available results')}
                            </div>
                        )}
                        {selectedMembers.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', marginBottom: '10px' }}>
                                {selectedMembers.map((member: any) => (
                                    <div
                                        key={member.user_id}
                                        style={{
                                            display: 'inline-flex',
                                            alignItems: 'center',
                                            gap: '8px',
                                            border: '1px solid var(--border-subtle)',
                                            borderRadius: '10px',
                                            padding: '8px 10px',
                                            background: 'var(--bg-primary)',
                                            fontSize: '12px',
                                            lineHeight: 1.2,
                                        }}
                                    >
                                        <div style={{ width: '24px', height: '24px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '11px', flexShrink: 0 }}>
                                            {member.name?.[0] || '?'}
                                        </div>
                                        <div style={{ minWidth: 0 }}>
                                            <div style={{ fontWeight: 600 }}>{member.name}</div>
                                            {member.nickname && member.nickname !== member.name && (
                                                <div style={{ color: 'var(--text-secondary)', fontSize: '11px' }}>
                                                    {isChinese ? '昵称' : 'Nickname'}: {member.nickname}
                                                </div>
                                            )}
                                            <div style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>{member.department_path || member.email || ''}</div>
                                        </div>
                                        <button className="btn btn-ghost" type="button" style={{ fontSize: '12px', padding: 0, minWidth: 'auto', marginLeft: '2px' }} onClick={() => toggleMemberSelection(member)}>×</button>
                                    </div>
                                ))}
                            </div>
                        )}
                        <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                            <select className="input" value={relation} onChange={e => setRelation(e.target.value)} style={{ width: '160px', fontSize: '12px' }}>
                                {getRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                            </select>
                        </div>
                        <textarea className="input" placeholder="" value={description} onChange={e => setDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px' }} />
                        <div style={{ display: 'flex', gap: '8px' }}>
                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={addRelationship} disabled={selectedMembers.length === 0}>
                                {t('common.confirm')} {selectedMembers.length > 0 ? `(${selectedMembers.length})` : ''}
                            </button>
                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={resetHumanDraft}>
                                {t('common.cancel')}
                            </button>
                        </div>
                    </div>
                )}
            </div>
            <div className="card" style={{ marginBottom: '12px' }}>
                <h4 style={{ marginBottom: '12px' }}>{t('agent.detail.agentRelationships')}</h4>
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>{t('agent.detail.agentRelationships')}</p>
                {agentRelationships.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
                        {agentRelationships.map((r: any) => (
                            <div key={r.id} style={{
                                borderRadius: '8px',
                                border: `1px solid ${r.access_status && r.access_status !== 'active' ? 'rgba(245,158,11,0.35)' : 'rgba(16,185,129,0.3)'}`,
                                background: r.access_status && r.access_status !== 'active' ? 'rgba(245,158,11,0.06)' : 'rgba(16,185,129,0.05)', overflow: 'hidden',
                                opacity: deletingIds.has(r.id) ? 0.4 : 1,
                                transition: 'opacity 0.2s ease',
                                pointerEvents: deletingIds.has(r.id) ? 'none' : 'auto',
                            }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px' }}>
                                    <div style={{ width: '36px', height: '36px', borderRadius: '50%', background: 'rgba(16,185,129,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '16px', flexShrink: 0 }}>A</div>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div style={{ fontWeight: 600, fontSize: '13px' }}>
                                            {r.target_agent?.name || '?'} <span className="badge" style={{ fontSize: '10px', marginLeft: '4px', background: 'rgba(16,185,129,0.15)', color: 'rgb(16,185,129)' }}>{r.relation_label}</span>
                                            {r.access_status && r.access_status !== 'active' && (
                                                <span
                                                    className="badge"
                                                    onMouseEnter={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onMouseLeave={hideRestrictedTooltip}
                                                    onFocus={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onBlur={hideRestrictedTooltip}
                                                    tabIndex={0}
                                                    style={{ fontSize: '10px', marginLeft: '4px', color: 'var(--warning)', background: 'rgba(245,158,11,0.12)', cursor: 'help' }}
                                                >
                                                    {r.access_status}
                                                </span>
                                            )}
                                        </div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {r.target_agent?.role_description || 'Agent'}
                                            {r.access_status_reason ? ` · ${r.access_status_reason}` : ''}
                                        </div>
                                        {r.description && editingAgentId !== r.id && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>{r.description}</div>}
                                    </div>
                                    {!readOnly && editingAgentId !== r.id && (
                                        <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
                                            <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={() => startEditAgentRelationship(r)}>{t('common.edit', 'Edit')}</button>
                                            <button
                                                className="btn btn-ghost"
                                                style={{ color: deletingIds.has(r.id) ? 'var(--text-tertiary)' : 'var(--error)', fontSize: '12px' }}
                                                disabled={deletingIds.has(r.id)}
                                                onClick={() => removeAgentRelationship(r.id)}
                                            >
                                                {deletingIds.has(r.id) ? t('common.deleting', 'Deleting...') : t('common.delete')}
                                            </button>
                                        </div>
                                    )}
                                </div>
                                {editingAgentId === r.id && (
                                    <div style={{ padding: '0 10px 10px', borderTop: '1px solid rgba(16,185,129,0.2)', background: 'var(--bg-elevated)' }}>
                                        <div style={{ display: 'flex', gap: '8px', marginTop: '8px', marginBottom: '8px' }}>
                                            <select className="input" value={editAgentRelation} onChange={e => setEditAgentRelation(e.target.value)} style={{ width: '140px', fontSize: '12px' }}>
                                                {getAgentRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                            </select>
                                        </div>
                                        <textarea className="input" value={editAgentDescription} onChange={e => setEditAgentDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px', width: '100%' }} placeholder={t('agent.detail.descriptionPlaceholder', 'Description...')} />
                                        <div style={{ display: 'flex', gap: '8px' }}>
                                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={() => saveEditAgentRelationship(r.id)}>{t('common.save', 'Save')}</button>
                                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditingAgentId(null)}>{t('common.cancel')}</button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
                {!readOnly && !showAgentForm && (
                    <button className="btn btn-secondary" type="button" onClick={() => setShowAgentForm(true)}>
                        {t('agent.detail.addRelationship', 'Add Relationship')}
                    </button>
                )}
                {!readOnly && showAgentForm && (
                    <div
                        style={{ border: '1px solid rgba(16,185,129,0.3)', borderRadius: '8px', padding: '12px', background: 'var(--bg-elevated)' }}
                        onMouseDownCapture={(e) => {
                            const target = e.target as Node;
                            if (agentSearchRef.current && !agentSearchRef.current.contains(target)) {
                                setShowAgentDropdown(false);
                            }
                        }}
                    >
                        <div ref={agentSearchRef} style={{ position: 'relative', marginBottom: '8px' }}>
                            <input
                                className="input"
                                placeholder={t('agent.detail.searchAgents', '搜索可见数字员工...')}
                                value={agentSearch}
                                onChange={e => {
                                    setAgentSearch(e.target.value);
                                    setShowAgentDropdown(true);
                                }}
                                onFocus={() => {
                                    setShowAgentDropdown(true);
                                    if (!agentSearch.trim() && agentSearchResults.length === 0) {
                                        loadAgentCandidates();
                                    }
                                }}
                                style={{ fontSize: '13px' }}
                            />
                            {showAgentDropdown && visibleAgentResults.length > 0 && (
                                <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', marginTop: '4px', maxHeight: '200px', overflowY: 'auto', zIndex: 10, boxShadow: '0 4px 12px rgba(0,0,0,0.15)' }}>
                                    {visibleAgentResults.map((agent: any) => {
                                        const checked = selectedAgentIds.has(agent.agent_id);
                                        return (
                                            <div key={agent.agent_id} style={{ padding: '8px 12px', cursor: 'pointer', fontSize: '13px', borderBottom: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'flex-start', gap: '8px' }}
                                                onClick={() => toggleAgentSelection(agent)}
                                                onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-elevated)')}
                                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                                                <input type="checkbox" checked={checked} readOnly style={{ marginTop: '2px' }} />
                                                <div style={{ minWidth: 0, flex: 1 }}>
                                                    <div style={{ fontWeight: 500 }}>{agent.name}</div>
                                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{agent.role_description || 'Agent'}</div>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                        {showAgentDropdown && agentSearch && visibleAgentResults.length === 0 && (
                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                {t('agent.detail.noSearchResults', 'No available results')}
                            </div>
                        )}
                        {selectedAgents.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', marginBottom: '10px' }}>
                                {selectedAgents.map((agent: any) => (
                                    <div
                                        key={agent.agent_id}
                                        style={{
                                            display: 'inline-flex',
                                            alignItems: 'center',
                                            gap: '8px',
                                            border: '1px solid rgba(16,185,129,0.24)',
                                            borderRadius: '10px',
                                            padding: '8px 10px',
                                            background: 'var(--bg-primary)',
                                            fontSize: '12px',
                                            lineHeight: 1.2,
                                        }}
                                    >
                                        <div style={{ width: '24px', height: '24px', borderRadius: '50%', background: 'rgba(16,185,129,0.12)', color: 'rgb(16,185,129)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '11px', flexShrink: 0 }}>
                                            {agent.name?.[0] || 'A'}
                                        </div>
                                        <div style={{ minWidth: 0 }}>
                                            <div style={{ fontWeight: 600 }}>{agent.name}</div>
                                            <div style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>{agent.role_description || 'Agent'}</div>
                                        </div>
                                        <button className="btn btn-ghost" type="button" style={{ fontSize: '12px', padding: 0, minWidth: 'auto', marginLeft: '2px' }} onClick={() => toggleAgentSelection(agent)}>×</button>
                                    </div>
                                ))}
                            </div>
                        )}
                        <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                            <select className="input" value={agentRelation} onChange={e => setAgentRelation(e.target.value)} style={{ width: '160px', flexShrink: 0, fontSize: '12px' }}>
                                {getAgentRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                            </select>
                        </div>
                        <textarea className="input" placeholder="" value={agentDescription} onChange={e => setAgentDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px' }} />
                        <div style={{ display: 'flex', gap: '8px' }}>
                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={addAgentRelationship} disabled={selectedAgents.length === 0}>
                                {t('common.confirm')} {selectedAgents.length > 0 ? `(${selectedAgents.length})` : ''}
                            </button>
                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={resetAgentDraft}>
                                {t('common.cancel')}
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

export default function AgentDetailPage() {
    const { t, i18n } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const { id } = useParams<{ id: string }>();
    const navigate = useNavigate();
    const location = useLocation();
    const skipNextSessionUrlRestoreRef = useRef(false);
    const requestedSessionId = useMemo(
        () => parseChatSessionId(new URLSearchParams(location.search).get('session_id')),
        [location.search],
    );
    const writeSessionIdToUrl = useCallback((sessionId: string | null | undefined) => {
        const nextHref = writeChatSessionIdToHref(window.location.href, sessionId);
        const currentHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        if (nextHref === currentHref) return false;
        navigate(nextHref, { replace: true });
        return true;
    }, [navigate]);
    const queryClient = useQueryClient();
    const currentUser = useAuthStore((s) => s.user);
    const [sceneConfigDirty, setSceneConfigDirty] = useState(false);
    const {
        activeTab,
        isChatRoute,
        isSettingsRoute,
        setActiveTab: setActiveTabRoute,
    } = useAgentDetailRoute({ agentId: id });
    useUnsavedChangesGuard(
        activeTab === 'scenes' && sceneConfigDirty,
        '当前有未保存的编辑内容，继续操作将丢失这些修改。是否继续？',
    );
    const setActiveTab = setActiveTabRoute;

    const { data: agent, isLoading } = useQuery({
        queryKey: ['agent', id],
        queryFn: () => agentApi.get(id!),
        enabled: !!id,
    });

    useEffect(() => {
        if (
            agent
            && activeTab === 'scenes'
            && (agent.access_level !== 'manage' || !agent.scene_config_enabled)
        ) {
            setActiveTab('tools');
        }
    }, [activeTab, agent, setActiveTab]);

    // Tenant default model — used to render the "默认" tag and as a visual
    // fallback when an agent has no explicit primary model.
    const { data: myTenant } = useQuery({
        queryKey: ['tenant', 'me'],
        queryFn: () => tenantApi.me(),
        staleTime: 5 * 60 * 1000,
        refetchOnMount: 'always',
    });

    // Chat-side picker. The saved agent model is still the default source,
    // but ordinary collaborators must be able to pick a per-chat override
    // without needing permission to edit agent settings. Users with manage
    // access keep the previous behavior: picking here also updates the saved
    // agent default.
    const [overrideModelId, setOverrideModelId] = useState<string | null>(null);
    useEffect(() => {
        if (agent?.primary_model_id && agent.primary_model_id !== overrideModelId) {
            setOverrideModelId(agent.primary_model_id);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [agent?.primary_model_id]);

    const handleModelChange = useCallback((newModelId: string | null) => {
        setOverrideModelId(newModelId);
    }, []);

    const onboardingRequestsRef = useRef<Record<string, OnboardingKickoffRequest>>({});
    const [onboardingKickoffRequest, setOnboardingKickoffRequest] = useState<OnboardingKickoffRequest | null>(null);
    const [livePanelVisible, setLivePanelVisible] = useState(false);
    const [sidePanelTab, setSidePanelTab] = useState<SidePanelTab>('workspace');
    const awarePanelVisible = activeTab === 'chat' && livePanelVisible && sidePanelTab === 'aware';
    const awareDataActive = activeTab === 'aware' || awarePanelVisible;

    // ── Aware tab data: triggers ──
    const { data: awareTriggers = [], refetch: refetchTriggers } = useQuery({
        queryKey: ['triggers', id],
        queryFn: () => triggerApi.list(id!),
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 5000 : false,
    });
    const isPlatformAdmin = currentUser?.role === 'platform_admin'
        || !!(currentUser as any)?.is_platform_admin;
    const canReassignExecutionUser = (
        isPlatformAdmin || currentUser?.role === 'org_admin'
    ) && (agent as any)?.access_level === 'manage';
    const { data: executionUsers = [] } = useQuery({
        queryKey: ['background-execution-users', currentUser?.tenant_id],
        queryFn: () => enterpriseApi.listMembers(),
        enabled: !!id && awareDataActive && canReassignExecutionUser,
        staleTime: 60_000,
    });
    const [executionUserPickerTarget, setExecutionUserPickerTarget] = useState<{
        resourceType: 'trigger' | 'task' | 'schedule';
        resourceId: string;
        executionUserId: string;
        expectedExecutionUserId: string | null;
    } | null>(null);
    const reassignExecutionUser = useMutation({
        mutationFn: ({ resourceType, resourceId, executionUserId, expectedExecutionUserId }: {
            resourceType: 'trigger' | 'task' | 'schedule';
            resourceId: string;
            executionUserId: string;
            expectedExecutionUserId: string | null;
        }) => {
            const update = {
                execution_user_id: executionUserId,
                expected_execution_user_id: expectedExecutionUserId,
            };
            if (resourceType === 'trigger') return triggerApi.update(id!, resourceId, update);
            if (resourceType === 'task') return taskApi.update(id!, resourceId, update);
            return scheduleApi.update(id!, resourceId, update);
        },
        onSuccess: (_data, variables) => {
            const queryKey = variables.resourceType === 'trigger'
                ? ['triggers', id]
                : variables.resourceType === 'task'
                    ? ['tasks', id]
                    : ['schedules', id];
            queryClient.invalidateQueries({ queryKey });
            toast.success(t('agent.aware.executionIdentity.updated'));
        },
        onError: (err: any) => {
            toast.error(
                t('agent.aware.executionIdentity.updateFailed'),
                { details: String(err?.detail || err?.message || err) },
            );
        },
    });
    // ── Aware tab data: structured Focus ──
    const { data: focusRecords = [], refetch: refetchFocusItems } = useQuery({
        queryKey: ['focus', id],
        queryFn: () => focusApi.list(id!, true),
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 5000 : false,
    });

    // ── Aware tab data: task_history.md ──
    const { data: taskHistoryFile } = useQuery({
        queryKey: ['file', id, 'task_history.md'],
        queryFn: () => fileApi.read(id!, 'task_history.md').catch(() => null),
        enabled: !!id && awareDataActive,
    });

    // ── Aware tab data: reflection sessions (trigger monologues) ──
    const { data: awareSessionRows = [] } = useQuery({
        queryKey: ['reflection-sessions', id],
        queryFn: async () => {
            const tkn = localStorage.getItem('token');
            const res = await fetch(`/api/agents/${id}/sessions?scope=all`, { headers: { Authorization: `Bearer ${tkn}` } });
            if (!res.ok) return [];
            return await res.json();
        },
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 10000 : false,
    });
    const { data: triggerExecutions = [] } = useQuery({
        queryKey: ['trigger-executions', id],
        queryFn: () => triggerApi.executions(id!, 200),
        enabled: !!id && awareDataActive,
        refetchInterval: awareDataActive ? 10000 : false,
    });
    const reflectionSessions = useMemo(() => {
        const sessions = awareSessionRows as any[];
        const sessionById = new Map(sessions.map((session) => [session.id, session]));
        const linkedConversationIds = new Set<string>();
        const executionRows = (triggerExecutions as any[]).map((execution) => {
            const session = execution.conversation_id
                ? sessionById.get(execution.conversation_id)
                : null;
            if (execution.conversation_id) linkedConversationIds.add(execution.conversation_id);
            return {
                ...(session || {}),
                id: session?.id || `execution:${execution.id}`,
                record_id: execution.id,
                conversation_id: execution.conversation_id,
                conversation_missing: !session,
                title: execution.trigger_name,
                created_at: execution.scheduled_at,
                last_message_at: execution.finished_at || execution.scheduled_at,
                message_count: session?.message_count || 0,
                source_channel: session?.source_channel || execution.source,
                execution,
            };
        });
        const legacyRows = sessions
            .filter((session) => session.source_channel === 'trigger' && !linkedConversationIds.has(session.id))
            .map((session) => ({ ...session, record_id: `legacy:${session.id}`, conversation_id: session.id, execution: null }));
        return [...executionRows, ...legacyRows].sort((a, b) => (
            new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime()
        ));
    }, [awareSessionRows, triggerExecutions]);

    // ── Aware tab state ──
    const [expandedFocusIds, setExpandedFocusIds] = useState<Set<string>>(() => new Set());
    const [expandedReflection, setExpandedReflection] = useState<string | null>(null);
    const [reflectionMessages, setReflectionMessages] = useState<Record<string, any[]>>({});
    const [showAllFocus, setShowAllFocus] = useState(false);
    const [showCompletedFocus, setShowCompletedFocus] = useState(false);
    const [showAllReflections, setShowAllReflections] = useState(false);
    // Sidebar Focus group expand states
    const [showAllSideActive, setShowAllSideActive] = useState(false);
    const [showAllSideSystem, setShowAllSideSystem] = useState(false);
    const [showAllSideCompleted, setShowAllSideCompleted] = useState(false);
    const [awareView, setAwareView] = useState<'list' | 'calendar'>('list');
    const [awareCalendarMode, setAwareCalendarMode] = useState<'day' | 'week' | 'month'>('week');
    const [awareCalendarDate, setAwareCalendarDate] = useState<Date>(() => new Date());
    const [reflectionPage, setReflectionPage] = useState(0);
    const REFLECTIONS_PAGE_SIZE = 10;
    const SECTION_PAGE_SIZE = 5;

    const toggleExpandedFocus = (focusId: string) => {
        setExpandedFocusIds(prev => {
            const next = new Set(prev);
            if (next.has(focusId)) next.delete(focusId);
            else next.add(focusId);
            return next;
        });
    };

    const loadReflectionMessages = async (sessionId: string) => {
        if (!id || reflectionMessages[sessionId]) return;
        try {
            const tkn = localStorage.getItem('token');
            const res = await fetch(`/api/agents/${id}/sessions/${sessionId}/messages`, {
                headers: { Authorization: `Bearer ${tkn}` },
            });
            if (res.ok) {
                const data = await res.json();
                setReflectionMessages(prev => ({ ...prev, [sessionId]: data }));
            }
        } catch {
            // Reflection details are informational; keep the list usable if loading fails.
        }
    };

    const { data: soulContent } = useQuery({
        queryKey: ['file', id, 'soul.md'],
        queryFn: () => fileApi.read(id!, 'soul.md'),
        enabled: !!id && activeTab === 'mind',
    });

    const { data: memoryFiles = [] } = useQuery({
        queryKey: ['files', id, 'memory'],
        queryFn: () => fileApi.list(id!, 'memory'),
        enabled: !!id && activeTab === 'mind',
    });
    const [expandedMemory, setExpandedMemory] = useState<string | null>(null);
    const { data: memoryFileContent } = useQuery({
        queryKey: ['file', id, expandedMemory],
        queryFn: () => fileApi.read(id!, expandedMemory!),
        enabled: !!id && !!expandedMemory,
    });

    const { data: skillFiles = [] } = useQuery({
        queryKey: ['files', id, 'skills'],
        queryFn: () => fileApi.list(id!, 'skills'),
        enabled: !!id && activeTab === 'skills',
    });

    const [workspacePath, setWorkspacePath] = useState('workspace');
    const { data: workspaceFiles = [] } = useQuery({
        queryKey: ['files', id, workspacePath],
        queryFn: () => fileApi.list(id!, workspacePath),
        enabled: !!id && activeTab === 'workspace',
    });

    const { data: activityLogs = [] } = useQuery({
        queryKey: ['activity', id],
        queryFn: () => activityApi.list(id!, 100),
        enabled: !!id && (activeTab === 'activityLog' || activeTab === 'status'),
        refetchInterval: activeTab === 'activityLog' ? 10000 : false,
    });

    // Chat history
    // ── Session state (replaces old conversations query) ──────────────────
    const [sessions, setSessions] = useState<any[]>([]);
    const [allSessions, setAllSessions] = useState<any[]>([]);
    const [sessionsHasMore, setSessionsHasMore] = useState(false);
    const [allSessionsHasMore, setAllSessionsHasMore] = useState(false);
    const [sessionsNextCursor, setSessionsNextCursor] = useState<string | null>(null);
    const [allSessionsNextCursor, setAllSessionsNextCursor] = useState<string | null>(null);
    const [activeSession, setActiveSession] = useState<any | null>(null);
    const [subagentSessionRun, setSubagentSessionRun] = useState<SubagentRunCardData | null>(null);
    const openSubagentSession = useCallback((run: SubagentRunCardData) => setSubagentSessionRun(run), []);
    const closeSubagentSession = useCallback(() => setSubagentSessionRun(null), []);
    const { data: activeSessionExecution = null } = useQuery({
        queryKey: ['session-execution', id, activeSession?.id],
        queryFn: () => chatSessionApi.execution(id!, activeSession.id).catch(() => null),
        enabled: !!id && !!activeSession?.id,
        refetchInterval: activeSession?.id ? 10000 : false,
    });
    const [chatScope, setChatScope] = useState<'mine' | 'all'>('mine');
    const [scopeDropdownOpen, setScopeDropdownOpen] = useState(false);
    const scopeDropdownRef = useRef<HTMLDivElement>(null);
    const [historyMsgs, setHistoryMsgs] = useState<any[]>([]);
    // Cursor pagination (aligns with the backend `before` timestamp cursor on
    // GET /agents/{id}/sessions/{id}/messages): the created_at of the oldest
    // message currently loaded. Older pages are fetched with before=this.
    const [historyOldestTs, setHistoryOldestTs] = useState<string | null>(null);
    const [historyHasMore, setHistoryHasMore] = useState(true);
    const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [allSessionsLoading, setAllSessionsLoading] = useState(false);
    const [sessionsLoadingMore, setSessionsLoadingMore] = useState(false);
    const [allSessionsLoadingMore, setAllSessionsLoadingMore] = useState(false);
    const [agentExpired, setAgentExpired] = useState(false);
    // Websocket chat state (for 'me' conversation)
    const token = useAuthStore((s) => s.token);
    const isAgentOwner =
        currentUser?.id != null &&
        (agent as any)?.creator_id != null &&
        String((agent as any).creator_id) === String(currentUser.id);
    /** Chat sidebar: who may list all sessions & read others' threads (matches backend scope=all). */
    const canViewAllAgentChatSessions =
        currentUser?.role === 'platform_admin' ||
        currentUser?.role === 'org_admin' ||
        currentUser?.role === 'agent_admin' ||
        isAgentOwner;
    type SessionRuntimeKey = string;
    const wsMapRef = useRef<Record<SessionRuntimeKey, WebSocket>>({});
    const reconnectTimerRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setTimeout> | null>>({});
    const reconnectDisabledRef = useRef<Record<SessionRuntimeKey, boolean>>({});
    // Per-session reconnect attempt counter, driving exponential backoff. Reset
    // to 0 only after a socket proves stable (see onopen) so fast-fail loops back
    // off instead of hammering every 2s (the WebSocket reconnect-storm fix).
    const reconnectAttemptsRef = useRef<Record<SessionRuntimeKey, number>>({});
    const sessionUiStateRef = useRef<Record<SessionRuntimeKey, { isWaiting: boolean; isStreaming: boolean; isStopping: boolean }>>({});
    const sessionTurnRuntimeRef = useRef<Record<SessionRuntimeKey, ConversationTurnRuntime>>({});
    const activeSessionIdRef = useRef<string | null>(null);
    // True while the active session is a READ-ONLY monitor view (a session the
    // viewer may see but does not own). Live broadcasts are then mirrored into
    // `historyMsgs` (the read-only view's source) instead of `chatMessages`.
    const activeReadOnlyRef = useRef<boolean>(false);
    const currentAgentIdRef = useRef<string | undefined>(id);
    const sessionMsgAbortRef = useRef<AbortController | null>(null);
    const historyMoreAbortRef = useRef<AbortController | null>(null);
    const sessionsListAbortRef = useRef<AbortController | null>(null);
    const allSessionsListAbortRef = useRef<AbortController | null>(null);
    const sessionsListGenerationRef = useRef(0);
    const allSessionsListGenerationRef = useRef(0);
    const sessionLoadSeqRef = useRef(0);
    const pcPageSuspendedRef = useRef(false);
    const pcHiddenDroppedEventRef = useRef(false);
    const pcRecoveryPollingNeededRef = useRef(false);
    const pcRecoveryPollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const pcRecoveryPollingGenerationRef = useRef(0);
    const startPcRecoveryPollingRef = useRef<(
        session: any,
        scope: 'mine' | 'all',
    ) => void>(() => undefined);
    type BufferedPcSocketEvent = { data: any; consume: (data: any) => void };
    const pcResumeEventGateRef = useRef<ResumeEventGate<BufferedPcSocketEvent> | null>(null);
    const pcResumeGenerationRef = useRef(0);
    const pcResumeReconcilePromiseRef = useRef<Promise<void> | null>(null);
    const ensureSessionSocketRef = useRef<(
        session: any,
        agentId: string,
        authToken: string,
    ) => WebSocket | undefined>(() => undefined);
    const pcResumeHadActiveTurnRef = useRef(false);
    const cancelPcAutoFollowRef = useRef<() => void>(() => undefined);
    const [pcPageActive, setPcPageActive] = useState(() => !document.hidden);
    const [pcResumeMeasurementKey, setPcResumeMeasurementKey] = useState<number | null>(null);

    const buildSessionRuntimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;

    const cancelPcRecoveryPolling = () => {
        pcRecoveryPollingGenerationRef.current += 1;
        if (pcRecoveryPollTimerRef.current) clearTimeout(pcRecoveryPollTimerRef.current);
        pcRecoveryPollTimerRef.current = null;
    };

    const clearReconnectTimer = (key: SessionRuntimeKey) => {
        const timer = reconnectTimerRef.current[key];
        if (timer) {
            clearTimeout(timer);
            reconnectTimerRef.current[key] = null;
        }
    };

    const beginPcResumeEventGate = (runtimeKey: SessionRuntimeKey) => {
        const activeGate = pcResumeEventGateRef.current;
        const nextGeneration = pcResumeGenerationRef.current + 1;
        const { gate, displacedEvents } = createOrReuseResumeEventGate<BufferedPcSocketEvent>(
            activeGate,
            runtimeKey,
            nextGeneration,
        );
        if (gate !== activeGate) pcResumeGenerationRef.current = nextGeneration;
        pcResumeEventGateRef.current = gate;
        displacedEvents.forEach(({ data, consume }) => consume(data));
        return gate;
    };

    const finishPcResumeEventGate = (
        gate: ResumeEventGate<BufferedPcSocketEvent>,
        replay: boolean,
    ) => {
        if (pcResumeEventGateRef.current !== gate) return;
        pcResumeEventGateRef.current = null;
        const events = drainResumeEventGate(gate);
        if (!replay) return;
        events.forEach(({ data, consume }) => consume(data));
    };

    const releasePcResumeReconcileOwner = (
        owner: Promise<void>,
        session: any,
        ownerAgentId: string,
        ownerToken: string,
    ) => {
        if (pcResumeReconcilePromiseRef.current !== owner) return;
        pcResumeReconcilePromiseRef.current = null;
        const runtimeKey = buildSessionRuntimeKey(ownerAgentId, String(session.id));
        if (
            String(currentAgentIdRef.current || '') !== ownerAgentId
            || String(activeSessionIdRef.current || '') !== String(session.id)
            || !shouldScheduleResumeReconnect({
                pageSuspended: pcPageSuspendedRef.current,
                unmounted: false,
                hidden: document.hidden,
                socketReadyState: wsMapRef.current[runtimeKey]?.readyState,
            })
        ) return;
        reconnectDisabledRef.current[runtimeKey] = false;
        ensureSessionSocketRef.current(session, ownerAgentId, ownerToken);
    };

    const closeSessionSocket = (key: SessionRuntimeKey, disableReconnect = true) => {
        if (disableReconnect) reconnectDisabledRef.current[key] = true;
        clearReconnectTimer(key);
        reconnectAttemptsRef.current[key] = 0;
        const ws = wsMapRef.current[key];
        if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
        delete wsMapRef.current[key];
        delete sessionUiStateRef.current[key];
        delete sessionTurnRuntimeRef.current[key];
    };

    const setSessionUiState = (key: SessionRuntimeKey, next: Partial<{ isWaiting: boolean; isStreaming: boolean; isStopping: boolean }>) => {
        const prev = sessionUiStateRef.current[key] || { isWaiting: false, isStreaming: false, isStopping: false };
        sessionUiStateRef.current[key] = { ...prev, ...next };
    };

    /** Normalize IDs — API/JSON may use number vs string; loose equality was breaking "own session" detection. */
    const sessionUserIdStr = (s: any) => (s?.user_id == null ? '' : String(s.user_id));
    const viewerUserIdStr = () => (currentUser?.id == null ? '' : String(currentUser.id));
    const isAgentChatSession = (s: any) =>
        String(s?.source_channel || '').toLowerCase() === 'agent' ||
        String(s?.participant_type || '').toLowerCase() === 'agent';

    /** Ensure session shape from POST/list so P2P "mine" is never mistaken for read-only or agent thread. */
    const normalizeChatSession = (sess: any) => {
        if (!sess || typeof sess !== 'object') return sess;
        const vu = viewerUserIdStr();
        const rawUid =
            sess.user_id != null && String(sess.user_id).trim() !== '' ? String(sess.user_id) : vu;
        return {
            ...sess,
            id: String(sess.id),
            agent_id: sess.agent_id != null ? String(sess.agent_id) : sess.agent_id,
            user_id: rawUid,
            unread_count: Number(sess.unread_count || 0),
            is_primary: Boolean(sess.is_primary),
            source_channel:
                typeof sess.source_channel === 'string' && sess.source_channel.trim()
                    ? sess.source_channel
                    : 'web',
            participant_type:
                typeof sess.participant_type === 'string' && sess.participant_type.trim()
                    ? sess.participant_type
                    : 'user',
            is_group: Boolean(sess.is_group),
        };
    };

    const clearUnreadForSession = (sessionId?: string | null) => {
        if (!sessionId) return;
        const sid = String(sessionId);
        setSessions(prev => prev.map((item: any) => String(item.id) === sid ? { ...item, unread_count: 0 } : item));
        setAllSessions(prev => prev.map((item: any) => String(item.id) === sid ? { ...item, unread_count: 0 } : item));
        setActiveSession((prev: any) => prev && String(prev.id) === sid ? { ...prev, unread_count: 0 } : prev);
    };

    const isWritableSession = (sess: any, scopeOverride: 'mine' | 'all' = chatScope) => {
        if (!sess) return false;
        const sc = String(sess.source_channel || 'web').toLowerCase();
        const pt = String(sess.participant_type || 'user').toLowerCase();
        if (sc === 'agent' || sc === 'subagent' || pt === 'agent') return false;
        if (sess.is_group) return false;
        if (scopeOverride === 'all') return false;
        const su = sessionUserIdStr(sess);
        const vu = viewerUserIdStr();
        if (su && vu && su !== vu) return false;
        return true;
    };

    const isViewingOtherUsersSessions = canViewAllAgentChatSessions && chatScope === 'all';

    // Filtering out the viewer's own rows happens in SQL before offset/limit,
    // keeping every incremental page dense and stable.
    const othersListForPicker = allSessions;

    useEffect(() => {
        if (!canViewAllAgentChatSessions && chatScope === 'all') setChatScope('mine');
    }, [canViewAllAgentChatSessions, chatScope]);

    useEffect(() => {
        if (!scopeDropdownOpen) return;
        const handler = (e: MouseEvent) => {
            if (scopeDropdownRef.current && !scopeDropdownRef.current.contains(e.target as Node)) setScopeDropdownOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [scopeDropdownOpen]);

    const clearChatSelection = () => {
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setHistoryMsgs([]);
        setWsConnected(false);
        setIsStreaming(false);
        setIsWaiting(false);
        setIsStopping(false);
        skipNextSessionUrlRestoreRef.current = true;
        if (!writeSessionIdToUrl(null)) skipNextSessionUrlRestoreRef.current = false;
    };

    const onAdminTabMine = () => {
        setChatScope('mine');
        if (activeSession && sessionUserIdStr(activeSession) !== viewerUserIdStr()) clearChatSelection();
    };

    const onAdminTabOthers = () => {
        setChatScope('all');
        if (allSessions.length === 0) void fetchAllSessions();
        if (activeSession && sessionUserIdStr(activeSession) === viewerUserIdStr()) clearChatSelection();
    };
    const syncActiveSocketState = (sess: any | null = activeSession, agentId: string | undefined = id) => {
        if (!sess || !agentId) {
            wsRef.current = null;
            setWsConnected(false);
            return;
        }
        const key = buildSessionRuntimeKey(agentId, sess.id);
        const ws = wsMapRef.current[key];
        wsRef.current = ws ?? null;
        // CONNECTING 是中间态：不要把 wsConnected 打回 false。否则在建连窗口里，任何无关
        // 状态(权限/scope 异步加载完成等)触发的重新同步，都会让输入框反复闪回 "Connecting…"。
        // OPEN→已连接；无 ws / 已关闭→未连接；CONNECTING→保持当前显示，交给 onopen/onclose
        // 事件驱动收敛。
        if (ws && ws.readyState === WebSocket.OPEN && (ws as any)._serverConnected === true) {
            setWsConnected(true);
            setOnboardingKickoffRequest(onboardingRequestsRef.current[key] || null);
        } else if (!ws || ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED) {
            setWsConnected(false);
            setOnboardingKickoffRequest(null);
        }
    };

    const fetchMySessions = async (
        silent = false,
        agentId: string | undefined = id,
        append = false,
    ) => {
        if (!agentId) return [];
        const existingCount = currentAgentIdRef.current === agentId ? sessions.length : 0;
        const refreshLimit = silent && !append
            ? Math.min(200, Math.max(SESSION_PAGE_SIZE, existingCount))
            : SESSION_PAGE_SIZE;
        const requestCursor = append ? sessionsNextCursor : null;
        if (append && requestCursor == null) return [];
        const generation = append
            ? sessionsListGenerationRef.current
            : ++sessionsListGenerationRef.current;
        sessionsListAbortRef.current?.abort();
        const controller = new AbortController();
        sessionsListAbortRef.current = controller;
        if (append) setSessionsLoadingMore(true);
        else {
            // A reset/refresh supersedes any append request. Clear its spinner
            // immediately because the aborted append's finally block is no
            // longer allowed to mutate state for the new request generation.
            setSessionsLoadingMore(false);
            if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(true);
        }
        try {
            const page = await chatSessionApi.listPage(agentId, {
                scope: 'mine',
                limit: refreshLimit,
                cursor: requestCursor || undefined,
                signal: controller.signal,
            });
            const data = page.items.map((row: any) => normalizeChatSession(row));
            if (
                currentAgentIdRef.current === agentId
                && generation === sessionsListGenerationRef.current
            ) {
                if (append) {
                    setSessions(prev => mergeSessionsById(prev, data));
                    setSessionsHasMore(page.has_more);
                    setSessionsNextCursor(page.next_cursor);
                } else if (silent && existingCount > 200) {
                    setSessions(prev => mergeSessionsById(data, prev));
                } else {
                    setSessions(data);
                    setSessionsHasMore(page.has_more);
                    setSessionsNextCursor(page.next_cursor);
                }
            }
            return data;
        } catch (error: any) {
            if (error?.name !== 'AbortError') console.warn('[chat] failed to load session list', error);
            return [];
        } finally {
            if (sessionsListAbortRef.current === controller) {
                sessionsListAbortRef.current = null;
                if (append) setSessionsLoadingMore(false);
                else if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(false);
            }
        }
    };

    const fetchAllSessions = async (silent = false, append = false, agentId: string | undefined = id) => {
        if (!agentId || !canViewAllAgentChatSessions) return [];
        const existingCount = currentAgentIdRef.current === agentId ? allSessions.length : 0;
        const refreshLimit = silent && !append
            ? Math.min(200, Math.max(SESSION_PAGE_SIZE, existingCount))
            : SESSION_PAGE_SIZE;
        const requestCursor = append ? allSessionsNextCursor : null;
        if (append && requestCursor == null) return [];
        const generation = append
            ? allSessionsListGenerationRef.current
            : ++allSessionsListGenerationRef.current;
        allSessionsListAbortRef.current?.abort();
        const controller = new AbortController();
        allSessionsListAbortRef.current = controller;
        if (append) setAllSessionsLoadingMore(true);
        else {
            // See fetchMySessions: a newer reset owns the loading state after
            // aborting an in-flight append.
            setAllSessionsLoadingMore(false);
            if (!silent) setAllSessionsLoading(true);
        }
        try {
            const page = await chatSessionApi.listPage(agentId, {
                scope: 'all',
                exclude_mine: true,
                limit: refreshLimit,
                cursor: requestCursor || undefined,
                signal: controller.signal,
            });
            if (
                currentAgentIdRef.current !== agentId
                || generation !== allSessionsListGenerationRef.current
            ) return [];
            const data = page.items.map((row: any) => normalizeChatSession(row));
            if (append) {
                setAllSessions(prev => mergeSessionsById(prev, data));
                setAllSessionsHasMore(page.has_more);
                setAllSessionsNextCursor(page.next_cursor);
            } else if (silent && existingCount > 200) {
                setAllSessions(prev => mergeSessionsById(data, prev));
            } else {
                setAllSessions(data);
                setAllSessionsHasMore(page.has_more);
                setAllSessionsNextCursor(page.next_cursor);
            }
            return data;
        } catch (error: any) {
            if (currentAgentIdRef.current === agentId && !silent && !append && error?.name !== 'AbortError') {
                setAllSessions([]);
                setAllSessionsHasMore(false);
                setAllSessionsNextCursor(null);
                if (error?.status === 403) {
                    console.warn('[chat] scope=all sessions forbidden (need org/platform/agent admin)');
                }
            }
            return [];
        } finally {
            if (allSessionsListAbortRef.current === controller) {
                allSessionsListAbortRef.current = null;
                if (append) setAllSessionsLoadingMore(false);
                else if (!silent) setAllSessionsLoading(false);
            }
        }
    };

    const selectSession = async (
        rawSess: any,
        scopeOverride: 'mine' | 'all' = chatScope,
        options: { preserveLoadedHistory?: boolean; prepareWebResume?: boolean } = {},
    ) => {
        const sess = normalizeChatSession(rawSess);
        const targetAgentId = id;
        if (!targetAgentId) return false;
        const preserveLoadedHistory = Boolean(
            options.preserveLoadedHistory
            && String(activeSessionIdRef.current || '') === String(sess.id),
        );
        if (!preserveLoadedHistory) {
            pcRecoveryPollingNeededRef.current = false;
            cancelPcRecoveryPolling();
        }
        discardChatStreamBatch();
        discardMonitorStreamBatch();
        const runtimeKey = buildSessionRuntimeKey(targetAgentId, String(sess.id));
        const runtimeState = sessionUiStateRef.current[runtimeKey] || { isWaiting: false, isStreaming: false, isStopping: false };
        const writable = isWritableSession(sess, scopeOverride);
        activeSessionIdRef.current = sess.id;
        if (!preserveLoadedHistory) {
            setChatMessages([]);
            setHistoryMsgs([]);
            setHistoryOldestTs(null);
            setHistoryHasMore(true);
            historyAutoLoadCursorRef.current = null;
        }
        setHistoryLoadingMore(false);
        setIsStreaming(runtimeState.isStreaming);
        setIsWaiting(runtimeState.isWaiting);
        setIsStopping(runtimeState.isStopping);
        setActiveSession(sess);
        writeSessionIdToUrl(String(sess.id));
        setAgentExpired(false);
        syncActiveSocketState(sess, targetAgentId);
        if (writable) scheduleComposerFocus();

        // Abort any pending message load and increment sequence
        sessionMsgAbortRef.current?.abort();
        historyMoreAbortRef.current?.abort();
        historyMoreAbortRef.current = null;
        const controller = new AbortController();
        sessionMsgAbortRef.current = controller;
        const historyTimeout = window.setTimeout(() => controller.abort(), 10000);
        const loadSeq = ++sessionLoadSeqRef.current;
        try {
            const tkn = localStorage.getItem('token');
            const parseHistoryRows = (rows: any[]) => rows.map((m: any) => parseChatMsg({
                role: m.role, content: m.content || '',
                ...(Object.prototype.hasOwnProperty.call(m, 'display_content') && { display_content: m.display_content || '' }),
                ...(Object.prototype.hasOwnProperty.call(m, 'attachments') && { attachments: m.attachments || [] }),
                ...(m.quoted_message && { quoted_message: m.quoted_message }),
                ...(m.toolName && { toolName: m.toolName, toolArgs: m.toolArgs, toolStatus: m.toolStatus, toolResult: m.toolResult, toolThinking: m.toolThinking }),
                ...(m.toolCallId && { toolCallId: m.toolCallId }),
                ...(typeof m.toolCallIdExplicit === 'boolean' && { _toolCallIdExplicit: m.toolCallIdExplicit }),
                ...((m.turnAnchorId || m.message_meta?.turn_anchor_id) && { turnAnchorId: m.turnAnchorId || m.message_meta?.turn_anchor_id }),
                ...((m.turnGeneration ?? m.message_meta?.turn_generation) != null && { turnGeneration: m.turnGeneration ?? m.message_meta?.turn_generation }),
                ...((m.producerScope || m.producer_scope || m.message_meta?.producer_scope) && { producerScope: m.producerScope || m.producer_scope || m.message_meta?.producer_scope }),
                ...(m.thinking && { thinking: m.thinking }),
                ...(m.created_at && { timestamp: m.created_at }),
                ...(m.id && { id: m.id }),
                // Group-chat per-message attribution (Phase 2 #1): when the
                // backend resolved a real sender, surface it through the
                // shape so the renderer can label the bubble. parseChatMsg
                // happens to drop unknown keys, so we need to pre-pack them.
                ...(m.sender_name && { sender_name: m.sender_name }),
                ...(m.sender_user_id && { sender_user_id: m.sender_user_id }),
                ...(m.sender_agent_id && { sender_agent_id: m.sender_agent_id }),
            }));
            const currentLoadedMessages = writable
                ? chatMessagesSnapshotRef.current
                : historyMsgsSnapshotRef.current;
            let collectedRows: any[] = [];
            let responseCursor: string | null = null;
            let responseHasMore: string | null = null;
            let overlapFound = !preserveLoadedHistory || currentLoadedMessages.length === 0;
            let before: string | null = null;

            do {
                const params = createConversationHistoryPageParams(before);
                const res = await fetch(`/api/agents/${targetAgentId}/sessions/${sess.id}/message-turns?${params}`, {
                    headers: { Authorization: `Bearer ${tkn}` },
                    signal: controller.signal,
                });
                if (!res.ok) return false;
                const pageCursor = res.headers.get('X-Message-Next-Cursor');
                const pageHasMore = res.headers.get('X-Message-Has-More');
                const pageRows = await res.json();
                if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return false;
                if (currentAgentIdRef.current !== targetAgentId) return false;
                if (activeSessionIdRef.current !== sess.id) return false;
                const safePageRows = Array.isArray(pageRows) ? pageRows : [];
                collectedRows = [...safePageRows, ...collectedRows];
                responseCursor = pageCursor;
                responseHasMore = pageHasMore;
                if (preserveLoadedHistory && safePageRows.length > 0) {
                    overlapFound = latestHistoryWindowOverlaps(
                        currentLoadedMessages as any,
                        parseHistoryRows(safePageRows) as any,
                    );
                }
                const hasMore = resolveConversationHistoryHasMore(pageHasMore);
                if (overlapFound || !hasMore || !pageCursor || pageCursor === before) break;
                before = pageCursor;
            } while (true);

            const preParsed = parseHistoryRows(collectedRows);
            if (!preserveLoadedHistory || !overlapFound) {
                setHistoryHasMore(resolveConversationHistoryHasMore(responseHasMore));
                // Backend returns the page oldest-first. Pagination metadata is
                // based on raw DB rows and remains valid even when rendering
                // merges or splits tool messages.
                setHistoryOldestTs(responseCursor || (collectedRows.length && collectedRows[0].created_at
                    ? `${collectedRows[0].created_at}${collectedRows[0].id ? `|${collectedRows[0].id}` : ''}`
                    : null));
            }

            if (writable) {
                setChatMessages((prev) => preserveLoadedHistory
                    ? reconcileLatestHistoryWindow(
                        (options.prepareWebResume ? prepareMessagesForActiveTurnResume(prev) : prev) as any,
                        preParsed as any,
                    ) as ChatMsg[]
                    : preParsed);
            } else {
                setHistoryMsgs((prev) => preserveLoadedHistory
                    ? reconcileLatestHistoryWindow(
                        options.prepareWebResume ? prepareMessagesForActiveTurnResume(prev) : prev,
                        preParsed as any,
                    ) as any
                    : preParsed);
            }
            // The backend marks the session as read when the current user opens it. Mirror that
            // immediately in local state so unread badges clear without waiting for the next poll.
            clearUnreadForSession(String(sess.id));
            queryClient.invalidateQueries({ queryKey: ['agents'] });
            return true;
        } catch (err: any) {
            if (err?.name === 'AbortError') return false;
            console.error('Failed to load session messages:', err);
            return false;
        } finally {
            window.clearTimeout(historyTimeout);
            if (sessionMsgAbortRef.current === controller) sessionMsgAbortRef.current = null;
        }
    };

    const createNewSession = async () => {
        if (!id) return;
        try {
            const tkn = localStorage.getItem('token');
            const res = await fetch(`/api/agents/${id}/sessions`, {
                method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tkn}` },
                body: JSON.stringify({}),
            });
            if (res.ok) {
                const newSess = normalizeChatSession(await res.json());
                setChatScope('mine');
                setSessions((prev) => [
                    newSess,
                    ...prev.map((session) => (
                        String(session.id) !== String(newSess.id)
                        && sessionUserIdStr(session) === sessionUserIdStr(newSess)
                        && session.source_channel === newSess.source_channel
                            ? { ...session, is_primary: false }
                            : session
                    )),
                ]);
                setIsStreaming(false);
                setIsWaiting(false);
                setIsStopping(false);
                await selectSession(newSess, 'mine');
            } else {
                const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
                console.error('Failed to create session:', err);
                toast.error('创建会话失败', { details: String(err.detail || `HTTP ${res.status}`) });
            }
        } catch (err: any) {
            console.error('Failed to create session:', err);
            toast.error('创建会话失败', { details: String(err.message || err) });
        }
    };

    const deleteSession = async (sessionId: string) => {
        const ok = await dialog.confirm(
            t('chat.deleteConfirm', 'Delete this session and all its messages? This cannot be undone.'),
            { title: '删除会话', danger: true, confirmLabel: '删除' },
        );
        if (!ok) return;
        const tkn = localStorage.getItem('token');
        try {
            await fetch(`/api/agents/${id}/sessions/${sessionId}`, { method: 'DELETE', headers: { Authorization: `Bearer ${tkn}` } });
            if (id) closeSessionSocket(buildSessionRuntimeKey(id, sessionId), true);
            // If deleted the active session, clear it
            const deletedActiveSession = String(activeSession?.id || '') === String(sessionId);
            if (deletedActiveSession) clearChatSelection();
            const remainingMine = await fetchMySessions(false, id);
            if (canViewAllAgentChatSessions) await fetchAllSessions();
            if (deletedActiveSession && remainingMine.length > 0) {
                setChatScope('mine');
                await selectSession(remainingMine[0], 'mine');
            }
        } catch (e: any) {
            toast.error('删除失败', { details: String(e?.message || e) });
        }
    };

    // Expiry editor modal state
    const [showExpiryModal, setShowExpiryModal] = useState(false);
    const [expiryValue, setExpiryValue] = useState('');       // datetime-local string or ''
    const [expiryQuickHours, setExpiryQuickHours] = useState<number | null>(null);
    const [expirySaving, setExpirySaving] = useState(false);

    const openExpiryModal = () => {
        const cur = (agent as any)?.expires_at;
        // Convert ISO to datetime-local format (YYYY-MM-DDTHH:MM)
        setExpiryValue(cur ? new Date(cur).toISOString().slice(0, 16) : '');
        setExpiryQuickHours(null);
        setShowExpiryModal(true);
    };

    const addHours = (h: number) => {
        const base = (agent as any)?.expires_at ? new Date((agent as any).expires_at) : new Date();
        const next = new Date(base.getTime() + h * 3600_000);
        setExpiryValue(next.toISOString().slice(0, 16));
        setExpiryQuickHours(h);
    };

    const saveExpiry = async (permanent = false) => {
        setExpirySaving(true);
        try {
            const token = localStorage.getItem('token');
            const body = permanent ? { expires_at: null } : { expires_at: expiryValue ? new Date(expiryValue).toISOString() : null };
            await fetch(`/api/agents/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                body: JSON.stringify(body),
            });
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            setShowExpiryModal(false);
        } catch (e: any) { toast.error('保存失败', { details: String(e?.message || e) }); }
        setExpirySaving(false);
    };
    interface ChatMsg { role: 'user' | 'assistant' | 'tool_call'; content: string; display_content?: string; attachments?: ChatMessageAttachment[]; quoted_message?: ChatQuotedMessage; id?: string; fileName?: string; toolName?: string; toolCallId?: string; toolArgs?: any; toolStatus?: 'running' | 'done'; toolResult?: string; toolThinking?: string; thinking?: string; streaming?: boolean; _streaming?: boolean; imageUrl?: string; previewImages?: ChatPreviewImage[]; timestamp?: string; sender_name?: string; sender_user_id?: string; sender_agent_id?: string; turnAnchorId?: string; turnGeneration?: number; producerScope?: string; confirmationToolCalls?: ChatMsg[]; }
    const [chatMessages, setChatMessages] = useState<ChatMsg[]>([]);
    const chatMessagesSnapshotRef = useRef<ChatMsg[]>(chatMessages);
    const historyMsgsSnapshotRef = useRef<any[]>(historyMsgs);
    chatMessagesSnapshotRef.current = chatMessages;
    historyMsgsSnapshotRef.current = historyMsgs;
    const chatStreamBatchRef = useRef<Array<{
        type: 'thinking' | 'chunk';
        content: string;
        messageId?: string;
        turnAnchorId?: string;
        turnGeneration?: number;
        producerScope?: string;
    }>>([]);
    const chatStreamBatchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const flushChatStreamBatch = useCallback(() => {
        if (chatStreamBatchTimerRef.current) {
            clearTimeout(chatStreamBatchTimerRef.current);
            chatStreamBatchTimerRef.current = null;
        }
        const batch = chatStreamBatchRef.current;
        if (batch.length === 0) return;
        chatStreamBatchRef.current = [];
        setChatMessages((prev) => batch.reduce(
            (next, event) => foldConversationTimelineEvent(next as any, {
                type: event.type,
                content: event.content,
                message_id: event.messageId,
                producer_scope: event.producerScope,
                turn: event.turnAnchorId ? {
                    turn_anchor_id: event.turnAnchorId,
                    generation: event.turnGeneration,
                } : undefined,
            }, { makeId: createClientId }).messages as ChatMsg[],
            prev,
        ));
    }, []);
    const discardChatStreamBatch = useCallback(() => {
        if (chatStreamBatchTimerRef.current) clearTimeout(chatStreamBatchTimerRef.current);
        chatStreamBatchTimerRef.current = null;
        chatStreamBatchRef.current = [];
    }, []);
    const enqueueChatStreamEvent = useCallback((event: {
        type: 'thinking' | 'chunk';
        content: string;
        messageId?: string;
        turnAnchorId?: string;
        turnGeneration?: number;
        producerScope?: string;
    }) => {
        const last = chatStreamBatchRef.current[chatStreamBatchRef.current.length - 1];
        if (last && last.type === event.type && last.messageId === event.messageId
            && last.turnAnchorId === event.turnAnchorId
            && last.producerScope === event.producerScope) {
            last.content += event.content;
        } else {
            chatStreamBatchRef.current.push({ ...event });
        }
        if (!chatStreamBatchTimerRef.current) {
            chatStreamBatchTimerRef.current = setTimeout(flushChatStreamBatch, 40);
        }
    }, [flushChatStreamBatch]);
    const confirmationPending = chatMessages.some(isPendingConfirmationToolCall);
    const upsertToolCallMessage = (toolMsg: ChatMsg) => {
        setChatMessages(prev => mergeToolCallMessage(prev as any, toolMsg as any) as ChatMsg[]);
    };
    // Transient info banner (e.g. fallback model switch notification)
    const [chatInfoMsg, setChatInfoMsg] = useState<string | null>(null);
    const chatInfoTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [liveState, setLiveState] = useState<LivePreviewState>({});
    const [workspaceActivePath, setWorkspaceActivePath] = useState<string | null>(null);
    const [workspaceLockedPath, setWorkspaceLockedPath] = useState<string | null>(null);
    const [workspaceActivities, setWorkspaceActivities] = useState<WorkspaceActivity[]>([]);
    const [workspaceLiveDraft, setWorkspaceLiveDraft] = useState<WorkspaceLiveDraft | null>(null);
    const workspaceEditingRef = useRef(false);
    const workspaceLockedPathRef = useRef<string | null>(null);
    const [wsSessionId, setWsSessionId] = useState<string>('');
    const [sessionListCollapsed, setSessionListCollapsed] = useState(false);
    const livePanelAutoCollapsedRef = useRef(false);
    const [chatInput, setChatInput] = useState('');
    const [wsConnected, setWsConnected] = useState(false);
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    const [isStopping, setIsStopping] = useState(false);
    const [chatUploadDrafts, setChatUploadDrafts] = useState<{ id: string; name: string; percent: number; previewUrl?: string; sizeBytes: number }[]>([]);
    const chatUploadAbortRef = useRef<Map<string, () => void>>(new Map());
    type PendingChatMessage = {
        runtimeKey: SessionRuntimeKey;
        contentForLLM: string;
        displayContent: string;
        fileName: string;
        imageUrl?: string;
        previewImages: ChatPreviewImage[];
        attachments: ChatMessageAttachment[];
        modelId?: string | null;
        messageId: string;
    };
    const [attachedFiles, setAttachedFiles] = useState<ChatAttachedFile[]>([]);
    const attachedImagePreviews = useMemo(() => buildPreviewImagesFromAttachments(attachedFiles), [attachedFiles]);
    const [chatImagePreview, setChatImagePreview] = useState<{ images: ChatPreviewImage[]; index: number } | null>(null);
    const [unavailableAttachmentKeys, setUnavailableAttachmentKeys] = useState<Set<string>>(() => new Set());
    const dismissedWorkspaceRefPath = useRef<string | null>(null);
    const pendingChatSendRef = useRef<PendingChatMessage | null>(null);
    const wsRef = useRef<WebSocket | null>(null);

    const chatContainerRef = useRef<HTMLDivElement>(null);
    const chatInputRef = useRef<HTMLTextAreaElement>(null);
    const chatInputAreaRef = useRef<HTMLDivElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        setUnavailableAttachmentKeys(new Set());
    }, [activeSession?.id]);

    const markAttachmentUnavailable = useCallback((key: string) => {
        setUnavailableAttachmentKeys((current) => {
            if (current.has(key)) return current;
            const next = new Set(current);
            next.add(key);
            return next;
        });
    }, []);

    const handleAttachmentDownload = useCallback(async (path: string, name: string) => {
        if (!id) return;
        try {
            await downloadChatAttachment(fileApi.downloadUrl(id, path), name);
        } catch {
            markAttachmentUnavailable(path);
        }
    }, [id, markAttachmentUnavailable]);

    const workspacePreviewLocked = !!workspaceLockedPath;
    useEffect(() => {
        workspaceLockedPathRef.current = workspaceLockedPath;
    }, [workspaceLockedPath]);
    const allowWorkspaceAutoSwitch = useCallback((path?: string | null) => {
        if (!path) return false;
        if (workspaceEditingRef.current) return false;
        if (!workspaceLockedPathRef.current) return true;
        return workspaceLockedPathRef.current === path;
    }, []);
    const allowLivePanelAutoFocus = useCallback(() => {
        return !workspaceEditingRef.current && !workspaceLockedPathRef.current;
    }, []);
    const handleWorkspaceSelectPath = useCallback((path: string) => {
        setWorkspaceActivePath(path);
        if (workspaceLockedPath) setWorkspaceLockedPath(path);
    }, [workspaceLockedPath]);
    const handleWorkspaceToggleLock = useCallback(() => {
        setWorkspaceLockedPath((current) => current ? null : workspaceActivePath);
    }, [workspaceActivePath]);
    const handleWorkspaceEditingChange = useCallback((editing: boolean) => {
        workspaceEditingRef.current = editing;
    }, []);
    const collapseSidebarsForLivePanel = useCallback(() => {
        if (livePanelAutoCollapsedRef.current) return;
        livePanelAutoCollapsedRef.current = true;
        setSessionListCollapsed(true);
        useAppStore.setState({ sidebarCollapsed: true });
    }, []);
    useEffect(() => {
        if (!livePanelVisible) {
            livePanelAutoCollapsedRef.current = false;
        }
    }, [livePanelVisible]);
    const togglePreviewPanel = useCallback((tab: SidePanelTab) => {
        setLivePanelVisible((visible) => {
            if (visible && sidePanelTab === tab) {
                livePanelAutoCollapsedRef.current = false;
                return false;
            }
            setSidePanelTab(tab);
            collapseSidebarsForLivePanel();
            return true;
        });
    }, [collapseSidebarsForLivePanel, sidePanelTab]);

    const openAwarePanel = useCallback(() => {
        if (!allowLivePanelAutoFocus()) return;
        setSidePanelTab('aware');
        setLivePanelVisible(true);
        collapseSidebarsForLivePanel();
    }, [allowLivePanelAutoFocus, collapseSidebarsForLivePanel]);

    // Settings form local state
    const [settingsForm, setSettingsForm] = useState({
        primary_model_id: '',
        fallback_model_id: '',
        context_window_size: 100,
        max_tool_rounds: 50,
        max_tokens_per_day: '' as string | number,
        max_tokens_per_month: '' as string | number,
        max_triggers: 20,
        min_poll_interval_min: 5,
        webhook_rate_limit: 5,
        im_thinking_output_enabled: false,
    });
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsSaved, setSettingsSaved] = useState(false);
    const [settingsError, setSettingsError] = useState('');
    const settingsInitRef = useRef(false);

    // Sync settings form from server data on load
    useEffect(() => {
        if (agent && !settingsInitRef.current) {
            setSettingsForm({
                primary_model_id: agent.primary_model_id || '',
                fallback_model_id: agent.fallback_model_id || '',
                context_window_size: agent.context_window_size ?? 100,
                max_tool_rounds: (agent as any).max_tool_rounds ?? 50,
                max_tokens_per_day: agent.max_tokens_per_day || '',
                max_tokens_per_month: agent.max_tokens_per_month || '',
                max_triggers: (agent as any).max_triggers ?? 20,
                min_poll_interval_min: (agent as any).min_poll_interval_min ?? 5,
                webhook_rate_limit: (agent as any).webhook_rate_limit ?? 5,
                im_thinking_output_enabled: (agent as any).im_thinking_output_enabled ?? false,
            });
            settingsInitRef.current = true;
        }
    }, [agent]);

    // Welcome message editor state (must be at top level -- not inside IIFE)
    const [wmDraft, setWmDraft] = useState('');
    const [wmSaved, setWmSaved] = useState(false);
    useEffect(() => { setWmDraft((agent as any)?.welcome_message || ''); }, [(agent as any)?.welcome_message]);

    const hasSettingsChanges = (
        settingsForm.primary_model_id !== (agent?.primary_model_id || '') ||
        settingsForm.fallback_model_id !== (agent?.fallback_model_id || '') ||
        settingsForm.context_window_size !== (agent?.context_window_size ?? 100) ||
        settingsForm.max_tool_rounds !== ((agent as any)?.max_tool_rounds ?? 50) ||
        String(settingsForm.max_tokens_per_day) !== String(agent?.max_tokens_per_day || '') ||
        String(settingsForm.max_tokens_per_month) !== String(agent?.max_tokens_per_month || '') ||
        settingsForm.max_triggers !== ((agent as any)?.max_triggers ?? 20) ||
        settingsForm.min_poll_interval_min !== ((agent as any)?.min_poll_interval_min ?? 5) ||
        settingsForm.webhook_rate_limit !== ((agent as any)?.webhook_rate_limit ?? 5) ||
        settingsForm.im_thinking_output_enabled !== ((agent as any)?.im_thinking_output_enabled ?? false)
    );

    const handleSaveSettings = async () => {
        setSettingsSaving(true);
        setSettingsError('');
        try {
            const result: any = await agentApi.update(id!, {
                primary_model_id: settingsForm.primary_model_id || null,
                fallback_model_id: settingsForm.fallback_model_id || null,
                context_window_size: settingsForm.context_window_size,
                max_tool_rounds: settingsForm.max_tool_rounds,
                max_tokens_per_day: settingsForm.max_tokens_per_day ? Number(settingsForm.max_tokens_per_day) : null,
                max_tokens_per_month: settingsForm.max_tokens_per_month ? Number(settingsForm.max_tokens_per_month) : null,
                max_triggers: settingsForm.max_triggers,
                min_poll_interval_min: settingsForm.min_poll_interval_min,
                webhook_rate_limit: settingsForm.webhook_rate_limit,
                im_thinking_output_enabled: settingsForm.im_thinking_output_enabled,
            } as any);
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            settingsInitRef.current = false;
            const clamped = result?._clamped_fields;
            if (clamped && clamped.length > 0) {
                const isCh = i18n.language?.startsWith('zh');
                const fieldNames: Record<string, string> = isCh
                    ? { min_poll_interval_min: 'Poll 最短间隔', webhook_rate_limit: 'Webhook 频率限制', heartbeat_interval_minutes: '心跳间隔' }
                    : { min_poll_interval_min: 'Min Poll Interval', webhook_rate_limit: 'Webhook Rate Limit', heartbeat_interval_minutes: 'Heartbeat Interval' };
                const msgs = clamped.map((c: any) => {
                    const name = fieldNames[c.field] || c.field;
                    return isCh
                        ? `${name}: ${c.requested} -> ${c.applied} (公司策略限制)`
                        : `${name}: ${c.requested} -> ${c.applied} (company policy)`;
                });
                setSettingsError((isCh ? 'Some values were adjusted:\n' : 'Some values were adjusted:\n') + msgs.join('\n'));
                setTimeout(() => setSettingsError(''), 5000);
            }
            setSettingsSaved(true);
            setTimeout(() => setSettingsSaved(false), 2000);
        } catch (e: any) {
            setSettingsError(e?.message || 'Failed to save');
        } finally {
            setSettingsSaving(false);
        }
    };

    const handleSaveWelcomeMessage = async () => {
        try {
            await agentApi.update(id!, { welcome_message: wmDraft } as any);
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            setWmSaved(true);
            setTimeout(() => setWmSaved(false), 2000);
        } catch {
            // Keep current editor state when welcome message save fails.
        }
    };

    // Reset cached state when switching to a different agent
    const prevIdRef = useRef(id);
    useEffect(() => {
        if (id && id !== prevIdRef.current) {
            prevIdRef.current = id;
            settingsInitRef.current = false;
            setSettingsSaved(false);
            setSettingsError('');
            setWmDraft('');
            setWmSaved(false);
            // Invalidate all queries for the old agent to force fresh data
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            if (location.pathname.endsWith('/settings')) {
                window.history.replaceState(null, '', `#${activeTab}`);
            }
        }
    }, [id]);

    // Load chat history + connect websocket when chat tab is active
    const parseChatMsg = (msg: ChatMsg): ChatMsg => {
        const hasStructuredAttachments = Object.prototype.hasOwnProperty.call(msg, 'attachments');
        if (msg.role !== 'user' && !(msg.role === 'assistant' && hasStructuredAttachments)) return msg;
        if (!id) return msg;
        const normalized = normalizeChatAttachmentFields({
            raw: msg as Record<string, any>,
            sourceChannel: activeSession?.source_channel,
            buildDownloadUrl: (path, inline) => fileApi.downloadUrl(id, path, { inline }),
        });
        const markerImages = normalized.previewImages.length === 0
            ? extractChatImageDataMarkers(msg.content || '')
            : [];
        const images = normalized.previewImages.length > 0 ? normalized.previewImages : markerImages;
        return {
            ...msg,
            content: normalized.displayContent,
            attachments: normalized.attachments,
            fileName: normalized.fileName || msg.fileName,
            previewImages: msg.previewImages || images,
            imageUrl: msg.imageUrl || (images.length === 1 ? images[0].src : undefined),
        };
    };

    // Fold one live WS broadcast event into the READ-ONLY view's message list
    // (`historyMsgs`). Mirrors the streaming-bubble logic the writable live view
    // applies to `chatMessages`, so a monitored session updates live (user msg +
    // streamed assistant reply) while keeping the read-only view's pagination and
    // sender attribution intact. Returns the list unchanged for unhandled types.
    const applyMonitorEvent = (prev: any[], d: any): any[] => {
        return foldConversationTimelineEvent(prev, d, {
            makeId: createClientId,
            preserveTransient: d._preserveTurnStream === true,
        }).messages;
    };

    const applyMonitorEventRef = useRef(applyMonitorEvent);
    applyMonitorEventRef.current = applyMonitorEvent;
    const monitorStreamBatchRef = useRef<any[]>([]);
    const monitorStreamBatchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const flushMonitorStreamBatch = useCallback(() => {
        if (monitorStreamBatchTimerRef.current) {
            clearTimeout(monitorStreamBatchTimerRef.current);
            monitorStreamBatchTimerRef.current = null;
        }
        const batch = monitorStreamBatchRef.current;
        if (batch.length === 0) return;
        monitorStreamBatchRef.current = [];
        setHistoryMsgs((prev) => batch.reduce(
            (next, event) => applyMonitorEventRef.current(next, event),
            prev,
        ));
    }, []);
    const discardMonitorStreamBatch = useCallback(() => {
        if (monitorStreamBatchTimerRef.current) clearTimeout(monitorStreamBatchTimerRef.current);
        monitorStreamBatchTimerRef.current = null;
        monitorStreamBatchRef.current = [];
    }, []);
    const enqueueMonitorStreamEvent = useCallback((event: any) => {
        const last = monitorStreamBatchRef.current[monitorStreamBatchRef.current.length - 1];
        if (
            last
            && last.type === event.type
            && String(last.message_id || '') === String(event.message_id || '')
            && String(last.turn?.turn_anchor_id || '') === String(event.turn?.turn_anchor_id || '')
        ) {
            last.content = `${last.content || ''}${event.content || ''}`;
        } else {
            monitorStreamBatchRef.current.push({ ...event });
        }
        if (!monitorStreamBatchTimerRef.current) {
            monitorStreamBatchTimerRef.current = setTimeout(flushMonitorStreamBatch, 40);
        }
    }, [flushMonitorStreamBatch]);


    useEffect(() => {
        currentAgentIdRef.current = id;
    }, [id]);

    // Reset visible state whenever the viewed agent changes.
    // Existing background sockets keep running and will be cleaned up on unmount.
    useEffect(() => {
        sessionMsgAbortRef.current?.abort();
        historyMoreAbortRef.current?.abort();
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setHistoryMsgs([]);
        setIsStreaming(false);
        setIsWaiting(false);
        setIsStopping(false);
        setWsConnected(false);
        wsRef.current = null;
        onboardingRequestsRef.current = {};
        setOnboardingKickoffRequest(null);
        setWorkspaceLockedPath(null);
        setWorkspaceActivePath(null);
        setWorkspaceActivities([]);
        setWorkspaceLiveDraft(null);
        setLiveState({});
        setSidePanelTab('workspace');
        setChatScope('mine');
        setSessions([]);
        setAllSessions([]);
        setSessionsHasMore(false);
        setAllSessionsHasMore(false);
        setSessionsNextCursor(null);
        setAllSessionsNextCursor(null);
        sessionsListAbortRef.current?.abort();
        allSessionsListAbortRef.current?.abort();
        sessionsListGenerationRef.current += 1;
        allSessionsListGenerationRef.current += 1;
        setSessionsLoadingMore(false);
        setAllSessionsLoadingMore(false);
        setAgentExpired(false);
        settingsInitRef.current = false;
    }, [id]);

    // Switching login account or token must not leave another user's sessions/messages in memory.
    useEffect(() => {
        setSessions([]);
        setAllSessions([]);
        setSessionsHasMore(false);
        setAllSessionsHasMore(false);
        setSessionsNextCursor(null);
        setAllSessionsNextCursor(null);
        sessionsListAbortRef.current?.abort();
        allSessionsListAbortRef.current?.abort();
        sessionsListGenerationRef.current += 1;
        allSessionsListGenerationRef.current += 1;
        setChatScope('mine');
        sessionMsgAbortRef.current?.abort();
        historyMoreAbortRef.current?.abort();
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setHistoryMsgs([]);
        setWsConnected(false);
        setIsStreaming(false);
        setIsWaiting(false);
        setIsStopping(false);
        setSessionsLoading(false);
        setAllSessionsLoading(false);
        setSessionsLoadingMore(false);
        setAllSessionsLoadingMore(false);
        Object.keys(reconnectDisabledRef.current).forEach((k) => {
            reconnectDisabledRef.current[k] = true;
        });
        Object.keys(wsMapRef.current).forEach((k) => {
            const ws = wsMapRef.current[k];
            if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
        });
        wsMapRef.current = {};
        wsRef.current = null;
        onboardingRequestsRef.current = {};
        setOnboardingKickoffRequest(null);
    }, [currentUser?.id, token]);

    useEffect(() => {
        if (!id || !token || activeTab !== 'chat') return;
        if (skipNextSessionUrlRestoreRef.current) {
            skipNextSessionUrlRestoreRef.current = false;
            return;
        }
        if (requestedSessionId && activeSessionIdRef.current === requestedSessionId) return;

        let cancelled = false;
        const restoreSessionFromUrl = async () => {
            const mySessions = await fetchMySessions(false, id);
            if (cancelled || currentAgentIdRef.current !== id) return;
            setSessionsLoading(false);

            if (requestedSessionId) {
                const listedSession = mySessions.find((session: any) => String(session.id) === requestedSessionId);
                if (listedSession) {
                    setChatScope('mine');
                    await selectSession(listedSession, 'mine');
                    return;
                }

                try {
                    const resolvedSession = normalizeChatSession(await chatSessionApi.get(id, requestedSessionId));
                    if (cancelled || currentAgentIdRef.current !== id) return;
                    const resolvedScope: 'mine' | 'all' = resolvedSession.view_scope === 'all' ? 'all' : 'mine';
                    setChatScope(resolvedScope);
                    const isSubagentSession = String(resolvedSession.source_channel || '').toLowerCase() === 'subagent';
                    if (!isSubagentSession && resolvedScope === 'mine') {
                        setSessions((prev) => prev.some((item: any) => String(item.id) === requestedSessionId)
                            ? prev
                            : [resolvedSession, ...prev]);
                    } else if (!isSubagentSession) {
                        setAllSessions((prev) => prev.some((item: any) => String(item.id) === requestedSessionId)
                            ? prev
                            : [resolvedSession, ...prev]);
                        void fetchAllSessions().then((rows) => {
                            if (cancelled || currentAgentIdRef.current !== id) return;
                            if (!rows.some((item: any) => String(item.id) === requestedSessionId)) {
                                setAllSessions((prev) => prev.some((item: any) => String(item.id) === requestedSessionId)
                                    ? prev
                                    : [resolvedSession, ...prev]);
                            }
                        });
                    }
                    await selectSession(resolvedSession, resolvedScope);
                    return;
                } catch (error: any) {
                    if (cancelled) return;
                    console.warn('[chat] unable to restore session from URL:', error);
                    toast.warning(t('chat.sessionLinkUnavailable', 'The linked session is unavailable. Opened your latest session instead.'));
                }
            }

            const webSessions = mySessions.filter((session: any) => (
                String(session.source_channel || 'web').toLowerCase() === 'web'
                && !session.is_group
            ));
            const defaultWebSession = webSessions.find((session: any) => session.is_primary)
                || webSessions.reduce((latest: any | null, session: any) => {
                    if (!latest) return session;
                    const createdAt = String(session.created_at || '');
                    const latestCreatedAt = String(latest.created_at || '');
                    if (createdAt !== latestCreatedAt) return createdAt > latestCreatedAt ? session : latest;
                    return String(session.id) > String(latest.id) ? session : latest;
                }, null);

            if (defaultWebSession) {
                setChatScope('mine');
                await selectSession(defaultWebSession, 'mine');
            } else {
                clearChatSelection();
            }
        };

        void restoreSessionFromUrl();
        return () => {
            cancelled = true;
        };
    }, [id, token, activeTab, currentUser?.id, requestedSessionId]);

    const ensureSessionSocket = (sess: any, agentId: string, authToken: string) => {
        const sessionId = String(sess.id);
        const key = buildSessionRuntimeKey(agentId, sessionId);
        const existing = wsMapRef.current[key];
        if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return existing;
        reconnectDisabledRef.current[key] = false;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const sessionParam = `&session_id=${sessionId}`;

        const scheduleReconnect = () => {
            if (reconnectDisabledRef.current[key]) return;
            clearReconnectTimer(key);
            // Background tab: do NOT reconnect. A hidden tab that keeps hammering
            // every 2s is the reconnect-storm amplifier. The visibilitychange
            // handler resumes the active session the instant it is foregrounded.
            if (typeof document !== 'undefined' && document.hidden) return;
            // Exponential backoff with jitter, capped — replaces the old fixed
            // 2s retry that turned any transient drop into an unbounded storm.
            const attempt = reconnectAttemptsRef.current[key] || 0;
            reconnectAttemptsRef.current[key] = attempt + 1;
            const base = Math.min(30000, 1000 * 2 ** attempt); // 1s,2s,4s,…,30s cap
            const delay = Math.round(base * (0.75 + Math.random() * 0.5)); // ±25% jitter
            reconnectTimerRef.current[key] = setTimeout(() => {
                reconnectTimerRef.current[key] = null;
                if (!reconnectDisabledRef.current[key]) ensureSessionSocket(sess, agentId, authToken);
            }, delay);
        };

        const lang = (i18n.language || 'en').toLowerCase().startsWith('zh') ? 'zh' : 'en';
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/chat/${agentId}?token=${authToken}${sessionParam}&lang=${lang}`);
        let settleServerConnection: ((connected: boolean) => void) | null = null;
        (ws as any)._serverConnectedPromise = new Promise<boolean>((resolve) => {
            settleServerConnection = resolve;
        });
        (ws as any)._settleServerConnection = (connected: boolean) => {
            settleServerConnection?.(connected);
            settleServerConnection = null;
        };
        wsMapRef.current[key] = ws;
        ws.onopen = () => {
            // 若本 ws 已被新连接替换(陈旧引用)或已禁用重连，直接关掉它，不要触碰 UI 连接状态。
            if (reconnectDisabledRef.current[key] || wsMapRef.current[key] !== ws) {
                ws.close();
                return;
            }
            // Connected — cancel any pending reconnect. Reset the backoff counter
            // only after the socket proves STABLE (≥3s), so a connection that
            // opens then immediately drops keeps backing off instead of resetting
            // to a fast 1s retry loop. Cleared in onclose.
            clearReconnectTimer(key);
            (ws as any)._stableTimer = setTimeout(() => {
                if (wsMapRef.current[key] === ws) reconnectAttemptsRef.current[key] = 0;
            }, 3000);
            if (currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId) {
                wsRef.current = ws;
            }
        };
        ws.onclose = (e) => {
            (ws as any)._settleServerConnection?.(false);
            if ((ws as any)._stableTimer) {
                clearTimeout((ws as any)._stableTimer);
                (ws as any)._stableTimer = null;
            }
            const wasCurrent = wsMapRef.current[key] === ws;
            if (onboardingRequestsRef.current[key]?.socket === ws) {
                delete onboardingRequestsRef.current[key];
            }
            // 陈旧连接(已被新连接替换或显式关闭)的 onclose 不应扰动当前 UI 状态，也不应触发重连——
            // 否则活跃连接会被误判为断开，引发无谓的 2s 重连循环。
            if (!wasCurrent) return;
            delete wsMapRef.current[key];
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            const runtimeBeforeClose = sessionUiStateRef.current[key];
            sessionTurnRuntimeRef.current[key] = beginConversationTurnRecovery(
                sessionTurnRuntimeRef.current[key] || IDLE_CONVERSATION_TURN,
            );
            const turnWasActive = Boolean(runtimeBeforeClose
                && (runtimeBeforeClose.isWaiting || runtimeBeforeClose.isStreaming || runtimeBeforeClose.isStopping));
            if (turnWasActive && isActiveRuntime) {
                pcRecoveryPollingNeededRef.current = true;
            }
            setSessionUiState(key, { isWaiting: false, isStreaming: false, isStopping: false });
            if (isActiveRuntime) {
                wsRef.current = null;
                setWsConnected(false);
                setIsWaiting(false);
                setIsStreaming(false);
                setIsStopping(false);
                setOnboardingKickoffRequest(null);
                if (turnWasActive && !pcPageSuspendedRef.current) {
                    startPcRecoveryPollingRef.current(sess, activeReadOnlyRef.current ? 'all' : 'mine');
                }
            }
            if (e.code === 4003 || e.code === 4002) {
                reconnectDisabledRef.current[key] = true;
                clearReconnectTimer(key);
                reconnectAttemptsRef.current[key] = 0;
                if (isActiveRuntime && e.code === 4003) setAgentExpired(true);
                return;
            }
            scheduleReconnect();
        };
        ws.onerror = (error) => {
            if (wsMapRef.current[key] !== ws) return;
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            if (isActiveRuntime) setWsConnected(false);
            console.warn(`WebSocket error for session ${sessionId}:`, error);
            // Error automatically triggers onclose with abnormal code, which handles reconnect
        };
        const handleSocketMessage = (d: any) => {
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            const turnReduction = reduceConversationTurnEvent(
                sessionTurnRuntimeRef.current[key] || IDLE_CONVERSATION_TURN,
                d,
            );
            if (!conversationTurnEventShouldBeHandled(turnReduction)) return;
            sessionTurnRuntimeRef.current[key] = turnReduction.runtime;
            if (turnReduction.controlsLifecycle && turnReduction.hasSnapshot) {
                const nextWaiting = conversationTurnIsWaiting(turnReduction.runtime);
                const nextStreaming = conversationTurnIsStreaming(turnReduction.runtime);
                setSessionUiState(key, {
                    isWaiting: nextWaiting,
                    isStreaming: nextStreaming,
                    ...(turnReduction.runtime.snapshot.phase !== 'active' ? { isStopping: false } : {}),
                });
                if (isActiveRuntime) {
                    setIsWaiting(nextWaiting);
                    setIsStreaming(nextStreaming);
                    if (turnReduction.runtime.snapshot.phase !== 'active') setIsStopping(false);
                }
            }
            const isTerminalEvent = conversationTurnEventClosesStream(turnReduction, d);
            if (isTerminalEvent && pcPageSuspendedRef.current && isActiveRuntime) {
                // The terminal frame may precede durable persistence. Resume
                // reconciliation therefore keeps a short polling window alive.
                pcRecoveryPollingNeededRef.current = true;
            } else if (isTerminalEvent) {
                const recoveryAction = resolveVisibleTerminalRecoveryAction({
                    isActiveRuntime,
                    recoveryNeeded: pcRecoveryPollingNeededRef.current,
                });
                if (recoveryAction === 'continue') {
                    startPcRecoveryPollingRef.current(
                        sess,
                        activeReadOnlyRef.current ? 'all' : 'mine',
                    );
                } else if (recoveryAction === 'clear') {
                    pcRecoveryPollingNeededRef.current = false;
                    cancelPcRecoveryPolling();
                }
            }
            if (pcPageSuspendedRef.current && isActiveRuntime) {
                pcHiddenDroppedEventRef.current = true;
                if (isTerminalEvent) {
                    const currentRuntime = sessionUiStateRef.current[key] || {
                        isWaiting: false,
                        isStreaming: false,
                        isStopping: false,
                    };
                    sessionUiStateRef.current[key] = {
                        ...currentRuntime,
                        isWaiting: false,
                        isStreaming: false,
                        isStopping: false,
                    };
                }
                return;
            }
            if (d.type !== 'thinking' && d.type !== 'chunk') {
                flushChatStreamBatch();
                flushMonitorStreamBatch();
            }
            if (d.type === 'connected' && d.session_id) {
                (ws as any)._serverConnected = true;
                (ws as any)._settleServerConnection?.(true);
                const request: OnboardingKickoffRequest = {
                    sessionId: String(d.session_id),
                    required: d.onboarding_required === true,
                    socket: ws,
                };
                onboardingRequestsRef.current[key] = request;
                if (isActiveRuntime) {
                    wsRef.current = ws;
                    setWsConnected(true);
                    setWsSessionId(String(d.session_id));
                    setOnboardingKickoffRequest(request);
                    if (request.required) {
                        setSessionUiState(key, { isWaiting: true, isStreaming: false });
                        setIsWaiting(true);
                        setIsStreaming(false);
                    }
                }
                if (!request.required && pendingChatSendRef.current?.runtimeKey === key) {
                    const pending = pendingChatSendRef.current;
                    pendingChatSendRef.current = null;
                    setChatInfoMsg(null);
                    dispatchChatMessage(ws, key, pending);
                }
                return;
            }
            // The server committed the phase transition before publishing the
            // first welcome output. Refresh the cached agent record immediately.
            if (d.type === 'onboarded') {
                delete onboardingRequestsRef.current[key];
                if (isActiveRuntime) setOnboardingKickoffRequest(null);
                queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                return;
            }
            if (d.type === 'onboarding_skipped') {
                delete onboardingRequestsRef.current[key];
                setSessionUiState(key, { isWaiting: false, isStreaming: false, isStopping: false });
                if (isActiveRuntime) {
                    setOnboardingKickoffRequest(null);
                    setIsWaiting(false);
                    setIsStreaming(false);
                    setIsStopping(false);
                }
                queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                return;
            }
            if (d.type === 'turn_receipt') return;
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot && ['thinking', 'chunk', 'workspace_draft', 'tool_call', 'confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                const nextStreaming = ['thinking', 'chunk', 'workspace_draft', 'tool_call'].includes(d.type);
                const endStreaming = ['confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type);
                setSessionUiState(key, {
                    isWaiting: false,
                    isStreaming: endStreaming ? false : nextStreaming,
                    ...(endStreaming ? { isStopping: false } : {}),
                });
            }
            if (!isActiveRuntime) {
                if (['done', 'error', 'quota_exceeded', 'trigger_notification'].includes(d.type)) {
                    fetchMySessions(true, agentId);
                    queryClient.invalidateQueries({ queryKey: ['agents'] });
                }
                if (conversationTurnEventClosesStream(turnReduction, d)) {
                    closeSessionSocket(key, true);
                }
                if (['confirmation_card', 'confirmation_update'].includes(d.type)) {
                    // fall through to handle confirmation events even for inactive runtime
                } else {
                    return;
                }
            }

            // Active READ-ONLY monitor: mirror live broadcasts into the read-only
            // view's list (`historyMsgs`) instead of the writable live view's
            // `chatMessages`. The composer stays disabled — we only reflect the
            // conversation as it streams in, so a monitored channel / other-user
            // session updates live instead of only on reload.
            if (activeReadOnlyRef.current) {
                if (['channel_user_message', 'user_message_committed', 'assistant_message_committed', 'thinking', 'chunk', 'tool_call', 'done'].includes(d.type)) {
                    if (d.type === 'thinking' || d.type === 'chunk') enqueueMonitorStreamEvent(d);
                    else setHistoryMsgs(prev => applyMonitorEvent(prev, {
                        ...d,
                        _preserveTurnStream: !turnReduction.controlsLifecycle,
                    }));
                    if (d.type === 'done') {
                        const sid = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                        if (sid) clearUnreadForSession(sid);
                        fetchMySessions(true, agentId);
                    }
                }
                return;
            }

            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot && ['thinking', 'chunk', 'workspace_draft', 'tool_call', 'confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                setIsWaiting(false);
                if (['thinking', 'chunk', 'workspace_draft', 'tool_call'].includes(d.type)) setIsStreaming(true);
                if (['confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                    setIsStreaming(false);
                    setIsStopping(false);
                }
            }

            if (d.type === 'confirmation_required') {
                setChatMessages((prev) => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                }).messages as ChatMsg[]);
            } else if (d.type === 'thinking') {
                enqueueChatStreamEvent({
                    type: 'thinking',
                    content: d.content || '',
                    messageId: d.message_id ? String(d.message_id) : undefined,
                    turnAnchorId: d.turn?.turn_anchor_id ? String(d.turn.turn_anchor_id) : undefined,
                    turnGeneration: Number.isInteger(d.turn?.generation) ? Number(d.turn.generation) : undefined,
                    producerScope: d.producer_scope ? String(d.producer_scope) : undefined,
                });
            } else if (d.type === 'workspace_draft') {
                if (WORKSPACE_TOOLS.has(d.name)) {
                    const parsedDraft = parseWorkspaceDraftArgs(d.name, d.arguments || '');
                    const draft: WorkspaceLiveDraft = {
                        id: d.id || `${d.name}-${d.index || 0}`,
                        tool: d.name,
                        action: workspaceActionForTool(d.name),
                        status: 'drafting',
                        ...parsedDraft,
                    };
                    setWorkspaceLiveDraft(draft);
                    if (allowWorkspaceAutoSwitch(draft.path)) {
                        setWorkspaceActivePath(draft.path!);
                    }
                    if (isFocusPath(draft.path)) {
                        openAwarePanel();
                    } else if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('workspace');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
            } else if (d.type === 'tool_call') {
                if (AWARE_TOOLS.has(d.name)) {
                    openAwarePanel();
                    if (d.status === 'done') {
                        refetchTriggers();
                        refetchFocusItems();
                        queryClient.invalidateQueries({ queryKey: ['focus', id] });
                    }
                }
                if (d.name === 'agentbay_file_transfer') {
                    const transfer = parseAgentBayTransferArgs(d.args);
                    setLiveState(prev => ({
                        ...prev,
                        transfer: {
                            ...prev.transfer,
                            ...transfer,
                            status: d.status === 'done' ? 'done' : 'running',
                            result: d.status === 'done' && typeof d.result === 'string' ? d.result : prev.transfer?.result,
                            updatedAt: Date.now(),
                        },
                    }));
                    if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('transfer');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
                if (WORKSPACE_TOOLS.has(d.name)) {
                    if (d.status === 'running') {
                        const rawArgs = typeof d.args === 'string' ? d.args : JSON.stringify(d.args || {});
                        const parsedDraft = parseWorkspaceDraftArgs(d.name, rawArgs);
                        const draft: WorkspaceLiveDraft = {
                            id: d.id || `${d.name}-running`,
                            tool: d.name,
                            action: workspaceActionForTool(d.name),
                            status: 'running',
                            ...parsedDraft,
                        };
                        setWorkspaceLiveDraft(draft);
                        if (allowWorkspaceAutoSwitch(draft.path)) {
                            setWorkspaceActivePath(draft.path!);
                        }
                        if (isFocusPath(draft.path)) {
                            openAwarePanel();
                        } else if (allowLivePanelAutoFocus()) {
                            setSidePanelTab('workspace');
                            setLivePanelVisible(true);
                            collapseSidebarsForLivePanel();
                        }
                    } else if (d.status === 'done') {
                        setWorkspaceLiveDraft(null);
                    }
                }
                if (d.live_preview) {
                    const lp = d.live_preview;
                    setLiveState(prev => {
                        const next = { ...prev };
                        if ((lp.env === 'desktop' || lp.env === 'browser') && lp.screenshot_url) {
                            if (lp.env === 'desktop') next.desktop = { screenshotUrl: lp.screenshot_url };
                            else next.browser = { screenshotUrl: lp.screenshot_url };
                            if (allowLivePanelAutoFocus()) setSidePanelTab(lp.env === 'desktop' ? 'desktop' : 'browser');
                        } else if (lp.env === 'code' && lp.output) {
                            const existing = prev.code?.output || '';
                            next.code = { output: existing + (existing ? '\n---\n' : '') + lp.output };
                            if (allowLivePanelAutoFocus()) setSidePanelTab('code');
                        }
                        return next;
                    });
                    if (allowLivePanelAutoFocus()) {
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
                    if (d.workspace_activity) {
                        const activity = d.workspace_activity as WorkspaceActivity;
                        setWorkspaceLiveDraft(null);
                        setWorkspaceActivities(prev => [activity, ...prev.filter(item => item.path !== activity.path)].slice(0, 20));
                        if (activity.action === 'delete' && activity.ok !== false && !activity.pendingApproval) {
                            handleWorkspacePathDeleted(activity.path);
                        }
                        if (activity.action !== 'delete' && activity.ok !== false && allowWorkspaceAutoSwitch(activity.path)) {
                            setWorkspaceActivePath(activity.path);
                        }
                    if (isFocusPath(activity.path)) {
                        openAwarePanel();
                        refetchFocusItems();
                        queryClient.invalidateQueries({ queryKey: ['focus', id] });
                    } else if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('workspace');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                    queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                }
                setChatMessages((prev) => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                }).messages as ChatMsg[]);
                if (d.status === 'done') {
                    const currentSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                    if (currentSessionId) clearUnreadForSession(currentSessionId);
                    queryClient.invalidateQueries({ queryKey: ['agents'] });
                }
            } else if (d.type === 'user_message_committed') {
                setChatMessages(prev => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                }).messages as ChatMsg[]);
            } else if (d.type === 'assistant_message_committed') {
                setChatMessages(prev => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                    preserveTransient: !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot,
                }).messages as ChatMsg[]);
            } else if (d.type === 'channel_user_message') {
                // An IM (DingTalk/Feishu/…) user sent a message in this session —
                // mirror it live so a web viewer sees the user's own message
                // without reloading (the agent reply already streams in).
                setChatMessages(prev => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                }).messages as ChatMsg[]);
                const cuSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                if (cuSessionId) clearUnreadForSession(cuSessionId);
            } else if (d.type === 'chunk') {
                enqueueChatStreamEvent({
                    type: 'chunk',
                    content: d.content || '',
                    messageId: d.message_id ? String(d.message_id) : undefined,
                    turnAnchorId: d.turn?.turn_anchor_id ? String(d.turn.turn_anchor_id) : undefined,
                    turnGeneration: Number.isInteger(d.turn?.generation) ? Number(d.turn.generation) : undefined,
                    producerScope: d.producer_scope ? String(d.producer_scope) : undefined,
                });
            } else if (d.type === 'done') {
                setChatMessages(prev => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                    preserveTransient: !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot,
                }).messages as ChatMsg[]);
                const currentSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                if (currentSessionId) clearUnreadForSession(currentSessionId);
                fetchMySessions(true, agentId);
                if (canViewAllAgentChatSessions && (scopeDropdownOpen || chatScope === 'all' || allSessions.length > 0)) {
                    fetchAllSessions(true, false, agentId);
                }
                queryClient.invalidateQueries({ queryKey: ['agents'] });
            } else if (d.type === 'error' || d.type === 'quota_exceeded') {
                setChatMessages(prev => foldConversationTimelineEvent(prev as any, d, {
                    makeId: createClientId,
                }).messages as ChatMsg[]);
                const msg = d.content || d.detail || d.message || 'Request denied';
                const isNoModelError = msg.includes('no LLM model') || msg.includes('No model');
                if (isNoModelError) {
                    reconnectDisabledRef.current[key] = true;
                    return;
                }
                setChatMessages(prev => {
                    const last = prev[prev.length - 1];
                    const warningText = `Warning: ${msg}`;
                    if (last && last.role === 'assistant' && last.content === warningText) return prev;
                    return [...prev, parseChatMsg({ role: 'assistant', content: warningText })];
                });
                if (msg.includes('expired') || msg.includes('Setup failed')) {
                    reconnectDisabledRef.current[key] = true;
                    if (msg.includes('expired')) setAgentExpired(true);
                }
            } else if (d.type === 'trigger_notification') {
                const targetSessionId = d.session_id ? String(d.session_id) : '';
                const currentSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                if (targetSessionId && currentSessionId === targetSessionId) {
                    setChatMessages(prev => [...prev, parseChatMsg({ role: 'assistant', content: d.content })]);
                    clearUnreadForSession(targetSessionId);
                }
                fetchMySessions(true, agentId);
                queryClient.invalidateQueries({ queryKey: ['agents'] });
            } else if (d.type === 'info') {
                // Subtle transient banner for system events (e.g. fallback model switch)
                setChatInfoMsg(d.content || '');
                if (chatInfoTimerRef.current) clearTimeout(chatInfoTimerRef.current);
                chatInfoTimerRef.current = setTimeout(() => setChatInfoMsg(null), 6000);
            } else if (d.type === 'agentbay_live') {
                // Real-time streaming from execute_code or other AgentBay envs
                if ((d.env === 'desktop' || d.env === 'browser') && d.screenshot_url) {
                    setLiveState(prev => ({
                        ...prev,
                        [d.env]: { screenshotUrl: d.screenshot_url },
                    }));
                    if (allowLivePanelAutoFocus()) {
                        setSidePanelTab(d.env === 'desktop' ? 'desktop' : 'browser');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                } else if (d.env === 'code' && d.output) {
                    setLiveState(prev => ({
                        ...prev,
                        code: {
                            output: appendLiveCodeOutput(
                                prev.code?.output || '',
                                `${d.stream === 'stderr' ? '⚠️ ' : ''}${d.output}`
                            ),
                        },
                    }));
                    if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('code');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
            } else if (
                ['user', 'assistant', 'system'].includes(String(d.role || ''))
                && typeof d.content === 'string'
            ) {
                setChatMessages(prev => [...prev, parseChatMsg({ role: d.role, content: d.content })]);
            }
        };
        ws.onmessage = (e) => {
            if (wsMapRef.current[key] !== ws) return;
            const d = JSON.parse(e.data);
            if (
                d.type !== 'connected'
                && bufferResumeEvent(pcResumeEventGateRef.current, key, {
                    data: d,
                    consume: handleSocketMessage,
                })
            ) return;
            handleSocketMessage(d);
        };
        return ws;
    };
    ensureSessionSocketRef.current = ensureSessionSocket;

    const waitForPcSocketServerConnection = async (socket: WebSocket | undefined) => {
        if (!socket) return false;
        if ((socket as any)._serverConnected === true) return true;
        const connection = (socket as any)._serverConnectedPromise as Promise<boolean> | undefined;
        if (!connection) return false;
        let timeout: ReturnType<typeof setTimeout> | null = null;
        try {
            return await Promise.race([
                connection,
                new Promise<boolean>((resolve) => {
                    timeout = setTimeout(() => resolve(false), 5000);
                }),
            ]);
        } finally {
            if (timeout) clearTimeout(timeout);
        }
    };

    const dispatchChatMessage = (socket: WebSocket, runtimeKey: SessionRuntimeKey, payload: PendingChatMessage) => {
        pendingPcRouteRecoveryRuntimeKeys.delete(runtimeKey);
        setIsWaiting(true);
        setIsStreaming(false);
        setIsStopping(false);
        setSessionUiState(runtimeKey, { isWaiting: true, isStreaming: false, isStopping: false });
        setChatMessages(prev => [...prev, parseChatMsg({
            id: payload.messageId,
            role: 'user',
            content: payload.displayContent,
            display_content: payload.displayContent,
            fileName: payload.fileName,
            imageUrl: payload.imageUrl,
            previewImages: payload.previewImages,
            attachments: payload.attachments,
            timestamp: new Date().toISOString()
        })]);
        socket.send(JSON.stringify({
            message_id: payload.messageId,
            content: payload.contentForLLM,
            display_content: payload.displayContent,
            file_name: payload.fileName,
            attachments: payload.attachments,
            model_id: payload.modelId,
        }));
    };

    useEffect(() => {
        if (!id || !token || activeTab !== 'chat') return;
        if (document.hidden || pcPageSuspendedRef.current) return;
        if (!activeSession) {
            syncActiveSocketState(null, id);
            return;
        }
        activeSessionIdRef.current = String(activeSession.id);
        activeReadOnlyRef.current = !isWritableSession(activeSession);
        // Open a live socket for ANY visible session — including ones the viewer
        // does not own (read-only monitor). The backend accepts those read-only
        // (composer stays disabled), so monitored channel / other-user
        // conversations update live instead of only on reload.
        ensureSessionSocket(activeSession, id, token);
        syncActiveSocketState(activeSession, id);
    }, [id, token, activeTab, activeSession?.id, chatScope, canViewAllAgentChatSessions]);

    // Suspend the active PC chat while covered. WebViews can otherwise queue a
    // large burst of socket frames and React work, then replay it on foreground.
    // On resume, reload the latest page and reconnect once so missed final output
    // is reconciled from durable history instead of relying on frame replay.
    useEffect(() => {
        const startRecoveryPolling = (
            session: any,
            scope: 'mine' | 'all',
        ) => {
            cancelPcRecoveryPolling();
            const pollingGeneration = pcRecoveryPollingGenerationRef.current;
            const delays = [1000, 2000, 4000, 8000, 16000, 30000];
            let attempt = 0;
            let successfulLoads = 0;
            const poll = () => {
                if (
                    pollingGeneration !== pcRecoveryPollingGenerationRef.current
                    || !pcRecoveryPollingNeededRef.current
                    || pcPageSuspendedRef.current
                    || document.hidden
                    || String(activeSessionIdRef.current || '') !== String(session.id)
                ) return;
                if (pcResumeReconcilePromiseRef.current) {
                    if (pollingGeneration !== pcRecoveryPollingGenerationRef.current) return;
                    pcRecoveryPollTimerRef.current = setTimeout(poll, 500);
                    return;
                }
                const runtimeKey = buildSessionRuntimeKey(String(id || ''), String(session.id));
                const gate = beginPcResumeEventGate(runtimeKey);
                let loadedSuccessfully = false;
                const promise = selectSession(session, scope, { preserveLoadedHistory: true }).then((loaded) => {
                    loadedSuccessfully = loaded === true;
                    if (loadedSuccessfully) successfulLoads += 1;
                }).finally(() => {
                    const stillActive = !pcPageSuspendedRef.current
                        && !document.hidden
                        && String(activeSessionIdRef.current || '') === String(session.id);
                    finishPcResumeEventGate(gate, stillActive);
                    releasePcResumeReconcileOwner(
                        promise,
                        session,
                        String(id || ''),
                        String(token || ''),
                    );
                    if (stillActive) setPcResumeMeasurementKey((current) => (current ?? 0) + 1);
                    if (pollingGeneration !== pcRecoveryPollingGenerationRef.current) return;
                    if (shouldCompleteRecoveryPolling({
                        pollingGeneration,
                        currentGeneration: pcRecoveryPollingGenerationRef.current,
                        loadedSuccessfully,
                        successfulLoads,
                        stillActive,
                        socketReadyState: wsMapRef.current[runtimeKey]?.readyState,
                    })) {
                        pendingPcRouteRecoveryRuntimeKeys.delete(runtimeKey);
                        pcRecoveryPollingNeededRef.current = false;
                        pcRecoveryPollTimerRef.current = null;
                        return;
                    }
                    if (!pcRecoveryPollingNeededRef.current || attempt >= delays.length) return;
                    pcRecoveryPollTimerRef.current = setTimeout(poll, delays[attempt++]);
                });
                pcResumeReconcilePromiseRef.current = promise;
            };
            pcRecoveryPollTimerRef.current = setTimeout(poll, delays[attempt++]);
        };
        startPcRecoveryPollingRef.current = startRecoveryPolling;
        const suspend = () => {
            if (pcPageSuspendedRef.current) return;
            pcPageSuspendedRef.current = true;
            setPcPageActive(false);
            discardChatStreamBatch();
            discardMonitorStreamBatch();
            cancelPcAutoFollowRef.current();
            sessionMsgAbortRef.current?.abort();
            historyMoreAbortRef.current?.abort();
            cancelPcRecoveryPolling();
            const activeGate = pcResumeEventGateRef.current;
            if (activeGate) finishPcResumeEventGate(activeGate, false);
            if (!id || !activeSession) return;
            const key = buildSessionRuntimeKey(id, String(activeSession.id));
            const runtime = sessionUiStateRef.current[key];
            const ws = wsMapRef.current[key];
            const turnWasActive = !!runtime
                && (runtime.isWaiting || runtime.isStreaming || runtime.isStopping);
            sessionTurnRuntimeRef.current[key] = beginConversationTurnRecovery(
                sessionTurnRuntimeRef.current[key] || IDLE_CONVERSATION_TURN,
            );
            pcResumeHadActiveTurnRef.current = turnWasActive;
            if (turnWasActive) pcRecoveryPollingNeededRef.current = true;
            reconnectDisabledRef.current[key] = true;
            clearReconnectTimer(key);
            if (wsMapRef.current[key] === ws) delete wsMapRef.current[key];
            if (wsRef.current === ws) wsRef.current = null;
            if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, 'page hidden');
            setSessionUiState(key, { isWaiting: false, isStreaming: false, isStopping: false });
            setWsConnected(false);
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            setOnboardingKickoffRequest(null);
        };
        const resume = () => {
            if (!pcPageSuspendedRef.current || document.hidden) return;
            pcPageSuspendedRef.current = false;
            setPcPageActive(true);
            if (!id || !token || activeTab !== 'chat') return;
            if (!activeSession) return;
            const key = buildSessionRuntimeKey(id, String(activeSession.id));
            const resumeHadActiveTurn = pcResumeHadActiveTurnRef.current;
            pcResumeHadActiveTurnRef.current = false;
            pcHiddenDroppedEventRef.current = false;
            reconnectDisabledRef.current[key] = false;
            reconnectAttemptsRef.current[key] = 0;
            clearReconnectTimer(key);
            if (pcResumeReconcilePromiseRef.current) {
                pcRecoveryPollingNeededRef.current = true;
                startRecoveryPolling(activeSession, chatScope);
                return;
            }
            const gate = beginPcResumeEventGate(key);
            const socket = ensureSessionSocket(activeSession, id, token);
            const promise = (async () => {
                await waitForPcSocketServerConnection(socket);
                if (
                    pcPageSuspendedRef.current
                    || document.hidden
                    || String(activeSessionIdRef.current || '') !== String(activeSession.id)
                ) {
                    finishPcResumeEventGate(gate, false);
                    return;
                }
                await selectSession(activeSession, chatScope, {
                    preserveLoadedHistory: true,
                    prepareWebResume: resumeHadActiveTurn,
                });
                const stillActive = !pcPageSuspendedRef.current
                    && !document.hidden
                    && String(activeSessionIdRef.current || '') === String(activeSession.id);
                finishPcResumeEventGate(gate, stillActive);
                if (!stillActive) return;
                setPcResumeMeasurementKey((current) => (current ?? 0) + 1);
                if (pcRecoveryPollingNeededRef.current) startRecoveryPolling(activeSession, chatScope);
            })().finally(() => {
                releasePcResumeReconcileOwner(promise, activeSession, id, token);
            });
            pcResumeReconcilePromiseRef.current = promise;
        };
        const onVisibility = () => {
            if (document.hidden) suspend();
            else resume();
        };
        const onPageHide = () => suspend();
        const onPageShow = () => resume();
        document.addEventListener('visibilitychange', onVisibility);
        window.addEventListener('pagehide', onPageHide);
        window.addEventListener('pageshow', onPageShow);
        if (document.hidden || activeTab !== 'chat') suspend();
        else resume();
        return () => {
            document.removeEventListener('visibilitychange', onVisibility);
            window.removeEventListener('pagehide', onPageHide);
            window.removeEventListener('pageshow', onPageShow);
            cancelPcRecoveryPolling();
            startPcRecoveryPollingRef.current = () => undefined;
        };
    }, [id, token, activeTab, activeSession?.id, chatScope, discardChatStreamBatch, discardMonitorStreamBatch]);

    useEffect(() => {
        if (!id || !activeSession || activeTab !== 'chat' || document.hidden) return;
        const runtimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        if (!pendingPcRouteRecoveryRuntimeKeys.has(runtimeKey)) return;
        pcRecoveryPollingNeededRef.current = true;
        startPcRecoveryPollingRef.current(activeSession, chatScope);
    }, [id, activeTab, activeSession?.id, chatScope]);

    const handleWorkspacePathDeleted = useCallback((path: string) => {
        let removedName = '';
        setAttachedFiles((prev) => prev.filter((file) => {
            const shouldRemove = file.source === 'workspace_auto' && file.path === path;
            if (shouldRemove) removedName = file.name;
            return !shouldRemove;
        }));
        setWorkspaceLockedPath((current) => current === path ? null : current);
        dismissedWorkspaceRefPath.current = path;
        if (removedName) {
            setChatInfoMsg(`Removed attachment: ${removedName} (file was deleted).`);
            if (chatInfoTimerRef.current) clearTimeout(chatInfoTimerRef.current);
            chatInfoTimerRef.current = setTimeout(() => {
                setChatInfoMsg(null);
                chatInfoTimerRef.current = null;
            }, 4000);
        }
    }, []);

    useEffect(() => {
        const shouldAutoReference = livePanelVisible && sidePanelTab === 'workspace' && !!workspaceActivePath;
        if (!shouldAutoReference) {
            dismissedWorkspaceRefPath.current = null;
            setAttachedFiles((prev) => prev.filter((file) => file.source !== 'workspace_auto'));
            return;
        }
        const path = workspaceActivePath!;
        if (dismissedWorkspaceRefPath.current === path) return;
        setAttachedFiles((prev) => {
            const withoutAuto = prev.filter((file) => file.source !== 'workspace_auto');
            return [
                ...withoutAuto,
                { name: workspaceFileName(path), text: '', path, source: 'workspace_auto' },
            ];
        });
    }, [livePanelVisible, sidePanelTab, workspaceActivePath]);

    useEffect(() => {
        return () => {
            const activeAgentId = String(currentAgentIdRef.current || '');
            const activeSessionId = String(activeSessionIdRef.current || '');
            if (activeAgentId && activeSessionId) {
                const runtimeKey = buildSessionRuntimeKey(activeAgentId, activeSessionId);
                const runtime = sessionUiStateRef.current[runtimeKey];
                sessionTurnRuntimeRef.current[runtimeKey] = beginConversationTurnRecovery(
                    sessionTurnRuntimeRef.current[runtimeKey] || IDLE_CONVERSATION_TURN,
                );
                if (
                    pcRecoveryPollingNeededRef.current
                    || runtime?.isWaiting
                    || runtime?.isStreaming
                    || runtime?.isStopping
                ) {
                    pendingPcRouteRecoveryRuntimeKeys.add(runtimeKey);
                }
            }
            pcPageSuspendedRef.current = true;
            pcResumeReconcilePromiseRef.current = null;
            const resumeGate = pcResumeEventGateRef.current;
            pcResumeEventGateRef.current = null;
            if (resumeGate) drainResumeEventGate(resumeGate);
            ensureSessionSocketRef.current = () => undefined;
            sessionMsgAbortRef.current?.abort();
            historyMoreAbortRef.current?.abort();
            cancelPcRecoveryPolling();
            Object.keys(reconnectDisabledRef.current).forEach((key) => { reconnectDisabledRef.current[key] = true; });
            Object.keys(reconnectTimerRef.current).forEach((key) => clearReconnectTimer(key));
            Object.values(wsMapRef.current).forEach((ws) => {
                if (ws.readyState !== WebSocket.CLOSED) ws.close();
            });
            wsMapRef.current = {};
            wsRef.current = null;
            discardChatStreamBatch();
            discardMonitorStreamBatch();
        };
    }, [discardChatStreamBatch, discardMonitorStreamBatch]);

    // Conversation auto-follow is shared with H5. Any explicit user scroll
    // gesture pauses it until the floating button is clicked or a message is sent.
    const [chatScrollBtnBottom, setChatScrollBtnBottom] = useState(96);
    const historyContainerRef = useRef<HTMLDivElement>(null);
    const historyAutoLoadCursorRef = useRef<string | null>(null);
    const generationActive = isWaiting || isStreaming || isStopping;
    const liveScrollAnchor = useMemo(() => getConversationScrollAnchor(
        buildConversationEntries(chatMessages as any),
        generationActive,
    ), [chatMessages, generationActive]);
    const readonlyGenerationActive = Boolean(
        activeSession
        && !isWritableSession(activeSession)
        && generationActive
    );
    const historyScrollAnchor = useMemo(() => getConversationScrollAnchor(
        buildConversationEntries(historyMsgs as any),
        readonlyGenerationActive,
    ), [historyMsgs, readonlyGenerationActive]);
    const {
        showScrollToBottom: showScrollBtn,
        resumeAutoFollow: scrollToBottom,
        cancelPendingAutoFollow: cancelLiveAutoFollow,
        interactionProps: liveAutoFollowInteractionProps,
    } = useConversationAutoFollow({
        scrollerRef: chatContainerRef,
        contentKey: liveScrollAnchor,
        resetKey: activeSession?.id,
        enabled: pcPageActive && activeTab === 'chat' && !!activeSession && isWritableSession(activeSession),
    });
    const {
        showScrollToBottom: showHistoryScrollBtn,
        resumeAutoFollow: scrollHistoryToBottom,
        cancelPendingAutoFollow: cancelHistoryAutoFollow,
        interactionProps: historyAutoFollowInteractionProps,
    } = useConversationAutoFollow({
        scrollerRef: historyContainerRef,
        contentKey: historyScrollAnchor,
        resetKey: activeSession?.id,
        enabled: pcPageActive && activeTab === 'chat' && !!activeSession && !isWritableSession(activeSession),
    });
    useEffect(() => {
        cancelPcAutoFollowRef.current = () => {
            cancelLiveAutoFollow();
            cancelHistoryAutoFollow();
        };
        return () => {
            cancelPcAutoFollowRef.current = () => undefined;
        };
    }, [cancelHistoryAutoFollow, cancelLiveAutoFollow]);
    const scheduleComposerFocus = useCallback(() => {
        let attempts = 0;
        const focusWhenReady = () => {
            const el = chatInputRef.current;
            if (!el || activeTab !== 'chat') {
                if (attempts++ < 8) requestAnimationFrame(focusWhenReady);
                return;
            }
            el.focus({ preventScroll: true });
            const caret = el.value.length;
            try {
                el.setSelectionRange(caret, caret);
            } catch { }
        };
        requestAnimationFrame(focusWhenReady);
    }, [activeTab]);
    const loadMoreHistoryMessages = useCallback(async () => {
        if (historyLoadingMore || historyMoreAbortRef.current || !historyHasMore || !activeSession || !id) return;
        // Cursor pagination: without a cursor we cannot page older, and an empty
        // `before` would re-fetch the newest page → stop instead of looping.
        if (!historyOldestTs) { setHistoryHasMore(false); return; }
        const sess = activeSession;
        const targetAgentId = id;
        const loadSeq = sessionLoadSeqRef.current;
        const writable = isWritableSession(sess);
        const controller = new AbortController();
        historyMoreAbortRef.current = controller;
        setHistoryLoadingMore(true);
        try {
            const tkn = localStorage.getItem('token');
            const params = createConversationHistoryPageParams(historyOldestTs);
            const res = await fetch(`/api/agents/${targetAgentId}/sessions/${sess.id}/message-turns?${params}`, {
                headers: { Authorization: `Bearer ${tkn}` },
                signal: controller.signal,
            });
            if (!res.ok) return;
            const responseCursor = res.headers.get('X-Message-Next-Cursor');
            const responseHasMore = res.headers.get('X-Message-Has-More');
            const msgs = await res.json();
            if (loadSeq !== sessionLoadSeqRef.current || currentAgentIdRef.current !== targetAgentId
                || String(activeSessionIdRef.current) !== String(sess.id)) return;
            if (msgs.length === 0) {
                setHistoryHasMore(false);
                return;
            }
            const preParsed = msgs.map((m: any) => parseChatMsg({
                role: m.role, content: m.content || '',
                ...(Object.prototype.hasOwnProperty.call(m, 'display_content') && { display_content: m.display_content || '' }),
                ...(Object.prototype.hasOwnProperty.call(m, 'attachments') && { attachments: m.attachments || [] }),
                ...(m.quoted_message && { quoted_message: m.quoted_message }),
                ...(m.toolName && { toolName: m.toolName, toolArgs: m.toolArgs, toolStatus: m.toolStatus, toolResult: m.toolResult, toolThinking: m.toolThinking }),
                ...(m.toolCallId && { toolCallId: m.toolCallId }),
                ...(typeof m.toolCallIdExplicit === 'boolean' && { _toolCallIdExplicit: m.toolCallIdExplicit }),
                ...((m.turnAnchorId || m.message_meta?.turn_anchor_id) && { turnAnchorId: m.turnAnchorId || m.message_meta?.turn_anchor_id }),
                ...((m.turnGeneration ?? m.message_meta?.turn_generation) != null && { turnGeneration: m.turnGeneration ?? m.message_meta?.turn_generation }),
                ...((m.producerScope || m.producer_scope || m.message_meta?.producer_scope) && { producerScope: m.producerScope || m.producer_scope || m.message_meta?.producer_scope }),
                ...(m.thinking && { thinking: m.thinking }),
                ...(m.created_at && { timestamp: m.created_at }),
                ...(m.id && { id: m.id }),
                ...(m.sender_name && { sender_name: m.sender_name }),
                ...(m.sender_user_id && { sender_user_id: m.sender_user_id }),
                ...(m.sender_agent_id && { sender_agent_id: m.sender_agent_id }),
            }));
            // Save current scroll position
            const el = writable ? chatContainerRef.current : historyContainerRef.current;
            const oldScrollHeight = el?.scrollHeight ?? 0;
            const oldScrollTop = el?.scrollTop ?? 0;
            const prependPage = (prev: any[]) => {
                const newerMessageIds = new Set(prev.map(m => m.id).filter(Boolean));
                return normalizeChatTimelineMessages([
                    ...preParsed.filter((m: any) => !m.id || !newerMessageIds.has(m.id)),
                    ...prev,
                ]);
            };
            if (writable) setChatMessages(prependPage);
            else setHistoryMsgs(prependPage);
            // Advance the cursor to the oldest row of this (older) page.
            const nextOldestTs = responseCursor || (msgs[0]?.created_at
                ? `${msgs[0].created_at}${msgs[0].id ? `|${msgs[0].id}` : ''}`
                : null);
            setHistoryOldestTs(nextOldestTs);
            setHistoryHasMore(Boolean(
                nextOldestTs
                && nextOldestTs !== historyOldestTs
                && resolveConversationHistoryHasMore(responseHasMore)
            ));
            // Restore scroll position after new messages are prepended
            requestAnimationFrame(() => {
                if (el) {
                    const newScrollHeight = el.scrollHeight;
                    el.scrollTop = oldScrollTop + newScrollHeight - oldScrollHeight;
                }
            });
        } catch (err: any) {
            if (err?.name === 'AbortError') return;
            console.error('Failed to load more history messages:', err);
        } finally {
            if (historyMoreAbortRef.current === controller) historyMoreAbortRef.current = null;
            if (loadSeq === sessionLoadSeqRef.current) setHistoryLoadingMore(false);
        }
    }, [historyLoadingMore, historyHasMore, activeSession, id, historyOldestTs]);

    useEffect(() => {
        if (!activeSession || historyLoadingMore || !historyHasMore || !historyOldestTs) return;
        const el = isWritableSession(activeSession) ? chatContainerRef.current : historyContainerRef.current;
        if (el && el.clientHeight > 0 && el.scrollHeight <= el.clientHeight + 1
            && historyAutoLoadCursorRef.current !== historyOldestTs) {
            historyAutoLoadCursorRef.current = historyOldestTs;
            loadMoreHistoryMessages();
        }
    }, [activeSession?.id, chatMessages.length, historyMsgs.length, historyLoadingMore, historyHasMore, historyOldestTs, loadMoreHistoryMessages]);

    const handleHistoryScroll = () => {
        const el = historyContainerRef.current;
        if (!el) return;
        // Load more when scrolling near the top
        if (el.scrollTop < 100 && historyHasMore && !historyLoadingMore) {
            loadMoreHistoryMessages();
        }
    };
    useEffect(() => {
        if (activeTab === 'chat' && activeSession && isWritableSession(activeSession)) {
            scheduleComposerFocus();
        }
    }, [activeTab, activeSession?.id, scheduleComposerFocus]);
    // Memoized component for each chat message to avoid re-renders while typing

    const handleChatScroll = () => {
        const el = chatContainerRef.current;
        if (!el) return;
        if (el.scrollTop < 100 && historyHasMore && !historyLoadingMore) {
            loadMoreHistoryMessages();
        }
    };

    useEffect(() => {
        const gapAboveComposer = 14;
        const updateScrollButtonOffset = () => {
            const composerAreaHeight = chatInputAreaRef.current?.offsetHeight ?? 82;
            setChatScrollBtnBottom(composerAreaHeight + gapAboveComposer);
        };

        updateScrollButtonOffset();
        if (typeof ResizeObserver === 'undefined' || !chatInputAreaRef.current) return;

        const observer = new ResizeObserver(() => updateScrollButtonOffset());
        observer.observe(chatInputAreaRef.current);
        return () => observer.disconnect();
    }, [activeSession?.id, activeTab, chatUploadDrafts.length, attachedFiles.length]);

    const sendChatMsg = () => {
        if (!id || !activeSession?.id) return;
        if (showNoModelState) return;
        if (isWaiting || isStreaming || isStopping || confirmationPending) return;
        const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        const activeSocket = wsMapRef.current[activeRuntimeKey];
        if (!chatInput.trim() && attachedFiles.length === 0) return;

        const attachmentPayload = buildChatAttachmentPayload({
            input: chatInput.trim(),
            attachments: attachedFiles,
        });

        const payload: PendingChatMessage = {
            runtimeKey: activeRuntimeKey,
            contentForLLM: attachmentPayload.contentForLLM,
            displayContent: attachmentPayload.displayContent,
            fileName: attachmentPayload.fileName,
            imageUrl: attachmentPayload.imageUrl,
            previewImages: attachmentPayload.previewImages,
            attachments: attachmentPayload.attachments,
            modelId: effectiveChatModelId,
            messageId: createClientId(),
        };

        setChatInput('');
        scrollToBottom();
        // Reset textarea height after clearing content
        if (chatInputRef.current) {
            chatInputRef.current.style.height = 'auto';
        }
        dismissedWorkspaceRefPath.current = null;
        setAttachedFiles((prev) => prev.filter((file) => file.source === 'workspace_auto'));

        if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN) {
            pendingChatSendRef.current = payload;
            if (token) ensureSessionSocket(activeSession, id, token);
            setChatInfoMsg('Connection is reconnecting. Your message will be sent automatically.');
            if (chatInfoTimerRef.current) clearTimeout(chatInfoTimerRef.current);
            chatInfoTimerRef.current = setTimeout(() => setChatInfoMsg(null), 4000);
            return;
        }

        dispatchChatMessage(activeSocket, activeRuntimeKey, payload);
    };

    const handleChatFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        if (confirmationPending) {
            e.target.value = '';
            return;
        }
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        const allowedFiles = files.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            toast.warning('最多可附加 10 个文件');
            return;
        }

        const baseTime = Date.now();
        const newDrafts = allowedFiles.map((file, i) => ({
            id: `up-${baseTime}-${i}-${file.name}`,
            name: file.name,
            percent: 0,
            previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
            sizeBytes: file.size,
        }));
        setChatUploadDrafts((prev) => [...prev, ...newDrafts]);

        const runOne = async (file: File, draft: (typeof newDrafts)[0]) => {
            const { promise, abort } = uploadFileWithProgress(
                `/chat/upload`,
                file,
                (pct) => {
                    setChatUploadDrafts((prev) =>
                        prev.map((d) => (d.id === draft.id ? { ...d, percent: pct >= 101 ? 100 : pct } : d)),
                    );
                },
                id ? { agent_id: id } : undefined,
                600_000, // large files: align with nginx /api/ 600s so uploads aren't cut at the 120s default
            );
            chatUploadAbortRef.current.set(draft.id, abort);
            try {
                const data = await promise;
                const uploadedName = data.saved_filename || data.filename || file.name;
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setChatUploadDrafts((prev) => prev.filter((d) => d.id !== draft.id));
                chatUploadAbortRef.current.delete(draft.id);
                setAttachedFiles((prev) =>
                    [...prev, {
                        name: uploadedName,
                        text: data.extracted_text,
                        path: data.workspace_path,
                        imageUrl: data.image_data_url || undefined,
                        mimeType: file.type || undefined,
                        sizeBytes: data.size ?? file.size,
                    }].slice(0, 10),
                );
            } catch (err: any) {
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setChatUploadDrafts((prev) => prev.filter((d) => d.id !== draft.id));
                chatUploadAbortRef.current.delete(draft.id);
                if (err?.message !== 'Upload cancelled') toast.error(t('agent.upload.failed'), { details: String(err?.message || err) });
            }
        };

        await Promise.all(allowedFiles.map((file, i) => runOne(file, newDrafts[i])));
        if (fileInputRef.current) fileInputRef.current.value = '';
    };

    // Clipboard paste handler — auto-upload pasted images
    const handlePaste = async (e: React.ClipboardEvent) => {
        if (confirmationPending) {
            e.preventDefault();
            return;
        }
        const items = e.clipboardData?.items;
        if (!items) return;

        const filesToUpload: File[] = [];
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.startsWith('image/')) {
                const blob = items[i].getAsFile();
                if (blob) {
                    const ext = blob.type.split('/')[1] || 'png';
                    const fileName = `paste-${Date.now()}-${i}.${ext}`;
                    filesToUpload.push(new File([blob], fileName, { type: blob.type }));
                }
            }
        }

        if (!filesToUpload.length) return;
        e.preventDefault();
        const allowedFiles = filesToUpload.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            toast.warning('最多可附加 10 个文件');
            return;
        }

        const baseTime = Date.now();
        const newDrafts = allowedFiles.map((file, i) => ({
            id: `paste-${baseTime}-${i}-${file.name}`,
            name: file.name,
            percent: 0,
            previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
            sizeBytes: file.size,
        }));
        setChatUploadDrafts((prev) => [...prev, ...newDrafts]);

        const runOne = async (file: File, draft: (typeof newDrafts)[0]) => {
            const { promise, abort } = uploadFileWithProgress(
                `/chat/upload`,
                file,
                (pct) => {
                    setChatUploadDrafts((prev) =>
                        prev.map((d) => (d.id === draft.id ? { ...d, percent: pct >= 101 ? 100 : pct } : d)),
                    );
                },
                id ? { agent_id: id } : undefined,
                600_000, // large files: align with nginx /api/ 600s so uploads aren't cut at the 120s default
            );
            chatUploadAbortRef.current.set(draft.id, abort);
            try {
                const data = await promise;
                const uploadedName = data.saved_filename || data.filename || file.name;
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setChatUploadDrafts((prev) => prev.filter((d) => d.id !== draft.id));
                chatUploadAbortRef.current.delete(draft.id);
                setAttachedFiles((prev) =>
                    [...prev, {
                        name: uploadedName,
                        text: data.extracted_text,
                        path: data.workspace_path,
                        imageUrl: data.image_data_url || undefined,
                        mimeType: file.type || undefined,
                        sizeBytes: data.size ?? file.size,
                    }].slice(0, 10),
                );
            } catch (err: any) {
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setChatUploadDrafts((prev) => prev.filter((d) => d.id !== draft.id));
                chatUploadAbortRef.current.delete(draft.id);
                if (err?.message !== 'Upload cancelled') toast.error(t('agent.upload.failed'), { details: String(err?.message || err) });
            }
        };

        await Promise.all(allowedFiles.map((file, i) => runOne(file, newDrafts[i])));
    };

    // ── Drag-and-drop chat file upload ──
    const handleDroppedChatFiles = useCallback(async (files: File[]) => {
        if (confirmationPending || !wsConnected || chatUploadDrafts.length > 0 || isWaiting || isStreaming || isStopping || attachedFiles.length >= 10) return;
        const availableSlots = Math.max(0, 10 - attachedFiles.length);
        const filesToProcess = files.slice(0, availableSlots);

        for (const file of filesToProcess) {
            const draftId = Math.random().toString(36).slice(2, 9);
            const previewUrl = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined;
            setChatUploadDrafts(prev => [...prev, { id: draftId, name: file.name, percent: 0, previewUrl, sizeBytes: file.size }]);

            try {
                const { promise } = uploadFileWithProgress(
                    '/chat/upload',
                    file,
                    (pct) => {
                        setChatUploadDrafts(prev => prev.map(d => d.id === draftId ? { ...d, percent: pct >= 101 ? 100 : pct } : d));
                    },
                    id ? { agent_id: id } : undefined,
                    600_000, // large files: align with nginx /api/ 600s
                );
                const data = await promise;
                setAttachedFiles(prev => [...prev, {
                    name: data.filename,
                    text: data.extracted_text,
                    path: data.workspace_path,
                    imageUrl: data.image_data_url || undefined,
                    mimeType: file.type || undefined,
                    sizeBytes: data.size ?? file.size,
                }]);
            } catch (err: any) {
                if (err?.message !== 'Upload cancelled') {
                    toast.error(t('agent.upload.failed'), { details: String(err?.message || '') });
                }
            } finally {
                if (previewUrl) URL.revokeObjectURL(previewUrl);
                setChatUploadDrafts(prev => prev.filter(d => d.id !== draftId));
            }
        }
    }, [attachedFiles.length, chatUploadDrafts.length, confirmationPending, id, isStopping, isStreaming, isWaiting, t, wsConnected]);

    const { isDragging: isChatDragging, dropZoneProps: chatDropProps } = useDropZone({
        onDrop: handleDroppedChatFiles,
        disabled: confirmationPending || !wsConnected || chatUploadDrafts.length > 0 || isWaiting || isStreaming || isStopping || attachedFiles.length >= 10 || !activeSession || !isWritableSession(activeSession),
    });

    // Expandable activity log
    const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
    const [logFilter, setLogFilter] = useState<string>('user'); // 'user' | 'backend' | 'heartbeat' | 'schedule' | 'messages'

    const { data: backgroundTasks = [] } = useQuery({
        queryKey: ['tasks', id],
        queryFn: () => taskApi.list(id!),
        enabled: !!id && awareDataActive,
        staleTime: 15_000,
    });

    const { data: schedules = [] } = useQuery({
        queryKey: ['schedules', id],
        queryFn: () => scheduleApi.list(id!),
        enabled: !!id && awareDataActive,
        staleTime: 15_000,
    });

    // Schedule form state
    const [showScheduleForm, setShowScheduleForm] = useState(false);
    const schedDefaults = { freq: 'daily', interval: 1, time: '09:00', weekdays: [1, 2, 3, 4, 5] };
    const [schedForm, setSchedForm] = useState({ name: '', instruction: '', schedule: JSON.stringify(schedDefaults), due_date: '' });

    const createScheduleMut = useMutation({
        mutationFn: () => {
            let sched: any;
            try { sched = JSON.parse(schedForm.schedule); } catch { sched = schedDefaults; }
            return scheduleApi.create(id!, { name: schedForm.name, instruction: schedForm.instruction, cron_expr: schedToCron(sched) });
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['schedules', id] });
            setShowScheduleForm(false);
            setSchedForm({ name: '', instruction: '', schedule: JSON.stringify(schedDefaults), due_date: '' });
        },
        onError: (err: any) => {
            const msg = err?.detail || err?.message || String(err);
            toast.error('创建计划任务失败', { details: String(msg) });
        },
    });

    const toggleScheduleMut = useMutation({
        mutationFn: ({ sid, enabled }: { sid: string; enabled: boolean }) =>
            scheduleApi.update(id!, sid, { is_enabled: enabled }),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['schedules', id] }),
    });

    const deleteScheduleMut = useMutation({
        mutationFn: (sid: string) => scheduleApi.delete(id!, sid),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['schedules', id] }),
    });

    const triggerScheduleMut = useMutation({
        mutationFn: async (sid: string) => {
            const res = await scheduleApi.trigger(id!, sid);
            return res;
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['schedules', id] });
            showToast('Schedule triggered — executing in background', 'success');
        },
        onError: (err: any) => {
            const msg = err?.response?.data?.detail || err?.message || 'Failed to trigger schedule';
            showToast(msg, 'error');
        },
    });


    const { data: metrics } = useQuery({
        queryKey: ['metrics', id],
        queryFn: () => agentApi.metrics(id!).catch(() => null),
        enabled: !!id && activeTab === 'status',
        retry: false,
    });

    const { data: channelConfig } = useQuery({
        queryKey: ['channel', id],
        queryFn: () => channelApi.get(id!),
        enabled: !!id && activeTab === 'settings',
    });

    const { data: webhookData } = useQuery({
        queryKey: ['webhook-url', id],
        queryFn: () => channelApi.webhookUrl(id!),
        enabled: !!id && activeTab === 'settings',
    });

    const { data: llmModels = [], isLoading: llmModelsLoading } = useQuery({
        queryKey: ['llm-models'],
        queryFn: () => enterpriseApi.llmModels(),
        enabled: activeTab === 'settings' || activeTab === 'status' || activeTab === 'chat',
        refetchOnMount: 'always',
    });

    useEffect(() => {
        if (activeTab !== 'chat') return;
        queryClient.refetchQueries({ queryKey: ['llm-models'] });
        queryClient.refetchQueries({ queryKey: ['tenant', 'me'] });
    }, [activeTab, location.pathname, queryClient]);

    const enabledLlmModels = useMemo(
        () => (llmModels as any[]).filter((m: any) => m.enabled),
        [llmModels],
    );
    const effectiveChatModelId = overrideModelId
        || agent?.primary_model_id
        || myTenant?.default_model_id
        || enabledLlmModels[0]?.id
        || null;

    const enabledModelCount = enabledLlmModels.length;
    const effectiveModelReady = !!effectiveChatModelId && enabledLlmModels.some((m: any) => m.id === effectiveChatModelId);

    const handleOnboardingStart = useCallback(() => {
        setIsWaiting(true);
        setIsStreaming(false);
    }, []);

    useOnboardingKickoff({
        request: onboardingKickoffRequest,
        activeSessionId: activeSession?.id,
        effectiveModelId: effectiveChatModelId,
        enabled: wsConnected && !llmModelsLoading && effectiveModelReady,
        onStart: handleOnboardingStart,
    });

    const { data: permData } = useQuery({
        queryKey: ['agent-permissions', id],
        queryFn: () => fetchAuth<any>(`/agents/${id}/permissions`),
        enabled: !!id && activeTab === 'settings',
    });

    // ─── Soul editor ─────────────────────────────────────
    const [soulEditing, setSoulEditing] = useState(false);
    const [soulDraft, setSoulDraft] = useState('');

    const saveSoul = useMutation({
        mutationFn: () => fileApi.write(id!, 'soul.md', soulDraft),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['file', id, 'soul.md'] });
            setSoulEditing(false);
        },
    });


    const CopyBtn = ({ url }: { url: string }) => (
        <button title="Copy" style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', marginLeft: '6px', padding: '1px 4px', cursor: 'pointer', borderRadius: '3px', border: '1px solid var(--border-color)', background: 'var(--bg-primary)', color: 'var(--text-secondary)', verticalAlign: 'middle', lineHeight: 1 }}
            onClick={() => copyToClipboard(url).then(() => { })}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <rect x="4" y="4" width="9" height="11" rx="1.5" /><path d="M3 11H2a1 1 0 01-1-1V2a1 1 0 011-1h8a1 1 0 011 1v1" />
            </svg>
        </button>
    );

    // ─── File viewer ─────────────────────────────────────
    const [viewingFile, setViewingFile] = useState<string | null>(null);
    const [fileEditing, setFileEditing] = useState(false);
    const [fileDraft, setFileDraft] = useState('');
    const [promptModal, setPromptModal] = useState<{ title: string; placeholder: string; action: string } | null>(null);
    const [deleteConfirm, setDeleteConfirm] = useState<{ path: string; name: string; isDir: boolean } | null>(null);
    const [uploadToast, setUploadToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
    const [editingRole, setEditingRole] = useState(false);
    const [roleInput, setRoleInput] = useState('');
    const [editingName, setEditingName] = useState(false);
    const [nameInput, setNameInput] = useState('');
    const [infoCardOpen, setInfoCardOpen] = useState(false);
    const infoCardCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const clearCardCloseTimer = () => { if (infoCardCloseTimer.current) { clearTimeout(infoCardCloseTimer.current); infoCardCloseTimer.current = null; } };
    const scheduleCardClose = () => { clearCardCloseTimer(); infoCardCloseTimer.current = setTimeout(() => setInfoCardOpen(false), 180); };
    const showToast = (message: string, type: 'success' | 'error' = 'success') => {
        setUploadToast({ message, type });
        setTimeout(() => setUploadToast(null), 3000);
    };
    const { data: fileContent } = useQuery({
        queryKey: ['file-content', id, viewingFile],
        queryFn: () => fileApi.read(id!, viewingFile!),
        enabled: !!viewingFile,
    });

    // ─── Task creation & detail ───────────────────────────────────
    const [showTaskForm, setShowTaskForm] = useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [taskForm, setTaskForm] = useState({ title: '', description: '', priority: 'medium', type: 'todo' as 'todo' | 'supervision', supervision_target_user_id: '', supervision_target_agent_id: '', supervision_channel: '', remind_schedule: '', due_date: '' });
    const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
    const { data: taskLogs = [] } = useQuery({
        queryKey: ['task-logs', id, selectedTaskId],
        queryFn: () => taskApi.getLogs(id!, selectedTaskId!),
        enabled: !!id && !!selectedTaskId,
        refetchInterval: selectedTaskId ? 3000 : false,
    });

    // Schedule execution history (selectedTaskId format: 'sched-{uuid}')
    const expandedScheduleId = selectedTaskId?.startsWith('sched-') ? selectedTaskId.slice(6) : null;
    const { data: scheduleHistoryData } = useQuery({
        queryKey: ['schedule-history', id, expandedScheduleId],
        queryFn: () => scheduleApi.history(id!, expandedScheduleId!),
        enabled: !!id && !!expandedScheduleId,
    });
    const createTask = useMutation({
        mutationFn: (data: any) => {
            const cleaned = { ...data };
            if (!cleaned.due_date) delete cleaned.due_date;
            return taskApi.create(id!, cleaned);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['tasks', id] });
            setShowTaskForm(false);
            setTaskForm({ title: '', description: '', priority: 'medium', type: 'todo', supervision_target_user_id: '', supervision_target_agent_id: '', supervision_channel: '', remind_schedule: '', due_date: '' });
        },
    });

    if (isLoading || !agent) {
        return <div style={{ padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>;
    }

    // Compute display status (including OpenClaw disconnected detection)
    const computeStatusKey = () => {
        if (agent.status === 'error') return 'error';
        if (agent.status === 'creating') return 'creating';
        if (agent.status === 'stopped') return 'stopped';
        if ((agent as any).agent_type === 'openclaw' && agent.status === 'running' && (agent as any).openclaw_last_seen) {
            const elapsed = Date.now() - new Date((agent as any).openclaw_last_seen).getTime();
            if (elapsed > 60 * 60 * 1000) return 'disconnected';
        }
        return agent.status === 'running' ? 'running' : 'idle';
    };
    const statusKey = computeStatusKey();
    const canManage = (agent as any).access_level === 'manage';
    const formatAgentDate = (d?: string | null) => {
        if (!d) return '—';
        try { return new Date(d).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); } catch { return d; }
    };
    const primaryModel = llmModels.find((m: any) => m.id === agent.primary_model_id);
    const showNoModelState = !llmModelsLoading && (agent as any).agent_type !== 'openclaw' && (enabledModelCount === 0 || !effectiveModelReady);
    const canConfigureModels = currentUser?.role === 'platform_admin' || currentUser?.role === 'org_admin' || !!(currentUser as any)?.is_platform_admin;
    const renderNoModelGuide = (variant: 'empty' | 'floating' = 'empty') => (
        <div className={`chat-no-model-state${variant === 'floating' ? ' chat-no-model-state--floating' : ''}`}>
            <div className="chat-no-model-state__icon"><IconAlertTriangle size={20} stroke={1.8} /></div>
            <div className="chat-no-model-state__title">{t('agent.chat.noModelTitle', 'No company model configured')}</div>
            <div className="chat-no-model-state__text">
                {canConfigureModels
                    ? t('agent.chat.noModelAdmin', 'Configure a company model before chatting with this assistant.')
                    : t('agent.chat.noModelMember', 'This company has not configured a model yet. Please contact an administrator.')}
            </div>
            {canConfigureModels && (
                <button className="btn btn-primary" onClick={() => navigate('/enterprise#llm')}>
                    <IconSettings size={15} stroke={1.75} />
                    {t('agent.chat.goModelSettings', 'Go to model management')}
                </button>
            )}
        </div>
    );
    const modelLabel = primaryModel ? (primaryModel.label || primaryModel.model) : '—';
    const modelProvider = primaryModel ? primaryModel.provider : '—';
    const todayParts = formatTokensParts(agent.tokens_used_today || 0);
    const monthParts = formatTokensParts(agent.tokens_used_month || 0);
    const totalParts = formatTokensParts((agent as any).tokens_used_total || 0);
    const cacheReadToday = (agent as any).cache_read_tokens_today || metrics?.tokens?.cache_read_today || 0;
    const cacheReadMonth = (agent as any).cache_read_tokens_month || metrics?.tokens?.cache_read_month || 0;
    const cacheReadTotal = (agent as any).cache_read_tokens_total || metrics?.tokens?.cache_read_total || 0;
    const cacheHitRateToday = (agent.tokens_used_today || 0) > 0 ? Math.round((cacheReadToday / (agent.tokens_used_today || 1)) * 100) : 0;
    const cacheHitRateMonth = (agent.tokens_used_month || 0) > 0 ? Math.round((cacheReadMonth / (agent.tokens_used_month || 1)) * 100) : 0;
    const cacheHitRateTotal = ((agent as any).tokens_used_total || 0) > 0 ? Math.round((cacheReadTotal / ((agent as any).tokens_used_total || 1)) * 100) : 0;
    const expiryLabel = (agent as any).is_expired
        ? t('agent.settings.expiry.expired')
        : (agent as any).expires_at
            ? new Date((agent as any).expires_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
            : t('agent.settings.expiry.neverExpires');
    const renderAgentInfoCard = () => (
        <div className={`agent-info-card${infoCardOpen ? ' agent-info-card--open' : ''}`}>
            <div className="agent-info-card-inner">
                <div className="agent-info-card-glow" />
                <div className="agent-info-card-grid">
                    {/* Agent Profile */}
                    <div className="agent-info-card-section">
                        <div className="agent-info-card-section-header">
                            <span className="agent-info-section-icon agent-info-section-icon--indigo">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="8" r="4"/><path d="M20 21a8 8 0 0 0-16 0"/></svg>
                            </span>
                            <span className="agent-info-card-section-title">{t('agent.profile.title', 'Agent Profile')}</span>
                        </div>
                        <div className="agent-info-card-body">
                            <div className="agent-info-profile-panel">
                                {agent.role_description && (
                                    <div className="agent-info-profile-role" title={agent.role_description}>{agent.role_description}</div>
                                )}
                                <div className="agent-info-meta-list agent-info-profile-meta">
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.profile.created')}</span>
                                        <span>{formatAgentDate(agent.created_at)}</span>
                                    </div>
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.fields.createdBy', 'Created by')}</span>
                                        <span>{(agent as any).creator_username ? `@${(agent as any).creator_username}` : '—'}</span>
                                    </div>
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.profile.timezone')}</span>
                                        <span>{(agent as any).effective_timezone || agent.timezone || 'UTC'}</span>
                                    </div>
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.settings.expiry.title')}</span>
                                        <span className={(agent as any).is_expired ? 'agent-info-expiry--expired' : ''}>{expiryLabel}</span>
                                    </div>
                                </div>
                                {canManage && (
                                    <button
                                        type="button"
                                        className="agent-info-expiry-button"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            openExpiryModal();
                                        }}
                                    >
                                        {t('agent.settings.expiry.title')}
                                    </button>
                                )}
                            </div>
                        </div>
                    </div>
                    <div className="agent-info-card-section agent-info-card-section--stacked">
                        {/* Model Configuration */}
                        <div className="agent-info-subsection">
                            <div className="agent-info-card-section-header">
                                <span className="agent-info-section-icon agent-info-section-icon--indigo">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
                                </span>
                                <span className="agent-info-card-section-title">{t('agent.modelConfig.title', 'Configuration')}</span>
                            </div>
                            <div className="agent-info-card-body agent-info-card-body--compact">
                                <div className="agent-info-model-card">
                                    <div className="agent-info-model-card-text">
                                        <span className="agent-info-model-card-label">{t('agent.modelConfig.model')}</span>
                                        <span className="agent-info-model-card-name" title={modelLabel}>{modelLabel}</span>
                                    </div>
                                </div>
                                <div className="agent-info-meta-list">
                                    <div className="agent-info-meta-row">
                                        <span>{t('agent.modelConfig.provider', 'Provider')}</span>
                                        <span>{modelProvider}</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                        {/* Token Usage */}
                        <div className="agent-info-subsection">
                            <div className="agent-info-card-section-header">
                                <span className="agent-info-section-icon agent-info-section-icon--blue">
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
                                </span>
                                <span className="agent-info-card-section-title">Token</span>
                            </div>
                            <div className="agent-info-card-body agent-info-card-body--compact">
                                <div className="agent-info-token-glass">
                                    <div className="agent-info-token-hero">
                                        <span className="agent-info-token-hero-label">{t('agent.settings.today')}</span>
                                        <span className="agent-info-token-hero-value">
                                            {todayParts.value}
                                            {todayParts.unit && <span className="agent-info-token-hero-unit">{todayParts.unit}</span>}
                                        </span>
                                    </div>
                                    <div className="agent-info-token-stats">
                                        <div className="agent-info-stat-item">
                                            <span className="agent-info-stat-label">{t('agent.settings.month')}</span>
                                            <span className="agent-info-stat-value">
                                                {monthParts.value}
                                                {monthParts.unit && <span className="agent-info-stat-unit">{monthParts.unit}</span>}
                                            </span>
                                        </div>
                                        <div className="agent-info-stat-item">
                                            <span className="agent-info-stat-label">Cache</span>
                                            <span className="agent-info-stat-value" title={`Today cache hit: ${formatTokens(cacheReadToday)} · ${cacheHitRateToday}%`}>
                                                {formatTokens(cacheReadToday)}
                                                <span className="agent-info-stat-unit">{cacheHitRateToday}%</span>
                                            </span>
                                        </div>
                                        <div className="agent-info-stat-item">
                                            <span className="agent-info-stat-label">{t('agent.status.totalToken')}</span>
                                            <span className="agent-info-stat-value">
                                                {totalParts.value}
                                                {totalParts.unit && <span className="agent-info-stat-unit">{totalParts.unit}</span>}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
    const renderAwarePreview = () => {
        const focusItems = focusRecords.map(focusItemFromApi);
        const isZh = i18n.language?.startsWith('zh');
        const formatTrigger = (trig: any) => {
            if (trig.type === 'cron' && trig.config?.expr) return `Cron ${trig.config.expr}`;
            if (trig.type === 'interval' && trig.config?.minutes) return isZh ? `每 ${trig.config.minutes} 分钟` : `Every ${trig.config.minutes} min`;
            if (trig.type === 'once' && trig.config?.at) return new Date(trig.config.at).toLocaleString();
            return trig.name || trig.type;
        };
        const triggerTitle = (trig: any) => String(trig.reason || trig.name || trig.type || '').trim();
        const triggerMeta = (trig: any) => {
            const schedule = formatTrigger(trig);
            if (!trig.reason || schedule === trig.reason) return schedule;
            return schedule;
        };
        const triggerTooltip = (trig: any) => {
            const title = triggerTitle(trig);
            const meta = triggerMeta(trig);
            const parts = [title, meta];
            if (trig.reason && trig.reason !== title) parts.push(String(trig.reason));
            if (trig.name && trig.name !== title) parts.push(String(trig.name));
            return Array.from(new Set(parts.filter(Boolean))).join('\n');
        };
        const triggersByFocus: Record<string, any[]> = {};
        const focusNames = new Set(focusItems.map((item) => item.name));
        for (const trig of awareTriggers as any[]) {
            if (trig.focus_ref && focusNames.has(trig.focus_ref)) {
                if (!triggersByFocus[trig.focus_ref]) triggersByFocus[trig.focus_ref] = [];
                triggersByFocus[trig.focus_ref].push(trig);
            } else {
                const synthetic = synthesizeFocusForTrigger(trig);
                if (!triggersByFocus[synthetic.name]) triggersByFocus[synthetic.name] = [];
                triggersByFocus[synthetic.name].push(trig);
            }
        }
        const displayFocusItems = focusItems;
        const activeFocusItems = displayFocusItems.filter(item => !item.done && !item.system);
        const systemFocusItems = displayFocusItems.filter(item => !item.done && item.system);
        const completedFocusItems = displayFocusItems.filter(item => item.done);
        const renderTriggerDot = (done: boolean, label: string) => (
            <span className={`aware-side-status-dot ${done ? 'done' : 'active'}`} aria-label={label} />
        );
        const renderFocusItem = (item: FocusItem) => {
            const isExpanded = expandedFocusIds.has(item.id);
            const itemTriggers = triggersByFocus[item.name] || [];
            
            const hasTitle = !!item.title;
            const displayTitle = hasTitle ? item.title : item.name;
            const displaySubtitle = hasTitle ? item.name : null;
            const displayDescription = item.description;

            return (
                <div key={item.id} className={`aware-side-focus ${item.done ? 'done' : ''}`}>
                    <button className="aware-side-focus-head" type="button" onClick={() => toggleExpandedFocus(item.id)}>
                        <div className="aware-side-trigger-main">
                            <div className="aware-side-item-title" style={{ fontWeight: 500 }}>
                                <span>{displayTitle}</span>
                                {item.done && (
                                    <span className="aware-side-focus-badge done">
                                        {t('agent.aware.completed')}
                                    </span>
                                )}
                            </div>
                            {displaySubtitle && (
                                <div className="aware-side-item-meta" style={{ fontFamily: 'monospace' }}>
                                    {displaySubtitle}
                                </div>
                            )}
                            {displayDescription && (
                                <div className="aware-side-item-desc">
                                    {displayDescription}
                                </div>
                            )}
                        </div>
                        <span className="aware-side-count">
                            {isZh ? `${itemTriggers.length} 个` : itemTriggers.length}
                        </span>
                        <span className={`aware-side-chevron ${isExpanded ? 'open' : ''}`}>▶</span>
                    </button>
                    {isExpanded && (
                        <div className="aware-side-nested">
                            {itemTriggers.length === 0 ? (
                                <div className="aware-side-empty compact">{t('agent.aware.noTriggers')}</div>
                            ) : itemTriggers.map((trig: any) => (
                                <div key={trig.id} className={`aware-side-trigger ${trig.is_enabled ? '' : 'done'}`}>
                                    {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                    <div className="aware-side-trigger-main">
                                        <div className="aware-side-item-title">{triggerTitle(trig)}</div>
                                        <div className="aware-side-item-meta">{triggerMeta(trig)}</div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            );
        };
        const SIDE_FOCUS_LIMIT = 12;
        const renderFocusGroup = (
            title: string,
            items: FocusItem[],
            showAll: boolean,
            setShowAll: (val: boolean) => void,
        ) => {
            if (items.length === 0) return null;
            const hasMore = items.length > SIDE_FOCUS_LIMIT;
            const visibleItems = showAll ? items : items.slice(0, SIDE_FOCUS_LIMIT);
            return (
                <div className="aware-side-focus-group">
                    <div className="aware-side-subtitle" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span>{title}</span>
                        {hasMore && (
                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                {showAll ? '' : `${SIDE_FOCUS_LIMIT}/${items.length}`}
                            </span>
                        )}
                    </div>
                    {visibleItems.map(renderFocusItem)}
                    {hasMore && (
                        <button
                            type="button"
                            className="aware-side-collapse"
                            onClick={() => setShowAll(!showAll)}
                            style={{ marginTop: '4px', width: '100%', textAlign: 'center', borderTop: '1px dashed var(--border-subtle)', paddingTop: '6px' }}
                        >
                            <span>
                                {showAll
                                    ? (isZh ? '收起' : 'Show less')
                                    : (isZh ? `显示更多 (+${items.length - SIDE_FOCUS_LIMIT})` : `Show more (+${items.length - SIDE_FOCUS_LIMIT})`)
                                }
                            </span>
                        </button>
                    )}
                </div>
            );
        };
        const parseTriggerTime = (trig: any): Date | null => {
            if (trig.type === 'once' && trig.config?.at) {
                const date = new Date(trig.config.at);
                return Number.isNaN(date.getTime()) ? null : date;
            }
            return null;
        };
        const startOfDay = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
        const today = startOfDay(new Date());
        const calendarAnchor = startOfDay(awareCalendarDate);
        const calendarDays = (() => {
            if (awareCalendarMode === 'day') return [calendarAnchor];
            if (awareCalendarMode === 'month') {
                const first = new Date(calendarAnchor.getFullYear(), calendarAnchor.getMonth(), 1);
                return Array.from({ length: 31 }, (_, idx) => new Date(first.getFullYear(), first.getMonth(), first.getDate() + idx))
                    .filter(date => date.getMonth() === first.getMonth());
            }
            const weekStart = new Date(calendarAnchor);
            weekStart.setDate(calendarAnchor.getDate() - ((calendarAnchor.getDay() + 6) % 7));
            return Array.from({ length: 7 }, (_, idx) => new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + idx));
        })();
        const calendarRangeLabel = (() => {
            if (awareCalendarMode === 'day') {
                return calendarAnchor.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', weekday: 'short' });
            }
            if (awareCalendarMode === 'month') {
                return calendarAnchor.toLocaleDateString(undefined, { year: 'numeric', month: 'long' });
            }
            const first = calendarDays[0];
            const last = calendarDays[calendarDays.length - 1];
            return `${first.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} - ${last.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}`;
        })();
        const shiftCalendar = (direction: -1 | 1) => {
            setAwareCalendarDate(prev => {
                const next = new Date(prev);
                if (awareCalendarMode === 'day') next.setDate(next.getDate() + direction);
                else if (awareCalendarMode === 'week') next.setDate(next.getDate() + direction * 7);
                else next.setMonth(next.getMonth() + direction);
                return next;
            });
        };
        const timedTriggers = (awareTriggers as any[]).filter((trig) => ['once', 'cron', 'interval'].includes(trig.type));
        const recurringTriggers = timedTriggers.filter((trig) => !parseTriggerTime(trig));
        const triggersForDay = (day: Date) => timedTriggers.filter((trig) => {
            const when = parseTriggerTime(trig);
            return !!when && startOfDay(when).getTime() === day.getTime();
        });
        const renderCalendar = () => (
            <div className="aware-calendar">
                <div className="aware-calendar-header">
                    <div className="aware-calendar-toolbar">
                        {(['day', 'week', 'month'] as const).map(mode => (
                            <button
                                key={mode}
                                type="button"
                                className={`aware-view-button ${awareCalendarMode === mode ? 'active' : ''}`}
                                onClick={() => setAwareCalendarMode(mode)}
                            >
                                {isZh ? ({ day: '日', week: '周', month: '月' } as const)[mode] : mode}
                            </button>
                        ))}
                    </div>
                    <div className="aware-calendar-nav">
                        <button type="button" className="aware-calendar-nav-button" onClick={() => shiftCalendar(-1)} aria-label={isZh ? '上一段时间' : 'Previous'}>
                            ‹
                        </button>
                        <button type="button" className="aware-calendar-range" onClick={() => setAwareCalendarDate(new Date())}>
                            {calendarRangeLabel}
                        </button>
                        <button type="button" className="aware-calendar-nav-button" onClick={() => shiftCalendar(1)} aria-label={isZh ? '下一段时间' : 'Next'}>
                            ›
                        </button>
                    </div>
                </div>
                <div className={`aware-calendar-grid mode-${awareCalendarMode}`}>
                    {calendarDays.map((day) => {
                        const items = triggersForDay(day);
                        const isToday = day.getTime() === today.getTime();
                        return (
                            <div key={day.toISOString()} className={`aware-calendar-day ${isToday ? 'is-today' : ''}`}>
                                <div className="aware-calendar-day-label">
                                    {day.toLocaleDateString(undefined, awareCalendarMode === 'month' ? { day: 'numeric' } : { weekday: 'short', month: 'numeric', day: 'numeric' })}
                                    {isToday && <span className="aware-calendar-today-pill">{isZh ? '今天' : 'Today'}</span>}
                                </div>
                                {items.length === 0 ? (
                                    <div className="aware-calendar-empty">-</div>
                                ) : items.slice(0, 3).map((trig: any) => (
                                    <div key={trig.id} className="aware-calendar-event" data-tooltip={triggerTooltip(trig)} aria-label={triggerTooltip(trig)}>
                                        {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                        <span className="aware-calendar-event-body">
                                            <span className="aware-calendar-event-title">{triggerTitle(trig)}</span>
                                            <span className="aware-calendar-event-meta">{triggerMeta(trig)}</span>
                                        </span>
                                    </div>
                                ))}
                                {items.length > 3 && <div className="aware-calendar-more">+{items.length - 3}</div>}
                            </div>
                        );
                    })}
                </div>
                {recurringTriggers.length > 0 && (
                    <div className="aware-calendar-recurring">
                        <div className="aware-side-subtitle">{isZh ? '重复计划' : 'Recurring'}</div>
                        {recurringTriggers.slice(0, 6).map((trig: any) => (
                            <div key={trig.id} className="aware-calendar-event recurring" data-tooltip={triggerTooltip(trig)} aria-label={triggerTooltip(trig)}>
                                {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                <span className="aware-calendar-event-body">
                                    <span className="aware-calendar-event-title">{triggerTitle(trig)}</span>
                                    <span className="aware-calendar-event-meta">{triggerMeta(trig)}</span>
                                </span>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        );
        return (
            <div className="aware-side-preview">
                <div className="aware-side-section">
                    <div className="aware-side-title-row">
                        <div className="aware-side-section-title">{t('agent.aware.focus')}</div>
                        <div className="aware-view-switch">
                            <button
                                type="button"
                                className={`aware-view-button ${awareView === 'list' ? 'active' : ''}`}
                                onClick={() => setAwareView('list')}
                            >
                                {isZh ? '列表' : 'List'}
                            </button>
                            <button
                                type="button"
                                className={`aware-view-button ${awareView === 'calendar' ? 'active' : ''}`}
                                onClick={() => setAwareView('calendar')}
                            >
                                {isZh ? '日历' : 'Calendar'}
                            </button>
                        </div>
                    </div>
                    {awareView === 'calendar' ? renderCalendar() : (
                        displayFocusItems.length === 0 ? (
                            <div className="aware-side-empty">{t('agent.aware.focusEmpty')}</div>
                        ) : (
                            <>
                                {renderFocusGroup(isZh ? '进行中' : 'In progress', activeFocusItems, showAllSideActive, setShowAllSideActive)}
                                {renderFocusGroup(isZh ? '系统 Focus' : 'System Focus', systemFocusItems, showAllSideSystem, setShowAllSideSystem)}
                                {completedFocusItems.length > 0 && (
                                    <div className="aware-side-focus-group">
                                        <button
                                            type="button"
                                            className="aware-side-collapse"
                                            onClick={() => { setShowCompletedFocus(!showCompletedFocus); setShowAllSideCompleted(false); }}
                                        >
                                            <span>{showCompletedFocus ? (isZh ? '收起已完成' : 'Hide completed') : (isZh ? `已完成 (${completedFocusItems.length})` : `Completed (${completedFocusItems.length})`)}</span>
                                            <span className={`aware-side-chevron ${showCompletedFocus ? 'open' : ''}`}>▶</span>
                                        </button>
                                        {showCompletedFocus && (
                                            <>
                                                {(showAllSideCompleted ? completedFocusItems : completedFocusItems.slice(0, SIDE_FOCUS_LIMIT)).map(renderFocusItem)}
                                                {completedFocusItems.length > SIDE_FOCUS_LIMIT && (
                                                    <button
                                                        type="button"
                                                        className="aware-side-collapse"
                                                        onClick={() => setShowAllSideCompleted(!showAllSideCompleted)}
                                                        style={{ marginTop: '4px', width: '100%', textAlign: 'center', borderTop: '1px dashed var(--border-subtle)', paddingTop: '6px' }}
                                                    >
                                                        <span>
                                                            {showAllSideCompleted
                                                                ? (isZh ? '收起' : 'Show less')
                                                                : (isZh ? `显示更多 (+${completedFocusItems.length - SIDE_FOCUS_LIMIT})` : `Show more (+${completedFocusItems.length - SIDE_FOCUS_LIMIT})`)
                                                            }
                                                        </span>
                                                    </button>
                                                )}
                                            </>
                                        )}
                                    </div>
                                )}
                            </>
                        )
                    )}
                </div>
                <div className="aware-side-section">
                    <div className="aware-side-section-title">{isZh ? '执行记录' : 'Executions'}</div>
                    {(reflectionSessions as any[]).length === 0 ? (
                        <div className="aware-side-empty">{isZh ? '暂无执行记录' : 'No executions yet'}</div>
                    ) : (reflectionSessions as any[]).slice(0, 10).map((session: any) => {
                        const recordId = session.record_id || session.id;
                        const conversationId = session.conversation_id || (!session.conversation_missing ? session.id : null);
                        const isExpanded = expandedReflection === recordId;
                        const msgs = conversationId ? (reflectionMessages[conversationId] || []) : [];
                        return (
                            <div key={recordId} className="aware-side-reflection">
                                <button
                                    type="button"
                                    className="aware-side-reflection-head"
                                    onClick={async () => {
                                        if (isExpanded) {
                                            setExpandedReflection(null);
                                            return;
                                        }
                                        setExpandedReflection(recordId);
                                        if (conversationId) await loadReflectionMessages(conversationId);
                                    }}
                                >
                                    <span className="aware-side-dot active" />
                                    <div className="aware-side-trigger-main">
                                        <div className="aware-side-item-title">
                                            {session.execution
                                                ? `${session.execution.trigger_name} · ${session.execution.source}`
                                                : formatReflectionTitle(session.title, !!isZh)}
                                        </div>
                                        <div className="aware-side-item-meta">
                                            {new Date(session.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                            {session.message_count > 0 ? ` · ${session.message_count}` : ''}
                                        </div>
                                    </div>
                                    <span className={`aware-side-chevron ${isExpanded ? 'open' : ''}`}>▶</span>
                                </button>
                                {isExpanded && (
                                    <div className="aware-side-reflection-detail">
                                        {!conversationId ? (
                                            <ConversationTimeline
                                                agentId={id!}
                                                agentName={agent.name || 'Agent'}
                                                messages={[]}
                                                provenance={session.execution}
                                                onOpenSubagentSession={openSubagentSession}
                                                viewOf={() => ({ isLeft: true })}
                                            />
                                        ) : msgs.length === 0 ? (
                                            <div className="aware-side-empty compact">{isZh ? '正在加载...' : 'Loading...'}</div>
                                        ) : (
                                            <ConversationTimeline
                                                agentId={id!}
                                                agentName={agent.name || 'Agent'}
                                                messages={msgs}
                                                provenance={session.execution}
                                                viewOf={(message) => ({
                                                    isLeft: message.role !== 'user',
                                                    senderLabel: message.role === 'user'
                                                        ? (isZh ? '触发事件' : 'Trigger event')
                                                        : (agent.name || 'Agent'),
                                                    avatarText: message.role === 'user' ? 'T' : (agent.name || 'A')[0],
                                                    forceSenderLabel: true,
                                                })}
                                                unavailableAttachmentKeys={unavailableAttachmentKeys}
                                                onAttachmentDownload={handleAttachmentDownload}
                                                onAttachmentUnavailable={markAttachmentUnavailable}
                                                onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                                onOpenSubagentSession={openSubagentSession}
                                                onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                            />
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>
        );
    };

    return (
        <>
            <div className={`agent-detail-page ${activeTab === 'chat' ? 'agent-detail-page--chat' : 'agent-detail-page--settings'}`}>
                {/* Header */}
                {activeTab === 'chat' && (
                    <div className="page-header agent-detail-header">
                        <div
                            className="agent-detail-identity agent-detail-identity--compact"
                            onMouseEnter={clearCardCloseTimer}
                            onMouseLeave={scheduleCardClose}
                        >
                            <div className="agent-detail-identity-trigger">
                            <div className="agent-detail-avatar" style={{ overflow: 'hidden' }}>
                                {agent.avatar_url ? (
                                    <img
                                        src={agent.avatar_url.startsWith('/api') ? `${agent.avatar_url}${agent.avatar_url.includes('?') ? '&' : '?'}token=${token}` : agent.avatar_url}
                                        alt=""
                                        style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                                    />
                                ) : (
                                    (Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()
                                )}
                            </div>
                            <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
                                {canManage && editingName ? (
                                    <input
                                        className="page-title"
                                        autoFocus
                                        value={nameInput}
                                        onChange={e => setNameInput(e.target.value)}
                                        onBlur={async () => {
                                            setEditingName(false);
                                            if (nameInput.trim() && nameInput !== agent.name) {
                                                await agentApi.update(id!, { name: nameInput.trim() } as any);
                                                queryClient.invalidateQueries({ queryKey: ['agent', id] });
                                            } else {
                                                setNameInput(agent.name);
                                            }
                                        }}
                                        onKeyDown={async e => {
                                            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                                            if (e.key === 'Escape') { setEditingName(false); setNameInput(agent.name); }
                                        }}
                                        style={{
                                            background: 'var(--bg-elevated)', border: '1px solid var(--accent-primary)',
                                            borderRadius: '6px', color: 'var(--text-primary)',
                                            padding: '4px 10px', minWidth: '320px', width: 'auto', outline: 'none',
                                            marginBottom: '0', display: 'block',
                                        }}
                                    />
                                ) : (
                                    <h1 className="page-title"
                                        title={canManage ? "Click to edit name" : undefined}
                                        onClick={() => { if (canManage) { setNameInput(agent.name); setEditingName(true); } }}
                                        style={{ cursor: canManage ? 'text' : 'default', borderBottom: canManage ? '1px dashed transparent' : 'none', display: 'inline-block', marginBottom: '0' }}
                                        onMouseEnter={e => { if (canManage) e.currentTarget.style.borderBottomColor = 'var(--text-tertiary)'; }}
                                        onMouseLeave={e => { if (canManage) e.currentTarget.style.borderBottomColor = 'transparent'; }}
                                    >
                                        {agent.name}
                                    </h1>
                                )}
                            </div>
                            <button
                                className={`agent-info-chevron${infoCardOpen ? ' agent-info-chevron--open' : ''}`}
                                onClick={e => { e.stopPropagation(); setInfoCardOpen(prev => !prev); }}
                                aria-label="Toggle agent info"
                            >
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                            </button>
                            </div>
                            {renderAgentInfoCard()}
                        </div>
                        <div className="agent-detail-actions">
                            <>
                                <button
                                    className={`btn btn-ghost agent-top-action ${livePanelVisible && sidePanelTab === 'workspace' ? 'active' : ''}`}
                                    onClick={() => togglePreviewPanel('workspace')}
                                >
                                    <IconFolder size={16} stroke={1.7} />
                                    <span>{t('agent.tabs.workspace')}</span>
                                </button>
                                {(agent as any)?.agent_type !== 'openclaw' && (
                                    <button
                                        className={`btn btn-ghost agent-top-action ${(isSettingsRoute && location.hash === '#aware') || (livePanelVisible && sidePanelTab === 'aware') ? 'active' : ''}`}
                                        onClick={() => isChatRoute ? togglePreviewPanel('aware') : setActiveTab('aware')}
                                    >
                                        <IconBrain size={16} stroke={1.7} />
                                        <span>{t('agent.tabs.aware')}</span>
                                    </button>
                                )}
                                <button
                                    className={`btn btn-ghost agent-top-action ${isSettingsRoute ? 'active' : ''}`}
                                    onClick={() => navigate(`/agents/${id}/settings`)}
                                >
                                    <IconSettings size={16} stroke={1.7} />
                                    <span>{t('agent.tabs.settings')}</span>
                                </button>
                            </>
                            {(agent as any)?.agent_type !== 'openclaw' && (
                                <>
                                    {canManage && agent.status === 'stopped' && (
                                        <button className="btn btn-secondary" onClick={async () => { await agentApi.start(id!); queryClient.invalidateQueries({ queryKey: ['agent', id] }); }}>{t('agent.actions.start')}</button>
                                    )}
                                    {canManage && agent.status === 'running' && (
                                        <button className="btn btn-secondary" onClick={async () => { await agentApi.stop(id!); queryClient.invalidateQueries({ queryKey: ['agent', id] }); }}>{t('agent.actions.stop')}</button>
                                    )}
                                </>
                            )}
                        </div>
                    </div>
                )}

                {/* Tabs */}
                {activeTab !== 'chat' && <div className="tabs">
                    {AGENT_DETAIL_TABS.filter(tab => {
                        if (['workspace', 'chat'].includes(tab)) return false;
                        if (tab === 'scenes' && (!canManage || !agent?.scene_config_enabled)) return false;
                        // 'use' access keeps the existing tab bar unchanged; settings remains available via its own entry.
                        if ((agent as any)?.access_level === 'use') {
                            if (tab === 'settings' || tab === 'approvals') return false;
                        }
                        // OpenClaw agents: only show status, chat, activityLog, settings
                        if ((agent as any)?.agent_type === 'openclaw') {
                            return ['status', 'relationships', 'chat', 'activityLog', 'settings'].includes(tab);
                        }
                        return true;
                    }).map((tab) => (
                        <div key={tab} className={`tab ${activeTab === tab ? 'active' : ''}`} onClick={() => setActiveTab(tab)}>
                            {t(`agent.tabs.${tab}`)}
                        </div>
                    ))}
                    <button className="btn btn-ghost agent-top-action agent-tabs-chat-action" onClick={() => setActiveTab('chat')}>
                        <IconMessageCircle size={16} stroke={1.7} />
                        <span>{t('agent.actions.chat')}</span>
                    </button>
                </div>}

                {/* ── Enhanced Status Tab ── */}
                {activeTab === 'status' && (() => {
                    // Format date helper
                    const formatDate = (d: string) => {
                        try { return new Date(d).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); } catch { return d; }
                    };
                    // Get model label
                    const primaryModel = llmModels.find((m: any) => m.id === agent.primary_model_id);
                    const modelLabel = primaryModel ? (primaryModel.label || primaryModel.model) : '—';
                    const modelProvider = primaryModel ? primaryModel.provider : '—';

                    return (
                        <div>
                            {/* Metric cards */}
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px', marginBottom: '24px' }}>
                                <div className="card">
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.tabs.status')}</div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                        <span className={`status-dot ${statusKey}`} />
                                        <span style={{ fontSize: '16px', fontWeight: 500 }}>{t(`agent.status.${statusKey}`)}</span>
                                    </div>
                                </div>
                                <div className="card">
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.settings.today')} Token</div>
                                    <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens(agent.tokens_used_today)}</div>
                                    {agent.max_tokens_per_day && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{t('agent.settings.noLimit')} {formatTokens(agent.max_tokens_per_day)}</div>}
                                </div>
                                <div className="card">
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.settings.month')} Token</div>
                                    <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens(agent.tokens_used_month)}</div>
                                    {agent.max_tokens_per_month && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{t('agent.settings.noLimit')} {formatTokens(agent.max_tokens_per_month)}</div>}
                                </div>
                                <div className="card">
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>Cache Hit</div>
                                    <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens(cacheReadToday)}</div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                                        Today {cacheHitRateToday}% · Month {formatTokens(cacheReadMonth)} ({cacheHitRateMonth}%)
                                    </div>
                                </div>
                                {/* Native agent metrics */}
                                {(agent as any)?.agent_type !== 'openclaw' && (<>
                                    <div className="card">
                                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.status.llmCallsToday')}</div>
                                        <div style={{ fontSize: '22px', fontWeight: 600 }}>{((agent as any).llm_calls_today || 0).toLocaleString()}</div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{t('agent.status.max')}: {((agent as any).max_llm_calls_per_day || 1000).toLocaleString()}</div>
                                    </div>
                                    <div className="card">
                                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.status.totalToken')}</div>
                                        <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens((agent as any).tokens_used_total || 0)}</div>
                                    </div>
                                    {metrics && (
                                        <>
                                            <div className="card">
                                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.tasks.done')}</div>
                                                <div style={{ fontSize: '22px', fontWeight: 600 }}>{metrics.tasks?.done || 0}/{metrics.tasks?.total || 0}</div>
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}> {metrics.tasks?.completion_rate || 0}%</div>
                                            </div>
                                            <div className="card">
                                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.status.pending')}</div>
                                                <div style={{ fontSize: '22px', fontWeight: 600, color: metrics.approvals?.pending > 0 ? 'var(--warning)' : 'inherit' }}>{metrics.approvals?.pending || 0}</div>
                                            </div>
                                            <div className="card" style={{ position: 'relative' }}>
                                                <div className="metric-tooltip-trigger" style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px', cursor: 'help', display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                                                    {t('agent.status.24hActions')}
                                                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="8" cy="8" r="6.5" /><path d="M8 7v4M8 5.5v0" /></svg>
                                                    <span className="metric-tooltip">{t('agent.status.24hActionsTooltip')}</span>
                                                </div>
                                                <div style={{ fontSize: '22px', fontWeight: 600 }}>{metrics.activity?.actions_last_24h || 0}</div>
                                            </div>
                                        </>
                                    )}
                                </>)}
                                {/* OpenClaw-specific metrics */}
                                {(agent as any)?.agent_type === 'openclaw' && (
                                    <div className="card">
                                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>
                                            {t('agent.openclaw.lastSeen')}
                                        </div>
                                        <div style={{ fontSize: '16px', fontWeight: 500 }}>
                                            {(agent as any).openclaw_last_seen
                                                ? new Date((agent as any).openclaw_last_seen).toLocaleString()
                                                : t('agent.openclaw.notConnected')}
                                        </div>
                                    </div>
                                )}
                            </div>

                            {/* Agent Profile & Model Info */}
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '24px' }}>
                                <div className="card">
                                    <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>{t('agent.profile.title')}</h3>
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px', gap: '12px' }}>
                                            <span style={{ color: 'var(--text-tertiary)', flexShrink: 0 }}>{t('agent.fields.role')}</span>
                                            <span title={agent.role_description || ''} style={{ textAlign: 'right', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as any }}>{agent.role_description || '—'}</span>
                                        </div>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                            <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.profile.created')}</span>
                                            <span>{agent.created_at ? formatDate(agent.created_at) : '—'}</span>
                                        </div>
                                        {(agent as any).creator_username && (
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.fields.createdBy', 'Created by')}</span>
                                                <span style={{ color: 'var(--text-secondary)' }}>@{(agent as any).creator_username}</span>
                                            </div>
                                        )}
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                            <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.profile.lastActive')}</span>
                                            <span>{agent.last_active_at ? formatDate(agent.last_active_at) : '—'}</span>
                                        </div>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                            <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.profile.timezone')}</span>
                                            <span>{(agent as any).effective_timezone || agent.timezone || 'UTC'}</span>
                                        </div>
                                    </div>
                                </div>
                                {(agent as any)?.agent_type !== 'openclaw' ? (
                                    <div className="card">
                                        <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>{t('agent.modelConfig.title')}</h3>
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.modelConfig.model')}</span>
                                                <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px' }}>{modelLabel}</span>
                                            </div>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.modelConfig.provider')}</span>
                                                <span style={{ textTransform: 'capitalize' }}>{modelProvider}</span>
                                            </div>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.modelConfig.contextRounds')}</span>
                                                <span>{(agent as any).context_window_size || 100}</span>
                                            </div>
                                        </div>
                                    </div>
                                ) : (
                                    <div className="card">
                                        <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>
                                            {t('agent.openclaw.connection')}
                                        </h3>
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.openclaw.type')}</span>
                                                <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                                    <span style={{
                                                        fontSize: '10px', padding: '2px 6px', borderRadius: '4px',
                                                        background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', fontWeight: 600,
                                                    }}>OpenClaw</span>
                                                    Lab
                                                </span>
                                            </div>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.openclaw.lastSeen')}</span>
                                                <span>{(agent as any).openclaw_last_seen
                                                    ? new Date((agent as any).openclaw_last_seen).toLocaleString()
                                                    : t('agent.openclaw.never')}
                                                </span>
                                            </div>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                                                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.openclaw.model')}</span>
                                                <span style={{ color: 'var(--text-secondary)' }}>{t('agent.openclaw.managedBy')}</span>
                                            </div>
                                        </div>
                                    </div>
                                )}
                            </div>

                            {/* Recent Activity */}
                            {activityLogs && activityLogs.length > 0 && (
                                <div className="card">
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                                        <h3 style={{ fontSize: '14px', fontWeight: 600 }}>{t('agent.activity.recent', 'Recent Activity')}</h3>
                                        <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={() => setActiveTab('activityLog')}>View All →</button>
                                    </div>
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                                        {activityLogs.slice(0, 5).map((log: any, i: number) => (
                                            <div key={i} style={{ display: 'flex', gap: '12px', alignItems: 'flex-start', padding: '6px 0', borderBottom: i < 4 ? '1px solid var(--border-subtle)' : 'none' }}>
                                                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', minWidth: '60px', flexShrink: 0 }}>
                                                    {new Date(log.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                                                </span>
                                                <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{log.summary || log.action_type}</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {/* Quick Actions */}
                            <div style={{ display: 'flex', gap: '10px', marginTop: '20px' }}>
                                <button className="btn btn-secondary" onClick={() => setActiveTab('chat')}>{t('agent.actions.chat')}</button>
                                {canManage && <button className="btn btn-secondary" onClick={() => setActiveTab('settings')}>{t('agent.tabs.settings')}</button>}
                            </div>
                        </div>
                    );
                })()}

                {/* ── Aware Tab ── */}
                {activeTab === 'aware' && (() => {
                    // Structured Focus items from the backend database
                    const focusItems = focusRecords.map(focusItemFromApi);
                    const isZh = i18n.language?.startsWith('zh');

                    // Helper: convert trigger config to natural language
                    const triggerToHuman = (trig: any): string => {
                        const isZh = i18n.language?.startsWith('zh');
                        if (trig.type === 'cron' && trig.config?.expr) {
                            const expr = trig.config.expr;
                            const parts = expr.split(' ');
                            if (parts.length >= 5) {
                                const [min, hour, dom, , dow] = parts;
                                const timeStr = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
                                const dayNames = isZh
                                    ? ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
                                    : ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
                                if (dom !== '*' && dow === '*' && min !== '*' && hour !== '*') {
                                    const days = dom.split(',').join(isZh ? '、' : ', ');
                                    return isZh ? `每月 ${days} 日 ${timeStr}` : `Every month on day ${days} at ${timeStr}`;
                                }
                                if (dow === '*' && min !== '*' && hour !== '*') return isZh ? `每天 ${timeStr}` : `Every day at ${timeStr}`;
                                if (dow === '1-5' && min !== '*' && hour !== '*') return isZh ? `工作日 ${timeStr}` : `Weekdays at ${timeStr}`;
                                if ((dow === '0' || dow === '7') && min !== '*' && hour !== '*') return isZh ? `每周日 ${timeStr}` : `Sundays at ${timeStr}`;
                                if (/^[1-6]$/.test(dow) && min !== '*' && hour !== '*') return isZh ? `每${dayNames[Number(dow)]} ${timeStr}` : `${dayNames[Number(dow)]}s at ${timeStr}`;
                                if (hour === '*' && min === '0') {
                                    if (dow === '1-5') return isZh ? '工作日每小时' : 'Every hour on weekdays';
                                    return isZh ? '每小时' : 'Every hour';
                                }
                                if (hour === '*' && min !== '*') return isZh ? `每小时第 ${min.padStart(2, '0')} 分钟` : `Every hour at :${min.padStart(2, '0')}`;
                            }
                            return isZh ? `Cron：${expr}` : `Cron: ${expr}`;
                        }
                        if (trig.type === 'once' && trig.config?.at) {
                            try {
                                return isZh
                                    ? `一次性：${new Date(trig.config.at).toLocaleString()}`
                                    : `Once at ${new Date(trig.config.at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
                            } catch { return isZh ? `一次性：${trig.config.at}` : `Once at ${trig.config.at}`; }
                        }
                        if (trig.type === 'interval' && trig.config?.minutes) {
                            const m = trig.config.minutes;
                            return isZh ? `每 ${m >= 60 ? `${m / 60} 小时` : `${m} 分钟`}` : (m >= 60 ? `Every ${m / 60}h` : `Every ${m} min`);
                        }
                        if (trig.type === 'poll') return `${isZh ? '轮询' : 'Poll'}: ${trig.config?.url?.substring(0, 40) || 'URL'}`;
                        if (trig.type === 'on_message') {
                            const sender = trig.config?.from_agent_id || trig.config?.from_user_id || (isZh ? '未知对象' : 'unknown');
                            return isZh ? `收到 ${sender} 的消息时` : `On message from ${sender}`;
                        }
                        if (trig.type === 'webhook') {
                            return `Webhook${trig.config?.token ? ` (${trig.config.token.substring(0, 6)}...)` : ''}`;
                        }
                        return trig.type;
                    };

                    const triggerReasonText = (trig: any): string | null => {
                        if (!i18n.language?.startsWith('zh')) return trig.reason || null;
                        if (trig.name === 'daily_okr_report') {
                            return '系统触发器：如果启用了日报，收集成员进展、更新滞后的 KR，并生成日报。';
                        }
                        if (trig.name === 'weekly_okr_report') {
                            return '系统触发器：如果启用了周报，收集成员进展、更新滞后的 KR，并生成周报。';
                        }
                        if (trig.name === 'biweekly_okr_checkin') {
                            return '系统触发器：每月 1 日和 15 日进行 OKR 例行检查。';
                        }
                        if (trig.name === 'monthly_okr_report') {
                            return '系统触发器：每月 1 日生成 OKR 月度进展汇报。';
                        }
                        return trig.reason || null;
                    };

                    // Group triggers by focus_ref
                    const triggersByFocus: Record<string, any[]> = {};
                    const focusNames = new Set(focusItems.map((item) => item.name));
                    for (const trig of awareTriggers) {
                        if (trig.focus_ref && focusNames.has(trig.focus_ref)) {
                            if (!triggersByFocus[trig.focus_ref]) triggersByFocus[trig.focus_ref] = [];
                            triggersByFocus[trig.focus_ref].push(trig);
                        } else {
                            const synthetic = synthesizeFocusForTrigger(trig);
                            if (!triggersByFocus[synthetic.name]) triggersByFocus[synthetic.name] = [];
                            triggersByFocus[synthetic.name].push(trig);
                        }
                    }
                    const displayFocusItems = focusItems;

                    // Group activity logs by trigger name -> focus_ref
                    const triggerLogsByFocus: Record<string, any[]> = {};
                    const triggerNameToFocus: Record<string, string> = {};
                    for (const trig of awareTriggers) {
                        triggerNameToFocus[trig.name] = trig.focus_ref || focusKeyFromTrigger(trig);
                    }
                    const triggerRelatedLogs = activityLogs.filter((log: any) =>
                        log.action_type === 'trigger_fired' || log.action_type === 'trigger_created' ||
                        log.action_type === 'trigger_updated' || log.action_type === 'trigger_cancelled' ||
                        log.summary?.includes('trigger')
                    );
                    for (const log of triggerRelatedLogs) {
                        // Try to match log to a focus item via trigger name in the summary
                        let matched = false;
                        for (const [trigName, focusName] of Object.entries(triggerNameToFocus)) {
                            if (log.summary?.includes(trigName) || log.detail?.tool === trigName) {
                                if (!triggerLogsByFocus[focusName]) triggerLogsByFocus[focusName] = [];
                                triggerLogsByFocus[focusName].push(log);
                                matched = true;
                                break;
                            }
                        }
                        if (!matched) {
                            if (!triggerLogsByFocus['__unmatched__']) triggerLogsByFocus['__unmatched__'] = [];
                            triggerLogsByFocus['__unmatched__'].push(log);
                        }
                    }

                    const hasFocusItems = displayFocusItems.length > 0;

                    // Split focus items: active first, completed separately
                    const activeFocusItems = displayFocusItems.filter(f => !f.done && !f.system);
                    const systemFocusItems = displayFocusItems.filter(f => !f.done && f.system);
                    const completedFocusItems = displayFocusItems.filter(f => f.done);
                    const visibleActiveFocus = showAllFocus ? activeFocusItems : activeFocusItems.slice(0, SECTION_PAGE_SIZE);
                    const hiddenActiveCount = activeFocusItems.length - visibleActiveFocus.length;
                    const renderTriggerDot = (done: boolean, label: string) => (
                        <span className={`aware-side-status-dot ${done ? 'done' : 'active'}`} aria-label={label} />
                    );

                    // Render a focus item row
                    const renderFocusItem = (item: FocusItem) => {
                        const isExpanded = expandedFocusIds.has(item.id);
                        const itemTriggers = triggersByFocus[item.name] || [];
                        const itemLogs = triggerLogsByFocus[item.name] || [];
                        
                        const hasTitle = !!item.title;
                        const displayTitle = hasTitle ? item.title : item.name;
                        const displaySubtitle = hasTitle ? item.name : null;
                        const displayDescription = item.description;

                        return (
                            <div key={item.id} style={{
                                borderRadius: '8px',
                                border: '1px solid var(--border-subtle)',
                                overflow: 'hidden',
                                marginBottom: '6px',
                                background: 'var(--bg-primary)',
                                opacity: item.done ? 0.74 : 1,
                            }}>
                                {/* Focus Item Header */}
                                <div
                                    onClick={() => toggleExpandedFocus(item.id)}
                                    style={{
                                        padding: '12px 16px',
                                        display: 'flex',
                                        alignItems: 'flex-start',
                                        gap: '12px',
                                        cursor: 'pointer',
                                        transition: 'background 0.15s',
                                    }}
                                    onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-secondary)')}
                                    onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                                >
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div style={{
                                            fontSize: '13px', fontWeight: 500, lineHeight: '20px',
                                            textDecoration: item.done ? 'line-through' : 'none',
                                            color: item.done ? 'var(--text-tertiary)' : 'var(--text-primary)',
                                            display: 'flex',
                                            alignItems: 'center',
                                            gap: '8px',
                                            flexWrap: 'wrap',
                                        }}>
                                            <span>{displayTitle}</span>
                                            {item.done && (
                                                <span className="aware-side-focus-badge done">
                                                    {t('agent.aware.completed')}
                                                </span>
                                            )}
                                        </div>
                                        {displaySubtitle && (
                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'monospace', marginTop: '2px' }}>
                                                {displaySubtitle}
                                            </div>
                                        )}
                                        {displayDescription && (
                                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', whiteSpace: 'pre-wrap' }}>
                                                {displayDescription}
                                            </div>
                                        )}
                                    </div>
                                    {/* Trigger count badge */}
                                    <span style={{
                                        fontSize: '11px', color: 'var(--text-tertiary)',
                                        padding: '2px 8px', borderRadius: '10px',
                                        background: 'var(--bg-secondary)',
                                        whiteSpace: 'nowrap',
                                    }}>
                                        {i18n.language?.startsWith('zh')
                                            ? `${itemTriggers.length} 个触发器`
                                            : `${itemTriggers.length} trigger${itemTriggers.length > 1 ? 's' : ''}`}
                                    </span>
                                    {/* Expand arrow */}
                                    <span style={{
                                        fontSize: '11px', color: 'var(--text-tertiary)',
                                        transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                                        transition: 'transform 0.15s',
                                        marginTop: '4px',
                                    }}>&#9654;</span>
                                </div>

                                {/* Expanded content */}
                                {isExpanded && (
                                    <div style={{ padding: '0 16px 12px 36px', borderTop: '1px solid var(--border-subtle)' }}>
                                        {/* Nested Triggers */}
                                        {itemTriggers.length > 0 && (
                                            <div style={{ marginTop: '12px' }}>
                                                {itemTriggers.map((trig: any) => (
                                                    <div key={trig.id} style={{
                                                        display: 'flex', alignItems: 'center', gap: '10px',
                                                        padding: '8px 12px', marginBottom: '4px',
                                                        borderRadius: '6px', background: 'var(--bg-secondary)',
                                                        opacity: trig.is_enabled ? 1 : 0.5,
                                                    }}>
                                                        {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                                        <div style={{ flex: 1 }}>
                                                            <div style={{ fontSize: '12px', fontWeight: 500, color: 'var(--text-primary)' }}>
                                                                {triggerToHuman(trig)}
                                                            </div>
                                                            {triggerReasonText(trig) && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{triggerReasonText(trig)}</div>}
                                                            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '2px', fontFamily: 'monospace' }}>
                                                                {trig.type === 'cron' ? trig.config?.expr : ''}{' '}
                                                            </div>
                                                        </div>
                                                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
                                                            {t('agent.aware.fired', { count: trig.fire_count })}
                                                        </span>
                                                        <span style={{ fontSize: '10px', color: trig.is_enabled ? 'var(--accent-primary)' : 'var(--success, #10b981)' }}>
                                                            {trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed')}
                                                        </span>
                                                        <ExecutionIdentityRail
                                                            creatorId={trig.created_by_user_id}
                                                            creatorName={trig.creator_display_name}
                                                            executionUserId={trig.execution_user_id}
                                                            executionUserName={trig.execution_user_display_name}
                                                            users={executionUsers}
                                                            canReassign={canReassignExecutionUser}
                                                            isPending={reassignExecutionUser.isPending}
                                                            onChoose={() => setExecutionUserPickerTarget({
                                                                resourceType: 'trigger',
                                                                resourceId: trig.id,
                                                                executionUserId: trig.execution_user_id || trig.created_by_user_id,
                                                                expectedExecutionUserId: trig.execution_user_id || null,
                                                            })}
                                                        />
                                                        <div style={{ display: 'flex', gap: '4px' }}>
                                                            {canManage && !trig.is_system && <button className="btn btn-ghost" style={{ padding: '2px 6px', fontSize: '11px', color: 'var(--error)' }}
                                                                onClick={async (e) => {
                                                                    e.stopPropagation();
                                                                    if (!canManage) return;
                                                                    const ok = await dialog.confirm(t('agent.aware.deleteTriggerConfirm', { name: trig.name }), { title: '删除触发器', danger: true, confirmLabel: '删除' });
                                                                    if (ok) {
                                                                        await triggerApi.delete(id!, trig.id);
                                                                        refetchTriggers();
                                                                    }
                                                                }}>
                                                                {t('common.delete', 'Delete')}
                                                            </button>}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        )}

                                        {/* Activity Logs for this focus */}
                                        {itemLogs.length > 0 && (
                                            <div style={{ marginTop: '12px' }}>
                                                <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '6px' }}>
                                                    {t('agent.aware.reflections')}
                                                </div>
                                                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                                    {itemLogs.slice(0, 10).map((log: any) => (
                                                        <div key={log.id} style={{
                                                            padding: '6px 12px', borderRadius: '6px',
                                                            background: 'var(--bg-secondary)',
                                                            borderLeft: '2px solid var(--border-subtle)',
                                                        }}>
                                                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '2px' }}>
                                                                <span style={{
                                                                    fontSize: '10px', padding: '1px 5px', borderRadius: '3px',
                                                                    background: log.action_type === 'trigger_fired' ? 'rgba(var(--accent-primary-rgb, 99,102,241), 0.1)' : 'var(--bg-tertiary, #e5e7eb)',
                                                                    color: log.action_type === 'trigger_fired' ? 'var(--accent-primary)' : 'var(--text-tertiary)',
                                                                    fontWeight: 500,
                                                                }}>{log.action_type?.replace('trigger_', '')}</span>
                                                                <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                                                                    {new Date(log.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                                                </span>
                                                            </div>
                                                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{log.summary}</div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        {itemTriggers.length === 0 && itemLogs.length === 0 && (
                                            <div style={{ padding: '12px 0', fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                                {t('agent.aware.noTriggers')}
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    };

                    return (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                            {/* ── Focus Section ── */}
                            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                                    <div>
                                        <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.aware.focus')}</h4>
                                        <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.focusDesc')}</span>
                                    </div>
                                    {hasFocusItems && (
                                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {i18n.language?.startsWith('zh')
                                                ? `${activeFocusItems.length} 个进行中${systemFocusItems.length > 0 ? ` · ${systemFocusItems.length} 个系统` : ''}${completedFocusItems.length > 0 ? ` · ${completedFocusItems.length} 个已完成` : ''}`
                                                : `${activeFocusItems.length} active${systemFocusItems.length > 0 ? ` · ${systemFocusItems.length} system` : ''}${completedFocusItems.length > 0 ? ` · ${completedFocusItems.length} done` : ''}`}
                                        </span>
                                    )}
                                </div>

                                {/* Active Focus Items */}
                                {visibleActiveFocus.map(renderFocusItem)}

                                {/* Show more active items */}
                                {hiddenActiveCount > 0 && (
                                    <button
                                        onClick={() => setShowAllFocus(true)}
                                        className="btn btn-ghost"
                                        style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }}
                                    >
                                        {t('agent.aware.showMore', { count: hiddenActiveCount })}
                                    </button>
                                )}
                                {showAllFocus && activeFocusItems.length > SECTION_PAGE_SIZE && (
                                    <button
                                        onClick={(e) => { setShowAllFocus(false); e.currentTarget.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}
                                        className="btn btn-ghost"
                                        style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }}
                                    >
                                        {t('agent.aware.showLess')}
                                    </button>
                                )}

                                {/* System Focus Items */}
                                {systemFocusItems.length > 0 && (
                                    <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--border-subtle)' }}>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 600, marginBottom: '6px' }}>
                                            {i18n.language?.startsWith('zh') ? '系统 Focus' : 'System Focus'}
                                        </div>
                                        {systemFocusItems.map(renderFocusItem)}
                                    </div>
                                )}

                                {/* Completed Focus Items — auto-collapsed */}
                                {completedFocusItems.length > 0 && (
                                    <>
                                        <button
                                            onClick={() => setShowCompletedFocus(!showCompletedFocus)}
                                            className="btn btn-ghost"
                                            style={{
                                                width: '100%', fontSize: '12px', color: 'var(--text-tertiary)',
                                                padding: '8px', marginTop: '8px',
                                                borderTop: '1px solid var(--border-subtle)',
                                                borderRadius: 0,
                                            }}
                                        >
                                            {showCompletedFocus
                                                ? t('agent.aware.hideCompleted')
                                                : t('agent.aware.showCompleted', { count: completedFocusItems.length })
                                            }
                                        </button>
                                        {showCompletedFocus && completedFocusItems.map(renderFocusItem)}
                                    </>
                                )}

                                {/* Empty state */}
                                {!hasFocusItems && (
                                    <div style={{
                                        padding: '24px', textAlign: 'center', color: 'var(--text-tertiary)',
                                        border: '1px dashed var(--border-subtle)', borderRadius: '8px',
                                    }}>
                                        {t('agent.aware.focusEmpty')}
                                    </div>
                                )}
                            </div>

                            {/* ── Background execution identities ── */}
                            <div className="card background-resource-card" style={{ marginBottom: '16px', padding: '16px' }}>
                                <div className="background-resource-header">
                                    <div>
                                        <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>
                                            {t('agent.aware.executionIdentity.title')}
                                        </h4>
                                        <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                            {t('agent.aware.executionIdentity.description')}
                                        </span>
                                    </div>
                                    <span className="background-resource-count">
                                        {backgroundTasks.length + schedules.length}
                                    </span>
                                </div>

                                {backgroundTasks.length === 0 && schedules.length === 0 ? (
                                    <div className="background-resource-empty">
                                        {t('agent.aware.executionIdentity.empty')}
                                    </div>
                                ) : (
                                    <div className="background-resource-list">
                                        {(backgroundTasks as any[]).map((task) => (
                                            <div key={`task-${task.id}`} className="background-resource-row">
                                                <div className="background-resource-main">
                                                    <div className="background-resource-title-row">
                                                        <span className="background-resource-kind">{t('agent.aware.executionIdentity.task')}</span>
                                                        <span className="background-resource-title">{task.title}</span>
                                                    </div>
                                                    <div className="background-resource-meta">
                                                        <span>{task.status}</span>
                                                        <span>{task.priority}</span>
                                                        <span>{task.type}</span>
                                                    </div>
                                                </div>
                                                <ExecutionIdentityRail
                                                    creatorId={task.created_by_user_id || task.created_by}
                                                    creatorName={task.creator_display_name || task.creator_username}
                                                    executionUserId={task.execution_user_id}
                                                    executionUserName={task.execution_user_display_name}
                                                    users={executionUsers}
                                                    canReassign={canReassignExecutionUser}
                                                    isPending={reassignExecutionUser.isPending}
                                                    onChoose={() => setExecutionUserPickerTarget({
                                                        resourceType: 'task',
                                                        resourceId: task.id,
                                                        executionUserId: task.execution_user_id || task.created_by_user_id || task.created_by,
                                                        expectedExecutionUserId: task.execution_user_id || null,
                                                    })}
                                                />
                                            </div>
                                        ))}
                                        {(schedules as any[]).map((schedule) => (
                                            <div key={`schedule-${schedule.id}`} className="background-resource-row">
                                                <div className="background-resource-main">
                                                    <div className="background-resource-title-row">
                                                        <span className="background-resource-kind schedule">{t('agent.aware.executionIdentity.schedule')}</span>
                                                        <span className="background-resource-title">{schedule.name}</span>
                                                    </div>
                                                    <div className="background-resource-meta">
                                                        <span>{schedule.is_enabled
                                                            ? t('agent.aware.executionIdentity.enabled')
                                                            : t('agent.aware.executionIdentity.disabled')}</span>
                                                        <span className="background-resource-cron">{schedule.cron_expr}</span>
                                                        {schedule.next_run_at && (
                                                            <span>{t('agent.aware.executionIdentity.next')} {new Date(schedule.next_run_at).toLocaleString()}</span>
                                                        )}
                                                    </div>
                                                </div>
                                                <ExecutionIdentityRail
                                                    creatorId={schedule.created_by_user_id || schedule.created_by}
                                                    creatorName={schedule.creator_display_name || schedule.creator_username}
                                                    executionUserId={schedule.execution_user_id}
                                                    executionUserName={schedule.execution_user_display_name}
                                                    users={executionUsers}
                                                    canReassign={canReassignExecutionUser}
                                                    isPending={reassignExecutionUser.isPending}
                                                    onChoose={() => setExecutionUserPickerTarget({
                                                        resourceType: 'schedule',
                                                        resourceId: schedule.id,
                                                        executionUserId: schedule.execution_user_id || schedule.created_by_user_id || schedule.created_by,
                                                        expectedExecutionUserId: schedule.execution_user_id || null,
                                                    })}
                                                />
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {executionUserPickerTarget && (() => {
                                const currentUserOption = (executionUsers as ExecutionUserOption[]).find(
                                    (user) => user.id === executionUserPickerTarget.executionUserId,
                                );
                                return (
                                    <OrgMemberAccessPicker
                                        open
                                        agentId={id!}
                                        membersOnly
                                        singleSelect
                                        users={[{
                                            id: executionUserPickerTarget.executionUserId,
                                            name: currentUserOption?.display_name
                                                || currentUserOption?.username
                                                || currentUserOption?.email
                                                || shortIdentity(executionUserPickerTarget.executionUserId),
                                            email: currentUserOption?.email || undefined,
                                            access_level: 'use',
                                        }]}
                                        departments={[]}
                                        onClose={() => setExecutionUserPickerTarget(null)}
                                        onSave={async (users) => {
                                            const nextUser = users[0];
                                            if (!nextUser || nextUser.id === executionUserPickerTarget.executionUserId) return;
                                            await reassignExecutionUser.mutateAsync({
                                                resourceType: executionUserPickerTarget.resourceType,
                                                resourceId: executionUserPickerTarget.resourceId,
                                                executionUserId: nextUser.id,
                                                expectedExecutionUserId: executionUserPickerTarget.expectedExecutionUserId,
                                            });
                                        }}
                                    />
                                );
                            })()}

                            {reflectionSessions.length > 0 && (() => {
                                const totalPages = Math.ceil(reflectionSessions.length / REFLECTIONS_PAGE_SIZE);
                                const pageStart = reflectionPage * REFLECTIONS_PAGE_SIZE;
                                const visibleSessions = reflectionSessions.slice(pageStart, pageStart + REFLECTIONS_PAGE_SIZE);
                                return (
                                    <div className="card" style={{ padding: '16px' }}>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                                            <div>
                                                <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{isZh ? '执行记录' : 'Executions'}</h4>
                                                <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                                    {isZh ? '所有触发入口的状态与标准会话记录' : 'Status and standard conversations for every trigger entry'}
                                                </span>
                                            </div>
                                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                {reflectionSessions.length} session{reflectionSessions.length > 1 ? 's' : ''}
                                            </span>
                                        </div>
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                            {visibleSessions.map((session: any) => {
                                                const recordId = session.record_id || session.id;
                                                const conversationId = session.conversation_id || (!session.conversation_missing ? session.id : null);
                                                const isExpanded = expandedReflection === recordId;
                                                const msgs = conversationId ? (reflectionMessages[conversationId] || []) : [];
                                                return (
                                                    <div key={recordId} style={{
                                                        borderRadius: '8px',
                                                        border: '1px solid var(--border-subtle)',
                                                        overflow: 'hidden',
                                                        background: 'var(--bg-primary)',
                                                    }}>
                                                        <div
                                                            onClick={async () => {
                                                                if (isExpanded) {
                                                                    setExpandedReflection(null);
                                                                    return;
                                                                }
                                                                setExpandedReflection(recordId);
                                                                if (conversationId) await loadReflectionMessages(conversationId);
                                                            }}
                                                            style={{
                                                                padding: '10px 16px',
                                                                display: 'flex', alignItems: 'center', gap: '10px',
                                                                cursor: 'pointer', transition: 'background 0.15s',
                                                            }}
                                                            onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-secondary)')}
                                                            onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                                                        >
                                                            <div style={{
                                                                width: '6px', height: '6px', borderRadius: '50%',
                                                                background: 'var(--accent-primary)', flexShrink: 0,
                                                            }} />
                                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                                <div style={{ fontSize: '12px', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                    {session.execution
                                                                        ? `${session.execution.trigger_name} · ${session.execution.source}`
                                                                        : formatReflectionTitle(session.title, !!isZh)}
                                                                </div>
                                                                <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '1px' }}>
                                                                    {new Date(session.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                                                    {session.message_count > 0 && ` · ${session.message_count} msg`}
                                                                </div>
                                                            </div>
                                                            <span style={{
                                                                fontSize: '11px', color: 'var(--text-tertiary)',
                                                                transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                                                                transition: 'transform 0.15s',
                                                            }}>&#9654;</span>
                                                        </div>
                                                        {isExpanded && (
                                                            <div style={{ padding: '0 16px 12px', borderTop: '1px solid var(--border-subtle)' }}>
                                                                {!conversationId ? (
                                                                    <ConversationTimeline
                                                                        agentId={id!}
                                                                        agentName={agent.name || 'Agent'}
                                                                        messages={[]}
                                                                        provenance={session.execution}
                                                                        onOpenSubagentSession={openSubagentSession}
                                                                        viewOf={() => ({ isLeft: true })}
                                                                    />
                                                                ) : msgs.length === 0 ? (
                                                                    <div style={{ padding: '12px 0', fontSize: '12px', color: 'var(--text-tertiary)' }}>Loading...</div>
                                                                ) : (
                                                                    <div style={{ marginTop: '8px' }}>
                                                                        <ConversationTimeline
                                                                            agentId={id!}
                                                                            agentName={agent.name || 'Agent'}
                                                                            messages={msgs}
                                                                            provenance={session.execution}
                                                                            viewOf={(message) => ({
                                                                                isLeft: message.role !== 'user',
                                                                                senderLabel: message.role === 'user'
                                                                                    ? (isZh ? '触发事件' : 'Trigger event')
                                                                                    : (agent.name || 'Agent'),
                                                                                avatarText: message.role === 'user' ? 'T' : (agent.name || 'A')[0],
                                                                                forceSenderLabel: true,
                                                                            })}
                                                                            unavailableAttachmentKeys={unavailableAttachmentKeys}
                                                                            onAttachmentDownload={handleAttachmentDownload}
                                                                            onAttachmentUnavailable={markAttachmentUnavailable}
                                                                            onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                                                            onOpenSubagentSession={openSubagentSession}
                                                                            onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                                                        />
                                                                    </div>
                                                                )}
                                                            </div>
                                                        )}
                                                    </div>
                                                );
                                            })}
                                        </div>
                                        {/* Pagination controls */}
                                        {totalPages > 1 && (
                                            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', marginTop: '12px', paddingTop: '8px', borderTop: '1px solid var(--border-subtle)' }}>
                                                <button
                                                    onClick={() => { setReflectionPage(p => Math.max(0, p - 1)); setExpandedReflection(null); }}
                                                    disabled={reflectionPage === 0}
                                                    className="btn btn-ghost"
                                                    style={{ fontSize: '12px', padding: '4px 10px', opacity: reflectionPage === 0 ? 0.3 : 1 }}
                                                >
                                                    {i18n.language?.startsWith('zh') ? '上一页' : 'Prev'}
                                                </button>
                                                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums' }}>
                                                    {reflectionPage + 1} / {totalPages}
                                                </span>
                                                <button
                                                    onClick={() => { setReflectionPage(p => Math.min(totalPages - 1, p + 1)); setExpandedReflection(null); }}
                                                    disabled={reflectionPage >= totalPages - 1}
                                                    className="btn btn-ghost"
                                                    style={{ fontSize: '12px', padding: '4px 10px', opacity: reflectionPage >= totalPages - 1 ? 0.3 : 1 }}
                                                >
                                                    {i18n.language?.startsWith('zh') ? '下一页' : 'Next'}
                                                </button>
                                            </div>
                                        )}
                                    </div>
                                );
                            })()}
                        </div>
                    );
                })()}


                {/* ── Mind Tab (Soul + Memory + Heartbeat) ── */}
                {
                    activeTab === 'mind' && id && (
                        <MindTab agentId={id} canEdit={(agent as any)?.access_level !== 'use'} />
                    )
                }

                {/* ── Tools Tab ── */}
                {
                    activeTab === 'tools' && id && <ToolsTab agentId={id} agentName={agent?.name || 'Agent'} canManage={canManage} />
                }

                {/* ── Scene Configuration Tab ── */}
                {
                    activeTab === 'scenes' && id && canManage && agent?.scene_config_enabled && (
                        <SceneConfigTab agentId={id} onDirtyChange={setSceneConfigDirty} />
                    )
                }

                {/* ── Skills Tab ── */}
                {
                    activeTab === 'skills' && id && (
                        <SkillsTab agentId={id} canManage={canManage} />
                    )
                }

                {/* ── Relationships Tab ── */}
                {
                    activeTab === 'relationships' && (
                        <RelationshipEditor agentId={id!} readOnly={!canManage} />
                    )
                }

                {/* ── Workspace Tab ── */}
                {
                    activeTab === 'workspace' && (() => {
                        const adapter: FileBrowserApi = {
                            list: (p) => fileApi.list(id!, p),
                            read: (p) => fileApi.read(id!, p),
                            write: (p, c) => fileApi.write(id!, p, c),
                            delete: (p) => fileApi.delete(id!, p),
                            upload: (file, path, onProgress) => fileApi.upload(id!, file, path + '/', onProgress),
                            downloadUrl: (p) => fileApi.downloadUrl(id!, p),
                        };
                        return <FileBrowser api={adapter} rootPath="workspace" features={{ upload: canManage, newFile: canManage, newFolder: canManage, edit: canManage, delete: canManage, directoryNavigation: true }} />;
                    })()
                }

                {
                    activeTab === 'chat' && (
                        <div
                            className="agent-chat-shell"
                            style={{
                                display: 'flex',
                                gap: 0,
                                flex: 1,
                                minHeight: 0,
                                height: 'calc(100vh - 100px)',
                                margin: '0 8px 8px',
                                border: '1px solid rgba(0, 0, 0, 0.06)',
                                borderRadius: '12px',
                                overflow: 'hidden',
                                boxShadow: '0 2px 8px rgba(0, 0, 0, 0.04)',
                            }}
                        >
                            {/* ── Left: session sidebar ── */}
                            <div className={`session-sidebar ${sessionListCollapsed ? 'collapsed' : ''}`} style={{ width: sessionListCollapsed ? '0px' : '220px', transition: 'width 0.2s ease', flexShrink: 0, minHeight: 0, borderRight: sessionListCollapsed ? 'none' : '1px solid var(--border-subtle)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                                {/* ── Header: scope dropdown + collapse ── */}
                                <div style={{ flexShrink: 0 }}>
                                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '4px', padding: '10px 8px 8px 12px', minHeight: '40px', boxSizing: 'border-box' }}>
                                        {canViewAllAgentChatSessions ? (
                                            <div className="scope-dropdown" ref={scopeDropdownRef}>
                                                <button
                                                    className="scope-dropdown-trigger"
                                                    onClick={() => setScopeDropdownOpen(open => !open)}
                                                >
                                                    <span className="scope-dropdown-label">
                                                        {chatScope === 'mine'
                                                            ? t('agent.chat.mySessions')
                                                            : t('agent.chat.otherSessions', '其他会话')
                                                        }
                                                    </span>
                                                    <svg className={`scope-dropdown-chevron${scopeDropdownOpen ? ' scope-dropdown-chevron--open' : ''}`} width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                                                </button>
                                                {scopeDropdownOpen && (
                                                    <div className="scope-dropdown-menu">
                                                        <div
                                                            className={`scope-dropdown-item${chatScope === 'mine' ? ' scope-dropdown-item--active' : ''}`}
                                                            onClick={() => { onAdminTabMine(); setScopeDropdownOpen(false); }}
                                                        >{t('agent.chat.mySessions')}</div>
                                                        <div
                                                            className={`scope-dropdown-item${chatScope === 'all' ? ' scope-dropdown-item--active' : ''}`}
                                                            onClick={() => { onAdminTabOthers(); setScopeDropdownOpen(false); }}
                                                        >{t('agent.chat.otherSessions', '其他会话')}</div>
                                                    </div>
                                                )}
                                            </div>
                                        ) : (
                                            <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)', lineHeight: '1.25', flex: 1, minWidth: 0 }}>
                                                {t('agent.chat.mySessions')}
                                            </span>
                                        )}
                                        {!sessionListCollapsed && (
                                            <button
                                                type="button"
                                                onClick={() => setSessionListCollapsed(true)}
                                                className="session-sidebar-toggle-btn"
                                                title={t('agent.chat.collapseSidebar')}
                                            >
                                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
                                            </button>
                                        )}
                                    </div>
                                    {(!canViewAllAgentChatSessions || chatScope === 'mine') && (
                                        <div style={{ padding: '0 12px 8px' }}>
                                            <button
                                                type="button"
                                                onClick={createNewSession}
                                                className="new-session-btn"
                                            >
                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden style={{ display: 'block', flexShrink: 0 }}>
                                                    <line x1="12" y1="5" x2="12" y2="19" />
                                                    <line x1="5" y1="12" x2="19" y2="12" />
                                                </svg>
                                                <span>{t('agent.chat.newSession')}</span>
                                            </button>
                                        </div>
                                    )}
                                </div>

                                <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                                    {(!canViewAllAgentChatSessions || chatScope === 'mine') ? (
                                        <VirtualSessionList
                                            key={`${id || 'unknown'}:mine`}
                                            items={sessions}
                                            hasMore={sessionsHasMore}
                                            initialLoading={sessionsLoading}
                                            loadingMore={sessionsLoadingMore}
                                            estimateSize={59}
                                            loadingState={<div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>}
                                            emptyState={<div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.chat.noSessionsYet')}<br />{t('agent.chat.clickToStart')}</div>}
                                            loadMoreLabel={t('common.loading')}
                                            onLoadMore={() => fetchMySessions(true, id, true)}
                                            renderItem={(s: any) => {
                                                    const isActive = activeSession?.id === s.id && (chatScope === 'mine' || !canViewAllAgentChatSessions);
                                                    const channelLabel: Record<string, string> = {
                                                        feishu: t('common.channels.feishu'),
                                                        discord: t('common.channels.discord'),
                                                        slack: t('common.channels.slack'),
                                                        wechat: t('common.channels.wechat'),
                                                        dingtalk: t('common.channels.dingtalk'),
                                                        wecom: t('common.channels.wecom'),
                                                    };
                                                    const chLabel = channelLabel[s.source_channel];
                                                    return (
                                                        <div key={s.id} onClick={() => { setChatScope('mine'); selectSession(s, 'mine'); }}
                                                            className="session-item"
                                                            style={{ padding: '8px 12px', cursor: 'pointer', borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent', background: isActive ? 'var(--bg-secondary)' : 'transparent', marginBottom: '1px', display: 'flex', alignItems: 'center', gap: '4px' }}
                                                            onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = 'var(--bg-secondary)'; }}
                                                            onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = 'transparent'; }}>
                                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '2px' }}>
                                                                    <div style={{ fontSize: '12px', fontWeight: isActive ? 600 : 400, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>{s.title}</div>
                                                                    {s.is_primary && (
                                                                        <span style={{
                                                                            fontSize: '9px',
                                                                            padding: '1px 4px',
                                                                            borderRadius: '3px',
                                                                            background: 'var(--bg-tertiary)',
                                                                            color: 'var(--text-secondary)',
                                                                            flexShrink: 0,
                                                                            border: '1px solid var(--border-subtle)',
                                                                        }}>
                                                                            {i18n.language === 'zh' ? '主会话' : 'Primary'}
                                                                        </span>
                                                                    )}
                                                                    {s.unread_count > 0 && (
                                                                        <span style={{
                                                                            minWidth: s.unread_count > 9 ? '18px' : '14px',
                                                                            height: s.unread_count > 9 ? '18px' : '14px',
                                                                            padding: s.unread_count > 9 ? '0 4px' : '0',
                                                                            borderRadius: '999px',
                                                                            background: 'var(--text-primary)',
                                                                            color: 'var(--bg-primary)',
                                                                            fontSize: '10px',
                                                                            fontWeight: 600,
                                                                            display: 'flex',
                                                                            alignItems: 'center',
                                                                            justifyContent: 'center',
                                                                            flexShrink: 0,
                                                                        }}>
                                                                            {s.unread_count > 99 ? '99+' : s.unread_count}
                                                                        </span>
                                                                    )}
                                                                    {chLabel && <span style={{ fontSize: '9px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-tertiary)', color: 'var(--text-tertiary)', flexShrink: 0 }}>{chLabel}</span>}
                                                                </div>
                                                                <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                                                                    {s.last_message_at
                                                                        ? new Date(s.last_message_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                                                                        : new Date(s.created_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric' })}
                                                                    {s.message_count > 0 && <span className="session-msg-count" style={{ marginLeft: 'auto' }}>{s.message_count}</span>}
                                                                </div>
                                                            </div>
                                                            <button className="session-del-btn" onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
                                                                title={t('chat.deleteSession', 'Delete session')}>
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>
                                                            </button>
                                                        </div>
                                                    );
                                            }}
                                        />
                                    ) : (
                                        <VirtualSessionList
                                            key={`${id || 'unknown'}:all`}
                                            items={othersListForPicker}
                                            hasMore={allSessionsHasMore}
                                            initialLoading={allSessionsLoading}
                                            loadingMore={allSessionsLoadingMore}
                                            estimateSize={48}
                                            loadingState={(
                                                <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                                    {[...Array(3)].map((_, i) => (
                                                        <div key={i} style={{ padding: '6px 0', animation: 'pulse 1.5s ease-in-out infinite', animationDelay: `${i * 0.1}s` }}>
                                                            <div style={{ height: '12px', width: `${70 + (i % 3) * 10}%`, background: 'var(--bg-tertiary)', borderRadius: '4px', marginBottom: '6px' }} />
                                                            <div style={{ height: '10px', width: `${40 + (i % 4) * 8}%`, background: 'var(--bg-tertiary)', borderRadius: '3px', opacity: 0.6 }} />
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                            emptyState={<div style={{ padding: '16px 12px', fontSize: '12px', color: 'var(--text-tertiary)', textAlign: 'center' }}>{t('agent.chat.noSessionsYet')}</div>}
                                            loadMoreLabel={t('common.loading')}
                                            onLoadMore={() => fetchAllSessions(true, true, id)}
                                            renderItem={(s: any) => {
                                                        const isActive = activeSession?.id === s.id && chatScope === 'all';
                                                        const channelLabel: Record<string, string> = {
                                                            feishu: t('common.channels.feishu'),
                                                            discord: t('common.channels.discord'),
                                                            slack: t('common.channels.slack'),
                                                            wechat: t('common.channels.wechat'),
                                                            dingtalk: t('common.channels.dingtalk'),
                                                            wecom: t('common.channels.wecom'),
                                                        };
                                                        const chLabel = channelLabel[s.source_channel];
                                                        return (
                                                            <div key={s.id} onClick={() => selectSession(s, 'all')}
                                                                className="session-item"
                                                                style={{ padding: '6px 12px', cursor: 'pointer', borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent', background: isActive ? 'var(--bg-secondary)' : 'transparent', position: 'relative' }}
                                                                onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = 'var(--bg-secondary)'; }}
                                                                onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = 'transparent'; }}>
                                                                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '1px' }}>
                                                                    <div style={{ fontSize: '11px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-primary)', flex: 1 }}>{s.title}</div>
                                                                    {s.is_primary && (
                                                                        <span style={{
                                                                            fontSize: '9px',
                                                                            padding: '1px 4px',
                                                                            borderRadius: '3px',
                                                                            background: 'var(--bg-tertiary)',
                                                                            color: 'var(--text-secondary)',
                                                                            flexShrink: 0,
                                                                            border: '1px solid var(--border-subtle)',
                                                                        }}>
                                                                            {i18n.language === 'zh' ? '主会话' : 'Primary'}
                                                                        </span>
                                                                    )}
                                                                    {s.unread_count > 0 && (
                                                                        <span style={{
                                                                            minWidth: s.unread_count > 9 ? '18px' : '14px',
                                                                            height: s.unread_count > 9 ? '18px' : '14px',
                                                                            padding: s.unread_count > 9 ? '0 4px' : '0',
                                                                            borderRadius: '999px',
                                                                            background: 'var(--text-primary)',
                                                                            color: 'var(--bg-primary)',
                                                                            fontSize: '10px',
                                                                            fontWeight: 600,
                                                                            display: 'flex',
                                                                            alignItems: 'center',
                                                                            justifyContent: 'center',
                                                                            flexShrink: 0,
                                                                        }}>
                                                                            {s.unread_count > 99 ? '99+' : s.unread_count}
                                                                        </span>
                                                                    )}
                                                                    {chLabel && <span style={{ fontSize: '9px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-tertiary)', color: 'var(--text-tertiary)', flexShrink: 0 }}>{chLabel}</span>}
                                                                </div>
                                                                <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', display: 'flex', gap: '4px' }}>
                                                                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{s.username || ''}</span>
                                                                    <span style={{ flexShrink: 0 }}>{s.last_message_at ? new Date(s.last_message_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}{s.message_count > 0 ? ` · ${s.message_count}` : ''}</span>
                                                                </div>
                                                            </div>
                                                        );
                                            }}
                                        />
                                    )}
                                </div>
                            </div>

                            {/* ── Right: chat/message area ── */}
                            <div className={`agent-chat-area ${livePanelVisible ? 'has-live-panel' : ''}`} style={{ flex: 1, display: 'flex', flexDirection: 'row', position: 'relative', minWidth: 0, overflow: 'hidden' }}>
                                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative', minWidth: 0, overflow: 'hidden' }}>
                                    {sessionListCollapsed && (
                                        <button onClick={() => setSessionListCollapsed(false)} className="session-sidebar-toggle-btn session-sidebar-toggle-btn--floating" title="Show chat sessions">
                                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
                                        </button>
                                    )}
                                {!activeSession ? (
                                    <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-tertiary)', fontSize: '13px', flexDirection: 'column', gap: '8px' }}>
                                        <div>{t('agent.chat.noSessionSelected')}</div>
                                        {!isViewingOtherUsersSessions && (
                                            <button className="btn btn-secondary" onClick={createNewSession} style={{ fontSize: '12px' }}>{t('agent.chat.startNewSession')}</button>
                                        )}
                                    </div>
                                ) : !isWritableSession(activeSession) ? (
                                    /* ── Read-only history view (other user's session or agent-to-agent) ── */
                                    <>
                                        <div
                                            style={{
                                                position: 'absolute',
                                                top: '12px',
                                                left: sessionListCollapsed ? '52px' : '16px',
                                                zIndex: 10,
                                                fontSize: '11px',
                                                color: 'var(--text-tertiary)',
                                                padding: '4px 8px',
                                                background: 'var(--bg-secondary)',
                                                borderRadius: '4px',
                                                pointerEvents: 'none',
                                            }}
                                        >
                                            {activeSession.source_channel === 'agent' ? (
                                                <><IconRobot size={13} stroke={1.8} /> Agent Conversation · {activeSession.username || 'Agents'}</>
                                            ) : activeSession.source_channel === 'subagent' ? (
                                                <><IconRobot size={13} stroke={1.8} /> Read-only · Subagent</>
                                            ) : (
                                                <>Read-only · {activeSession.username || 'User'}</>
                                            )}
                                        </div>
                                        <div
                                            ref={historyContainerRef}
                                            data-conversation-scroller="web-history"
                                            tabIndex={0}
                                            aria-label={i18n.language?.startsWith('zh') ? '只读会话消息' : 'Read-only conversation messages'}
                                            onScroll={handleHistoryScroll}
                                            {...historyAutoFollowInteractionProps}
                                            style={{ flex: 1, overflowY: 'auto', padding: '48px 16px 12px' }}
                                        >
                                            {historyLoadingMore && (
                                                <div style={{ textAlign: 'center', padding: '12px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                                                    Loading more messages...
                                                </div>
                                            )}
                                            {!historyHasMore && historyMsgs.length > 0 && (
                                                <div style={{ textAlign: 'center', padding: '12px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                                                    All messages loaded
                                                </div>
                                            )}
                                            {(() => {
                                                // A2A perspective is based only on the canonical sender Agent ID.
                                                const isA2A = activeSession.source_channel === 'agent' || activeSession.participant_type === 'agent';
                                                const isGroupChat = !isA2A && !!activeSession.is_group;
                                                const isHumanReadonly = !isA2A && !activeSession.is_group;
                                                const thisAgentId = (agent as any)?.id != null ? String((agent as any).id) : null;
                                                const viewerId = currentUser?.id != null ? String(currentUser.id) : null;
                                                return <ConversationTimeline
                                                    agentId={id!}
                                                    agentName={(agent as any)?.name || 'Agent'}
                                                    messages={historyMsgs as any}
                                                    scrollerRef={historyContainerRef}
                                                    resumeMeasurementKey={pcResumeMeasurementKey}
                                                    provenance={activeSessionExecution}
                                                    isRunning={readonlyGenerationActive}
                                                    progressMessage={{
                                                        id: `conversation-turn-progress:${activeSession?.id || 'history'}:${sessionTurnRuntimeRef.current[`${id}:${activeSession?.id}`]?.snapshot.generation || 0}`,
                                                    }}
                                                    unavailableAttachmentKeys={unavailableAttachmentKeys}
                                                    onAttachmentDownload={handleAttachmentDownload}
                                                    onAttachmentUnavailable={markAttachmentUnavailable}
                                                    onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                                    onOpenSubagentSession={openSubagentSession}
                                                    onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                                    viewOf={(m: any) => {
                                                    // Canonical actor IDs, not LLM roles, determine A2A ownership.
                                                    // All Agent actors are left and human actors are right.
                                                    // Actorless legacy rows fail closed to the left.
                                                    // Group chat: assistant always left; user msgs are RIGHT only when sent
                                                    // by the logged-in viewer themself, otherwise LEFT (so each distinct
                                                    // human speaker gets their own avatar/name label).
                                                    let isLeft: boolean;
                                                    if (isA2A) {
                                                        isLeft = isA2AMessageLeft(m);
                                                    } else if (isGroupChat) {
                                                        if (m.role === 'assistant') {
                                                            isLeft = true;
                                                        } else {
                                                            isLeft = !(viewerId && m.sender_user_id && m.sender_user_id === viewerId);
                                                        }
                                                    } else {
                                                        isLeft = m.role === 'assistant';
                                                    }
                                                    const senderLabel = isHumanReadonly
                                                        ? (isLeft ? ((agent as any)?.name || 'Agent') : (activeSession.username || 'User'))
                                                        : isGroupChat
                                                            ? (m.role === 'assistant'
                                                                ? ((agent as any)?.name || 'Agent')
                                                                : (m.sender_name || 'User'))
                                                            : undefined;
                                                    const avatarText = isHumanReadonly
                                                        ? (isLeft ? (((agent as any)?.name || 'Agent')[0]) : ((activeSession.username || 'User')[0]))
                                                        : isGroupChat
                                                            ? (m.role === 'assistant'
                                                                ? ((((agent as any)?.name || 'Agent')[0]) || 'A')
                                                                : ((m.sender_name && m.sender_name[0]) || 'U'))
                                                            : undefined;
                                                        return { isLeft, senderLabel, avatarText, forceSenderLabel: isHumanReadonly || isGroupChat };
                                                    }}
                                                />;
                                            })()}
                                        </div>
                                        {showHistoryScrollBtn && (
                                            <ConversationScrollToBottomButton
                                                variant="web"
                                                onClick={scrollHistoryToBottom}
                                                label={i18n.language?.startsWith('zh') ? '滚动到底' : 'Scroll to bottom'}
                                            />
                                        )}
                                    </>
                                ) : (
                                    /* ── Live WebSocket chat (own session) ── */
                                    <div {...chatDropProps} style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative', minHeight: 0, overflow: 'hidden' }}>
                                        {/* Drop overlay */}
                                        {isChatDragging && (
                                            <div className="drop-zone-overlay">
                                                <div className="drop-zone-overlay__icon"><IconPaperclip size={28} stroke={1.8} /></div>
                                                <div className="drop-zone-overlay__text">{t('agent.upload.dropToAttach', 'Drop files to attach (max 10)')}</div>
                                            </div>
                                        )}
                                        {showNoModelState && renderNoModelGuide('floating')}
                                        <div
                                            ref={chatContainerRef}
                                            data-conversation-scroller="web-live"
                                            tabIndex={0}
                                            aria-label={i18n.language?.startsWith('zh') ? '会话消息' : 'Conversation messages'}
                                            onScroll={handleChatScroll}
                                            {...liveAutoFollowInteractionProps}
                                            style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}
                                        >
                                            {chatMessages.length === 0 && !showNoModelState && (
                                                <div className="chat-empty-state">
                                                    <div className="chat-empty-state__title">{activeSession?.title || t('agent.chat.startChat')}</div>
                                                    <div className="chat-empty-state__subtitle">{t('agent.chat.startConversation', { name: agent.name })}</div>
                                                    <div className="chat-empty-state__hint">{t('agent.chat.fileSupport')}</div>
                                                </div>
                                            )}
                                            {(() => {
                                                const visibleChatMessages = showNoModelState
                                                    ? chatMessages.filter((msg: any) => {
                                                        const content = String(msg?.content || msg?.message || '');
                                                        return !(msg?.role === 'assistant' && (content.includes('no LLM model') || content.includes('No model')));
                                                    })
                                                    : chatMessages;
                                                return <ConversationTimeline
                                                    agentId={id!}
                                                    agentName={(agent as any)?.name || 'Agent'}
                                                    messages={visibleChatMessages as any}
                                                    scrollerRef={chatContainerRef}
                                                    resumeMeasurementKey={pcResumeMeasurementKey}
                                                    provenance={activeSessionExecution}
                                                    isRunning={generationActive}
                                                    progressMessage={{
                                                        id: `conversation-turn-progress:${activeSession?.id || 'new'}:${sessionTurnRuntimeRef.current[`${id}:${activeSession?.id}`]?.snapshot.generation || 0}`,
                                                    }}
                                                    unavailableAttachmentKeys={unavailableAttachmentKeys}
                                                    onAttachmentDownload={handleAttachmentDownload}
                                                    onAttachmentUnavailable={markAttachmentUnavailable}
                                                    onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                                    onOpenSubagentSession={openSubagentSession}
                                                    onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                                    viewOf={(m: any) => ({
                                                        isLeft: m.role === 'assistant',
                                                        senderLabel: m.role === 'assistant'
                                                            ? ((agent as any)?.name || 'Agent')
                                                            : (currentUser?.display_name || undefined),
                                                        avatarText: m.role === 'assistant'
                                                            ? (((agent as any)?.name || 'Agent')[0])
                                                            : (currentUser?.display_name?.[0] || undefined),
                                                    })}
                                                />;
                                            })()
                                            }
                                        </div>
                                        {showScrollBtn && (
                                            <ConversationScrollToBottomButton
                                                variant="web"
                                                bottom={chatScrollBtnBottom}
                                                onClick={scrollToBottom}
                                                label={i18n.language?.startsWith('zh') ? '滚动到底' : 'Scroll to bottom'}
                                            />
                                        )}
                                        {/* Transient info banner — e.g. fallback model switch */}
                                        {chatInfoMsg && (
                                            <div style={{ padding: '6px 14px', borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--text-secondary)', animation: 'fadeIn 0.2s ease' }}>
                                                <span style={{ opacity: 0.7 }}>ℹ️</span>
                                                <span style={{ flex: 1 }}>{chatInfoMsg}</span>
                                                <button onClick={() => setChatInfoMsg(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '14px', lineHeight: 1, padding: '0 2px' }}>✕</button>
                                            </div>
                                        )}
                                        {/* Transient info banner — e.g. fallback model switch */}
                                        {chatInfoMsg && (
                                            <div style={{ padding: '6px 14px', borderTop: '1px solid rgba(99,102,241,0.25)', background: 'rgba(99,102,241,0.07)', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--text-secondary)', animation: 'fadeIn 0.2s ease' }}>
                                                <span style={{ opacity: 0.7 }}>ℹ️</span>
                                                <span style={{ flex: 1 }}>{chatInfoMsg}</span>
                                                <button onClick={() => setChatInfoMsg(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '14px', lineHeight: 1, padding: '0 2px' }}>✕</button>
                                            </div>
                                        )}
                                        {agentExpired ? (
                                            <div style={{ padding: '7px 16px', borderTop: '1px solid rgba(245,158,11,0.3)', background: 'rgba(245,158,11,0.08)', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'rgb(180,100,0)' }}>
                                                <span>⏸</span>
                                                <span>This Agent has <strong>expired</strong> and is off duty. Contact your admin to extend its service.</span>
                                            </div>
                                        ) : !wsConnected && !!currentUser && sessionUserIdStr(activeSession) === viewerUserIdStr() ? (
                                            <div style={{ padding: '3px 16px', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                <span style={{ display: 'inline-block', width: '5px', height: '5px', borderRadius: '50%', background: 'var(--accent-primary)', opacity: 0.8, animation: 'pulse 1.2s ease-in-out infinite' }} />
                                                Connecting...
                                            </div>
                                        ) : null}
                                        <div ref={chatInputAreaRef} className="chat-input-area" style={{ flexShrink: 0 }}>
                                            <div className="chat-composer">
                                            {(chatUploadDrafts.length > 0 || attachedFiles.length > 0) && (
                                                <div className="chat-composer-attachments">
                                                    {chatUploadDrafts.map((draft) => (
                                                        <div key={draft.id} className="chat-file-pill">
                                                            <div
                                                                className="chat-file-pill__fill"
                                                                style={{ width: `${draft.percent}%` }}
                                                            />
                                                            <div className="chat-file-pill__row">
                                                                {draft.previewUrl ? (
                                                                    <img className="chat-file-pill__thumb" src={draft.previewUrl} alt="" />
                                                                ) : (
                                                                    <span className="chat-file-pill__icon">
                                                                        <ChatAttachmentIcon name={draft.name} size={16} />
                                                                    </span>
                                                                )}
                                                                <span className="chat-file-pill__name">{draft.name}</span>
                                                                <span className="chat-file-pill__size">{formatFileSize(draft.sizeBytes)}</span>
                                                                <span className="chat-file-pill__pct">{draft.percent}%</span>
                                                                <button
                                                                    type="button"
                                                                    className="chat-file-pill__remove"
                                                                    onClick={() => {
                                                                        chatUploadAbortRef.current.get(draft.id)?.();
                                                                    }}
                                                                    title="Cancel upload"
                                                                >
                                                                    ×
                                                                </button>
                                                            </div>
                                                        </div>
                                                    ))}
                                                    {attachedFiles.map((file, idx) => {
                                                        const imageIndex = attachedImagePreviews.findIndex((image) => image.src === file.imageUrl);
                                                        return (
                                                            <div
                                                                key={`a-${idx}-${file.name}`}
                                                                className={`chat-file-pill ${file.source === 'workspace_auto' ? 'chat-file-pill--workspace' : ''}`}
                                                                title={file.path || file.name}
                                                            >
                                                                <div className="chat-file-pill__row">
                                                                    {file.imageUrl ? (
                                                                        <button
                                                                            type="button"
                                                                            className="chat-file-pill__thumb-button"
                                                                            onClick={() => {
                                                                                if (imageIndex >= 0) setChatImagePreview({ images: attachedImagePreviews, index: imageIndex });
                                                                            }}
                                                                            title={t('common.preview', 'Preview')}
                                                                        >
                                                                            <img className="chat-file-pill__thumb" src={file.imageUrl} alt="" />
                                                                        </button>
                                                                    ) : (
                                                                        <span className="chat-file-pill__icon">
                                                                            <ChatAttachmentIcon
                                                                                name={file.name}
                                                                                mimeType={file.mimeType}
                                                                                size={16}
                                                                            />
                                                                        </span>
                                                                    )}
                                                                    <span className="chat-file-pill__name">{file.name}</span>
                                                                    {file.source === 'workspace_auto' && <span className="chat-file-pill__source">Workspace</span>}
                                                                    <button
                                                                        type="button"
                                                                        className="chat-file-pill__remove"
                                                                        onClick={() => {
                                                                            if (file.source === 'workspace_auto' && file.path) dismissedWorkspaceRefPath.current = file.path;
                                                                            setAttachedFiles((prev) => prev.filter((_, i) => i !== idx));
                                                                        }}
                                                                        title="Remove file"
                                                                    >
                                                                        ×
                                                                    </button>
                                                                </div>
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            )}
                                            <div className="chat-composer-input-block">
                                                <textarea
                                                    ref={chatInputRef}
                                                    className="chat-input"
                                                    disabled={showNoModelState || confirmationPending}
                                                    value={chatInput}
                                                    onChange={e => {
                                                        setChatInput(e.target.value);
                                                        // Auto-grow: reset height then expand to scrollHeight
                                                        const el = e.target;
                                                        el.style.height = 'auto';
                                                        el.style.height = el.scrollHeight + 'px';
                                                    }}
                                                    onKeyDown={e => {
                                                        // Enter sends the message; Shift+Enter inserts a newline
                                                        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !isWaiting && !isStreaming && !isStopping) {
                                                            e.preventDefault();
                                                            sendChatMsg();
                                                        }
                                                    }}
                                                    onPaste={handlePaste}
                                                    placeholder={confirmationPending
                                                        ? '请先完成上方确认'
                                                        : showNoModelState
                                                            ? t('agent.chat.noModelPlaceholder', 'Configure a company model to start chatting')
                                                            : (!wsConnected && !!currentUser && sessionUserIdStr(activeSession) === viewerUserIdStr() ? 'Connecting...' : t('chat.placeholder'))}
                                                    rows={1}
                                                />
                                            </div>
                                            <div className="chat-composer-toolbar">
                                                <input type="file" multiple ref={fileInputRef} onChange={handleChatFile} style={{ display: 'none' }} />
                                                <button
                                                    type="button"
                                                    className="chat-composer-btn"
                                                    onClick={() => fileInputRef.current?.click()}
                                                    disabled={showNoModelState || confirmationPending || !wsConnected || chatUploadDrafts.length > 0 || isWaiting || isStreaming || isStopping || attachedFiles.length >= 10}
                                                    title={t('agent.workspace.uploadFile')}
                                                >
                                                    <IconPaperclip size={16} stroke={1.75} />
                                                </button>
                                                <ModelSwitcher
                                                    value={overrideModelId}
                                                    onChange={handleModelChange}
                                                    tenantDefaultId={myTenant?.default_model_id || null}
                                                    disabled={showNoModelState || !wsConnected}
                                                />
                                                <div style={{ flex: 1 }} />
                                                {(isStreaming || isWaiting || isStopping) ? (
                                                    <button
                                                        type="button"
                                                        className="btn btn-stop-generation"
                                                        onClick={() => {
                                                            if (!id || !activeSession?.id) return;
                                                            const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
                                                            const activeSocket = wsMapRef.current[activeRuntimeKey];
                                                            if (activeSocket?.readyState === WebSocket.OPEN) {
                                                                activeSocket.send(JSON.stringify({ type: 'abort' }));
                                                                setIsStopping(true);
                                                                setSessionUiState(activeRuntimeKey, { isStopping: true });
                                                            } else {
                                                                setIsStreaming(false);
                                                                setIsWaiting(false);
                                                                setIsStopping(false);
                                                                setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false, isStopping: false });
                                                            }
                                                        }}
                                                        title={t('chat.stop', 'Stop')}
                                                    >
                                                        <span className="stop-icon" />
                                                    </button>
                                                ) : (
                                                    <button
                                                        type="button"
                                                        className="btn btn-primary chat-composer-send"
                                                        onClick={sendChatMsg}
                                                        disabled={showNoModelState || confirmationPending || !wsConnected || (!chatInput.trim() && attachedFiles.length === 0)}
                                                        title={t('chat.send')}
                                                    >
                                                        <IconSend size={16} stroke={1.75} />
                                                    </button>
                                                )}
                                            </div>
                                        </div>
                                        </div>
                                    </div>
                                )}
                                </div>
                                <AgentSidePanel
                                    liveState={liveState}
                                    workspaceActivePath={workspaceActivePath}
                                    workspaceActivities={workspaceActivities}
                                    workspaceLiveDraft={workspaceLiveDraft}
                                    visible={livePanelVisible}
                                    onToggle={() => setLivePanelVisible(false)}
                                    activeTab={sidePanelTab}
                                    onTabChange={setSidePanelTab}
                                    awareContent={renderAwarePreview()}
                                    workspaceLocked={workspacePreviewLocked}
                                    onWorkspaceSelectPath={handleWorkspaceSelectPath}
                                    onWorkspaceToggleLock={handleWorkspaceToggleLock}
                                    onWorkspaceEditingChange={handleWorkspaceEditingChange}
                                    onWorkspacePathDeleted={handleWorkspacePathDeleted}
                                    canManageWorkspace={canManage}
                                    agentId={id}
                                    sessionId={wsSessionId}
                                    onLiveUpdate={(env, screenshotDataUri) => {
                                        // Refresh the live preview with the final screenshot
                                        // captured by TakeControlPanel on close, so the panel
                                        // reflects the state the user left the browser in.
                                        setLiveState(prev => ({
                                            ...prev,
                                            [env]: { screenshotUrl: screenshotDataUri },
                                        }));
                                    }}
                                />
                            </div>
                        </div>
                    )
                }

                {
                    activeTab === 'activityLog' && (() => {
                        // Category definitions
                        const userActionTypes = ['chat_reply', 'tool_call', 'task_created', 'task_updated', 'file_written', 'error'];
                        const heartbeatTypes = ['heartbeat'];
                        const scheduleTypes = ['schedule_run'];
                        const messageTypes = ['feishu_msg_sent', 'agent_msg_sent', 'web_msg_sent'];

                        let filteredLogs = activityLogs;
                        if (logFilter === 'user') {
                            filteredLogs = activityLogs.filter((l: any) => userActionTypes.includes(l.action_type));
                        } else if (logFilter === 'backend') {
                            filteredLogs = activityLogs.filter((l: any) => !userActionTypes.includes(l.action_type));
                        } else if (logFilter === 'heartbeat') {
                            filteredLogs = activityLogs.filter((l: any) => heartbeatTypes.includes(l.action_type));
                        } else if (logFilter === 'schedule') {
                            filteredLogs = activityLogs.filter((l: any) => scheduleTypes.includes(l.action_type));
                        } else if (logFilter === 'messages') {
                            filteredLogs = activityLogs.filter((l: any) => messageTypes.includes(l.action_type));
                        }

                        const filterBtn = (key: string, label: React.ReactNode, indent = false) => (
                            <button
                                key={key}
                                onClick={() => setLogFilter(key)}
                                style={{
                                    padding: indent ? '4px 10px 4px 20px' : '6px 14px',
                                    fontSize: indent ? '11px' : '12px',
                                    fontWeight: logFilter === key ? 600 : 400,
                                    color: logFilter === key ? 'var(--accent-primary)' : 'var(--text-secondary)',
                                    background: logFilter === key ? 'rgba(99,102,241,0.1)' : 'transparent',
                                    border: logFilter === key ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                                    borderRadius: '6px',
                                    cursor: 'pointer',
                                    transition: 'all 0.15s',
                                    whiteSpace: 'nowrap' as const,
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    gap: '5px',
                                }}
                            >
                                {label}
                            </button>
                        );

                        return (
                            <div>
                                <h3 style={{ marginBottom: '12px' }}>{t('agent.activityLog.title')}</h3>

                                {/* Filter tabs */}
                                <div style={{ display: 'flex', gap: '6px', marginBottom: '16px', flexWrap: 'wrap', alignItems: 'center' }}>
                                    {filterBtn('user', <><IconUser size={13} stroke={1.8} /> {t('agent.activityLog.userActions', 'User Actions')}</>)}
                                    {(agent as any)?.agent_type !== 'openclaw' && (<>
                                        {filterBtn('backend', <><IconSettings size={13} stroke={1.8} /> {t('agent.activityLog.backendServices', 'Backend Services')}</>)}
                                        {(logFilter === 'backend' || logFilter === 'heartbeat' || logFilter === 'schedule' || logFilter === 'messages') && (
                                            <>
                                                <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>│</span>
                                                {filterBtn('heartbeat', <><IconHeartbeat size={13} stroke={1.8} /> {t('agent.mind.heartbeatTitle')}</>)}
                                                {filterBtn('schedule', <><IconClock size={13} stroke={1.8} /> {t('agent.activityLog.scheduleCron')}</>, true)}
                                                {filterBtn('messages', <><IconMailForward size={13} stroke={1.8} /> {t('agent.activityLog.messages')}</>, true)}
                                            </>
                                        )}
                                    </>)}
                                </div>

                                {filteredLogs.length > 0 ? (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                        {filteredLogs.map((log: any) => {
                                            const icons: Record<string, React.ReactNode> = {
                                                chat_reply: <IconMessageCircle size={16} stroke={1.8} />,
                                                tool_call: <IconBolt size={16} stroke={1.8} />,
                                                feishu_msg_sent: <IconSend size={16} stroke={1.8} />,
                                                agent_msg_sent: <IconRobot size={16} stroke={1.8} />,
                                                web_msg_sent: <IconWorld size={16} stroke={1.8} />,
                                                task_created: <IconFileText size={16} stroke={1.8} />,
                                                task_updated: <IconCheck size={16} stroke={1.8} />,
                                                file_written: <IconFileText size={16} stroke={1.8} />,
                                                error: <IconAlertTriangle size={16} stroke={1.8} />,
                                                schedule_run: <IconClock size={16} stroke={1.8} />,
                                                heartbeat: <IconHeartbeat size={16} stroke={1.8} />,
                                            };
                                            const time = log.created_at ? new Date(log.created_at).toLocaleString('zh-CN', {
                                                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
                                            }) : '';
                                            const isExpanded = expandedLogId === log.id;
                                            return (
                                                <div key={log.id}
                                                    onClick={() => setExpandedLogId(isExpanded ? null : log.id)}
                                                    style={{
                                                        padding: '10px 14px', borderRadius: '8px', cursor: 'pointer',
                                                        background: isExpanded ? 'var(--bg-elevated)' : 'var(--bg-secondary)', fontSize: '13px',
                                                        border: isExpanded ? '1px solid var(--accent-primary)' : '1px solid transparent',
                                                        transition: 'all 0.15s ease',
                                                    }}
                                                >
                                                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                                                        <span style={{ width: '18px', height: '18px', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: '1px', color: 'var(--text-tertiary)' }}>
                                                            {icons[log.action_type] || '·'}
                                                        </span>
                                                        <div style={{ flex: 1, minWidth: 0 }}>
                                                            <div style={{ fontWeight: 500, marginBottom: '2px' }}>{log.summary}</div>
                                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                                {time} · {log.action_type}
                                                                {log.detail && !isExpanded && <span style={{ marginLeft: '8px', color: 'var(--accent-primary)' }}>▸ Details</span>}
                                                            </div>
                                                        </div>
                                                    </div>
                                                    {isExpanded && log.detail && (
                                                        <div style={{ marginTop: '8px', padding: '10px', borderRadius: '6px', background: 'var(--bg-primary)', fontSize: '12px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-all', lineHeight: '1.6', color: 'var(--text-secondary)', maxHeight: '300px', overflowY: 'auto' }}>
                                                            {Object.entries(log.detail).map(([k, v]: [string, any]) => (
                                                                <div key={k} style={{ marginBottom: '6px' }}>
                                                                    <span style={{ color: 'var(--accent-primary)', fontWeight: 600 }}>{k}:</span>{' '}
                                                                    <span>{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</span>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                ) : (
                                    <div className="card" style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
                                        {t('agent.activityLog.noRecords')}
                                    </div>
                                )}
                            </div>
                        );
                    })()
                }

                {/* ── Feishu Channel Tab ── */}

                {/* ── Approvals Tab ── */}
                {
                    activeTab === 'approvals' && id && <ApprovalsTab agentId={id} canManage={canManage} />
                }

                {/* ── Settings Tab ── */}
                {
                    activeTab === 'settings' && id && (
                        <SettingsTab
                            agent={agent}
                            agentId={id}
                            canManage={canManage}
                            llmModels={llmModels}
                            settingsForm={settingsForm}
                            setSettingsForm={setSettingsForm}
                            settingsSaved={settingsSaved}
                            settingsError={settingsError}
                            settingsSaving={settingsSaving}
                            hasChanges={hasSettingsChanges}
                            onSaveSettings={handleSaveSettings}
                            wmDraft={wmDraft}
                            setWmDraft={setWmDraft}
                            wmSaved={wmSaved}
                            onSaveWelcomeMessage={handleSaveWelcomeMessage}
                            accessPermissionsPanel={(
                                <AccessPermissionsPanel
                                    agentId={id}
                                    permData={permData}
                                    canManage={canManage}
                                    queryClient={queryClient}
                                />
                            )}
                            queryClient={queryClient}
                            formatTokens={formatTokens}
                            showDeleteConfirm={showDeleteConfirm}
                            setShowDeleteConfirm={setShowDeleteConfirm}
                            onDeleteAgent={async () => {
                                try {
                                    await agentApi.delete(id);
                                    queryClient.invalidateQueries({ queryKey: ['agents'] });
                                    navigate('/');
                                } catch (err: any) {
                                    await dialog.alert('删除数字员工失败', { type: 'error', details: String(err?.message || err) });
                                }
                            }}
                        />
                    )
                }
            </div >

            <PromptModal
                open={!!promptModal}
                title={promptModal?.title || ''}
                placeholder={promptModal?.placeholder || ''}
                onCancel={() => setPromptModal(null)}
                onConfirm={async (value) => {
                    const action = promptModal?.action;
                    setPromptModal(null);
                    if (action === 'newFolder') {
                        await fileApi.write(id!, `${workspacePath}/${value}/.gitkeep`, '');
                        queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                    } else if (action === 'newFile') {
                        await fileApi.write(id!, `${workspacePath}/${value}`, '');
                        queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                        setViewingFile(`${workspacePath}/${value}`);
                        setFileEditing(true);
                        setFileDraft('');
                    } else if (action === 'newSkill') {
                        const template = `---\nname: ${value}\ndescription: Describe what this skill does\n---\n\n# ${value}\n\n## Overview\nDescribe the purpose and when to use this skill.\n\n## Process\n1. Step one\n2. Step two\n\n## Output Format\nDescribe the expected output format.\n`;
                        await fileApi.write(id!, `skills/${value}/SKILL.md`, template);
                        queryClient.invalidateQueries({ queryKey: ['files', id, 'skills'] });
                        setViewingFile(`skills/${value}/SKILL.md`);
                        setFileEditing(true);
                        setFileDraft(template);
                    }
                }}
            />

            <ConfirmModal
                open={!!deleteConfirm}
                title={t('common.delete')}
                message={`${t('common.delete')}: ${deleteConfirm?.name}?`}
                confirmLabel={t('common.delete')}
                danger
                onCancel={() => setDeleteConfirm(null)}
                onConfirm={async () => {
                    const path = deleteConfirm?.path;
                    setDeleteConfirm(null);
                    if (path) {
                        try {
                            await fileApi.delete(id!, path);
                            setViewingFile(null);
                            setFileEditing(false);
                            queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                            showToast(t('common.delete'));
                        } catch (err: any) {
                            showToast(t('agent.upload.failed'), 'error');
                        }
                    }
                }}
            />

            <ChatImageLightbox
                open={!!chatImagePreview}
                images={chatImagePreview?.images || []}
                index={chatImagePreview?.index || 0}
                mode="desktop"
                onClose={() => setChatImagePreview(null)}
                onIndexChange={(index) => setChatImagePreview((prev) => prev ? { ...prev, index } : prev)}
            />

            <SessionViewerDrawer
                agentId={id!}
                agentName={(agent as any)?.name || 'Agent'}
                target={subagentSessionRun?.sessionId ? {
                    sessionId: subagentSessionRun.sessionId,
                    agentId: subagentSessionRun.executionAgentId,
                    title: subagentSessionRun.name || subagentSessionRun.task,
                    status: subagentSessionRun.status,
                    mode: subagentSessionRun.mode,
                    model: subagentSessionRun.model,
                } : null}
                onClose={closeSubagentSession}
                unavailableAttachmentKeys={unavailableAttachmentKeys}
                onAttachmentDownload={handleAttachmentDownload}
                onAttachmentUnavailable={markAttachmentUnavailable}
                onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
            />

            {
                uploadToast && (
                    <div style={{
                        position: 'fixed', top: '20px', right: '20px', zIndex: 20000,
                        padding: '12px 20px', borderRadius: '8px',
                        background: uploadToast.type === 'success' ? 'rgba(34, 197, 94, 0.9)' : 'rgba(239, 68, 68, 0.9)',
                        color: '#fff', fontSize: '14px', fontWeight: 500,
                        boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                    }}>
                        {''}{uploadToast.message}
                    </div>
                )
            }

            {/* ── Expiry Editor Modal (admin only) ── */}
            {
                showExpiryModal && (
                    <div className="agent-expiry-modal-backdrop"
                        onClick={() => setShowExpiryModal(false)}>
                        <div className="agent-expiry-modal"
                            onClick={e => e.stopPropagation()}>
                            <div className="agent-expiry-modal-header">
                                <div>
                                    <h3>{t('agent.settings.expiry.title')}</h3>
                                    <div className="agent-expiry-current">
                                        {(agent as any).is_expired
                                            ? <span className="agent-expiry-status agent-expiry-status--expired">{t('agent.settings.expiry.expired')}</span>
                                            : (agent as any).expires_at
                                                ? <>{t('agent.settings.expiry.currentExpiry')} <strong>{new Date((agent as any).expires_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US')}</strong></>
                                                : <span className="agent-expiry-status">{t('agent.settings.expiry.neverExpires')}</span>
                                        }
                                    </div>
                                </div>
                                <button className="agent-expiry-close" onClick={() => setShowExpiryModal(false)} aria-label={t('common.close', 'Close')}>×</button>
                            </div>
                            <div className="agent-expiry-section">
                                <div className="agent-expiry-label">{t('agent.settings.expiry.quickRenew')}</div>
                                <div className="agent-expiry-quick-actions">
                                    {([
                                        ['+ 24h', 24],
                                        [`+ ${t('agent.settings.expiry.days', { count: 7 })}`, 168],
                                        [`+ ${t('agent.settings.expiry.days', { count: 30 })}`, 720],
                                        [`+ ${t('agent.settings.expiry.days', { count: 90 })}`, 2160],
                                    ] as [string, number][]).map(([label, h]) => (
                                        <button key={h} onClick={() => addHours(h)}
                                            className={`agent-expiry-chip${expiryQuickHours === h ? ' agent-expiry-chip--selected' : ''}`}
                                            aria-pressed={expiryQuickHours === h}>
                                            {label}
                                        </button>
                                    ))}
                                </div>
                            </div>
                            <div className="agent-expiry-section">
                                <div className="agent-expiry-label">{t('agent.settings.expiry.customDeadline')}</div>
                                <input type="datetime-local" value={expiryValue} onChange={e => {
                                    setExpiryValue(e.target.value);
                                    setExpiryQuickHours(null);
                                }}
                                    className="agent-expiry-input" />
                            </div>
                            <div className="agent-expiry-actions">
                                <button onClick={() => saveExpiry(true)} disabled={expirySaving}
                                    className="agent-expiry-secondary-action">
                                    {t('agent.settings.expiry.neverExpires')}
                                </button>
                                <div className="agent-expiry-action-group">
                                    <button onClick={() => setShowExpiryModal(false)} disabled={expirySaving}
                                        className="agent-expiry-secondary-action">
                                        {t('common.cancel')}
                                    </button>
                                    <button onClick={() => saveExpiry(false)} disabled={expirySaving || !expiryValue}
                                        className="agent-expiry-primary-action">
                                        {expirySaving ? t('agent.settings.expiry.saving') : t('common.save')}
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                )
            }

        </>
    );
}
