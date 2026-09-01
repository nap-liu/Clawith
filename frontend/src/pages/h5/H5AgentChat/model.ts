import { authApi } from '../../../services/api';
import type { TokenResponse } from '../../../types';
import { createClientId } from '../../../utils/clientId';
import {
    buildH5ConversationEntries,
    type H5AnalysisItem,
} from '../chatTimeline';

export type AuthStatus = 'checking' | 'exchanging' | 'ready' | 'error';
export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'disconnected';
export type BufferedH5SocketEvent = { data: any; socket: WebSocket };
export type H5UploadDraft = { id: string; name: string; percent: number; previewUrl?: string; sizeBytes: number };
export type H5SessionSummary = {
    id: string;
    title: string;
    source_channel: string;
    created_at: string;
    last_message_at?: string | null;
    message_count: number;
    unread_count: number;
    is_primary: boolean;
};

export const VIRTUALIZE_ENTRY_THRESHOLD = 40;
export const H5_SESSION_PAGE_SIZE = 50;
export const STREAM_BATCH_DELAY_MS = 40;
export const QUICK_ACTIONS_MENU_CLOSE_MS = 180;

export const OAUTH_TRANSIENT_PARAMS = [
    'code',
    'state',
    'error',
    'error_description',
    'scope',
    'authuser',
    'prompt',
    'session_state',
];

export type CodeExchangePayload = Parameters<typeof authApi.exchangeCode>[0];

export const codeExchangePromises = new Map<string, Promise<TokenResponse>>();
export const consumedExchangeCodes = new Set<string>();

export const makeId = createClientId;

export function buildCodeExchangeRedirectUri(href: string) {
    const url = new URL(href);
    for (const key of OAUTH_TRANSIENT_PARAMS) url.searchParams.delete(key);
    return url.toString();
}

export function cleanedOAuthAddress(href: string) {
    const url = new URL(href);
    for (const key of OAUTH_TRANSIENT_PARAMS) url.searchParams.delete(key);
    const search = url.searchParams.toString();
    return `${url.pathname}${search ? `?${search}` : ''}${url.hash}`;
}

export function exchangeCodeOnce(key: string, payload: CodeExchangePayload) {
    const existing = codeExchangePromises.get(key);
    if (existing) return existing;
    const promise = authApi.exchangeCode(payload).finally(() => {
        codeExchangePromises.delete(key);
    });
    codeExchangePromises.set(key, promise);
    return promise;
}

export function titleCaseToolName(name: string) {
    return (name || 'tool')
        .replace(/^mcp[_:-]/i, '')
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, ch => ch.toUpperCase());
}

export function firstString(...values: any[]) {
    for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
}

export function h5ToolTitle(item: Extract<H5AnalysisItem, { type: 'tool' }>) {
    const args = item.args && typeof item.args === 'object' && !Array.isArray(item.args) ? item.args : {};
    const path = firstString(args.output_path, args.path, args.file_path, args.filename, args.name);
    const url = firstString(args.url, args.link, args.uri);
    const query = firstString(args.query, args.q, args.keyword, args.search);
    const recipient = firstString(args.to, args.recipient, args.user, args.channel, args.agent_name);
    const target = path || url || query || recipient;
    const base = titleCaseToolName(item.name);
    return target ? `${base}: ${target}` : base;
}

export function h5AnalysisTitle(items: H5AnalysisItem[]) {
    const toolItems = items.filter((item) => item.type === 'tool') as Extract<H5AnalysisItem, { type: 'tool' }>[];
    const runningTool = [...toolItems].reverse().find((item) => item.status === 'running');
    if (runningTool) return `正在使用：${h5ToolTitle(runningTool)}`;
    if (toolItems.length > 0) return `已执行 ${toolItems.length} 个工具`;
    return '思考过程';
}

export function stringifyDetail(value: any) {
    if (value == null || value === '') return '';
    if (typeof value === 'string') return value;
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

export function formatFileSize(bytes: number) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '';
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

export function stripAttachmentDisplayPrefix(content: string) {
    return (content || '').replace(/^(?:\[Attachment: [^\]]+\]\s*)+/, '').trim();
}

export function resolveAgentAvatarUrl(avatarUrl: string | null | undefined, token: string | null | undefined) {
    if (!avatarUrl) return '';
    if (!avatarUrl.startsWith('/api') || !token) return avatarUrl;
    return `${avatarUrl}${avatarUrl.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
}

export function normalizeH5SessionSummary(row: any): H5SessionSummary | null {
    if (!row || row.id == null) return null;
    return {
        id: String(row.id),
        title: String(row.title || row.group_name || '未命名会话'),
        source_channel: String(row.source_channel || 'web'),
        created_at: String(row.created_at || ''),
        last_message_at: row.last_message_at ? String(row.last_message_at) : null,
        message_count: Number(row.message_count || 0),
        unread_count: Number(row.unread_count || 0),
        is_primary: Boolean(row.is_primary),
    };
}

export function h5SessionChannelLabel(channel: string) {
    const labels: Record<string, string> = {
        web: 'Web',
        miniprogram: '小程序',
        wechat_miniprogram: '小程序',
        feishu: '飞书',
        dingtalk: '钉钉',
        wecom: '企业微信',
        slack: 'Slack',
        discord: 'Discord',
    };
    return labels[channel] || channel || '未知渠道';
}

export function formatH5SessionTime(value?: string | null) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const locale = typeof navigator !== 'undefined' ? navigator.language : 'zh-CN';
    return new Intl.DateTimeFormat(locale, {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    }).format(date);
}

export function estimateConversationEntrySize(entry: ReturnType<typeof buildH5ConversationEntries>[number], expanded: boolean) {
    if (entry.type === 'analysis_group') {
        return expanded ? Math.min(360, 52 + entry.items.length * 74) : 52;
    }
    const msg = entry.msg;
    if (msg.role === 'tool_call') return 190;
    const text = `${msg.content || ''}${msg.thinking || ''}`;
    return Math.min(360, Math.max(54, 38 + Math.ceil(text.length / 18) * 24));
}
