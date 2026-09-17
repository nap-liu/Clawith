import { getChatToolRenderType } from '../../components/ChatToolCallRenderer';
import type { AgentAccessDepartment, AgentAccessUser } from '../../components/OrgMemberAccessPicker';
import type { LivePreviewState } from '../../components/AgentBayLivePanel';
import type { WorkspaceLiveDraft } from '../../components/WorkspaceOperationPanel';
import type { FocusApiItem } from '../../services/api';

export const WORKSPACE_TOOLS = new Set([
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

export const AWARE_TOOLS = new Set(['set_trigger', 'update_trigger', 'cancel_trigger', 'delete_trigger', 'list_triggers', 'list_focus_items', 'upsert_focus_item', 'complete_focus_item']);
export const SESSION_PAGE_SIZE = 40;
export const mergeSessionsById = (first: any[], second: any[]) => {
    const seen = new Set<string>();
    return [...first, ...second].filter((session) => {
        const sessionId = String(session.id);
        if (seen.has(sessionId)) return false;
        seen.add(sessionId);
        return true;
    });
};
const trimLeadingPictograph = (value: string) => value.replace(/^\p{Extended_Pictographic}\s*/u, '');
export const formatReflectionTitle = (value: string | undefined, isZh: boolean) => {
    const clean = trimLeadingPictograph(value || 'Trigger execution').trim();
    const legacyMatch = clean.match(/^内心独白[:：]\s*(.*)$/);
    if (legacyMatch) return isZh ? `内心独白：${legacyMatch[1]}` : `Reflection: ${legacyMatch[1]}`;
    return clean;
};

export const pendingPcRouteRecoveryRuntimeKeys = new Set<string>();

export type FocusItem = {
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

export type ExecutionUserOption = {
    id: string;
    display_name?: string | null;
    username?: string | null;
    email?: string | null;
};

export const shortIdentity = (userId?: string | null) => userId ? userId.slice(0, 8) : '—';

export function focusItemFromApi(item: FocusApiItem): FocusItem {
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

export const isConfirmationToolCall = (msg: any): boolean => {
    return getChatToolRenderType(msg) === 'confirmation';
};

export const isPendingConfirmationToolCall = (msg: any): boolean => {
    if (!isConfirmationToolCall(msg)) return false;
    const parsed = (() => { try { return JSON.parse(msg.content || '{}'); } catch { return {}; } })();
    const status = msg.toolStatus || parsed.status;
    const args = msg.toolArgs || parsed.args || {};
    return (status === 'running' || status === 'pending')
        && args.force_confirmation !== false;
};

export function isFocusPath(path?: string | null): boolean {
    if (!path) return false;
    const normalized = path.replace(/^\/+/, '').toLowerCase();
    return normalized === 'focus.md' || normalized.endsWith('/focus.md');
}

export function workspaceActionForTool(tool: string): WorkspaceLiveDraft['action'] {
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

export function parseWorkspaceDraftArgs(tool: string, raw: string): Pick<WorkspaceLiveDraft, 'path' | 'content'> {
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

export function parseFocusItems(raw: string): FocusItem[] {
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

export function focusKeyFromTrigger(trig: any): string {
    if (trig?.focus_ref) return String(trig.focus_ref);
    if (isOkrSystemTrigger(trig)) return 'system:okr_reports';
    if (trig?.is_system) return `system:${String(trig.name || 'trigger')}`;
    return String(trig.name || trig.reason || 'trigger_focus');
}

export function synthesizeFocusForTrigger(trig: any): FocusItem {
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

export function parseAgentBayTransferArgs(rawArgs: any): NonNullable<LivePreviewState['transfer']> {
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

export function workspaceFileName(path: string): string {
    return path.replace(/^workspace\//, '') || path;
}

export const formatTokens = (n: number) => {
    if (!n) return '0';
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return String(n);
};

export const formatTokensParts = (n: number): { value: string; unit: string } => {
    if (!n) return { value: '0', unit: '' };
    if (n >= 1000000) return { value: (n / 1000000).toFixed(1), unit: 'M' };
    if (n >= 1000) return { value: (n / 1000).toFixed(1), unit: 'K' };
    return { value: String(n), unit: '' };
};

export function schedToCron(sched: { freq: string; interval: number; time: string; weekdays?: number[] }): string {
    const [h, m] = (sched.time || '09:00').split(':').map(Number);
    if (sched.freq === 'weekly') {
        const days = (sched.weekdays || [1, 2, 3, 4, 5]).join(',');
        return sched.interval > 1 ? `${m} ${h} * * ${days}` : `${m} ${h} * * ${days}`;
    }
    if (sched.interval === 1) return `${m} ${h} * * *`;
    return `${m} ${h} */${sched.interval} * *`;
}

export const getRelationOptions = (t: any) => [
    { value: 'supervisor', label: t('agent.detail.supervisor') },
    { value: 'subordinate', label: t('agent.detail.subordinate') },
    { value: 'collaborator', label: t('agent.detail.collaborator') },
    { value: 'peer', label: t('agent.detail.peer') },
    { value: 'mentor', label: t('agent.detail.mentor') },
    { value: 'stakeholder', label: t('agent.detail.stakeholder') },
    { value: 'other', label: t('agent.detail.other') },
];

export const getAgentRelationOptions = getRelationOptions;

export type AccessUser = AgentAccessUser;
export type AccessDepartment = AgentAccessDepartment;
