import type { ChatPreviewImage } from '../../utils/chatAttachments';
import { parseFileDeliveryToolResult, type ChatFileDelivery } from '../../utils/chatFileDelivery';

export type H5ToolStatus = 'running' | 'done';

export type H5ChatMessage = {
    id: string;
    role: 'user' | 'assistant' | 'system' | 'tool_call';
    content: string;
    thinking?: string;
    streaming?: boolean;
    created_at?: string | null;
    toolCallId?: string;
    toolName?: string;
    toolArgs?: any;
    toolStatus?: H5ToolStatus;
    toolResult?: string;
    toolThinking?: string;
    fileName?: string;
    imageUrl?: string;
    previewImages?: ChatPreviewImage[];
};

export type H5AnalysisItem =
    | { type: 'thinking'; content: string }
    | { type: 'tool'; name: string; args: any; status: H5ToolStatus; result?: string };

export type H5ConversationEntry =
    | { type: 'analysis_group'; items: H5AnalysisItem[]; key: string; running: boolean }
    | { type: 'file_delivery'; delivery: ChatFileDelivery; msg: H5ChatMessage; key: string }
    | { type: 'message'; msg: H5ChatMessage; key: string };

export type H5AssistantStreamMessage = {
    type: 'thinking' | 'chunk' | 'done';
    content?: string;
    now?: string;
};

const CONFIRMATION_TOOL = 'request_confirmation';

const defaultMakeId = () => {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID();
    }
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

export function parseToolArgs(raw: any): Record<string, any> {
    if (!raw) return {};
    if (typeof raw === 'object') return raw;
    if (typeof raw !== 'string') return {};
    try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
        return {};
    }
}

function parseStoredToolPayload(content: any): Record<string, any> {
    if (!content || typeof content !== 'string') return {};
    try {
        const parsed = JSON.parse(content);
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
        return {};
    }
}

function normalizeToolStatus(status: any): H5ToolStatus {
    return status === 'running' ? 'running' : 'done';
}

function normalizeToolResult(result: any): string | undefined {
    if (result == null) return undefined;
    if (typeof result === 'string') return result;
    try {
        return JSON.stringify(result);
    } catch {
        return String(result);
    }
}

export function mapHistoryMessage(raw: any, makeId: () => string = defaultMakeId): H5ChatMessage | null {
    if (!raw || !['user', 'assistant', 'system', 'tool_call'].includes(raw.role)) return null;

    if (raw.role === 'tool_call') {
        const parsed = parseStoredToolPayload(raw.content);
        const id = String(raw.toolCallId || raw.id || makeId());
        const toolArgs = raw.toolArgs ?? parsed.args ?? parsed.arguments ?? {};
        return {
            id,
            role: 'tool_call',
            content: parsed.name ? '' : (raw.content || ''),
            created_at: raw.created_at || null,
            toolCallId: String(raw.toolCallId || parsed.call_id || parsed.id || id),
            toolName: raw.toolName || parsed.name || parsed.tool_name || 'tool',
            toolArgs,
            toolStatus: normalizeToolStatus(raw.toolStatus || parsed.status),
            toolResult: normalizeToolResult(raw.toolResult ?? parsed.result) || '',
            toolThinking: raw.toolThinking || parsed.reasoning_content || '',
        };
    }

    return {
        id: String(raw.id || makeId()),
        role: raw.role,
        content: raw.content || '',
        thinking: raw.thinking || undefined,
        created_at: raw.created_at || null,
    };
}

export function findStreamingAssistantIndex(messages: H5ChatMessage[]) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const msg = messages[i];
        if (msg.role === 'assistant' && msg.streaming) return i;
    }
    return -1;
}

function findStreamingAssistantIndexAfterLastTool(messages: H5ChatMessage[]) {
    let lastToolIndex = -1;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        if (messages[i].role === 'tool_call') {
            lastToolIndex = i;
            break;
        }
    }
    for (let i = messages.length - 1; i > lastToolIndex; i -= 1) {
        const msg = messages[i];
        if (msg.role === 'assistant' && msg.streaming) return i;
    }
    return -1;
}

export function applyAssistantStreamMessage(
    messages: H5ChatMessage[],
    event: H5AssistantStreamMessage,
    makeId: () => string = defaultMakeId,
): H5ChatMessage[] {
    const idx = findStreamingAssistantIndexAfterLastTool(messages);
    const content = event.content || '';
    const now = event.now || new Date().toISOString();

    if (event.type === 'thinking') {
        if (idx >= 0) {
            const next = [...messages];
            next[idx] = { ...next[idx], thinking: (next[idx].thinking || '') + content };
            return next;
        }
        return [...messages, { id: makeId(), role: 'assistant', content: '', thinking: content, streaming: true }];
    }

    if (event.type === 'chunk') {
        if (idx >= 0) {
            const next = [...messages];
            next[idx] = { ...next[idx], content: next[idx].content + content };
            return next;
        }
        return [...messages, { id: makeId(), role: 'assistant', content, streaming: true }];
    }

    if (idx >= 0) {
        const next = [...messages];
        next[idx] = {
            ...next[idx],
            content: content || next[idx].content,
            streaming: false,
            created_at: now,
        };
        return next;
    }
    if (!content) return messages;
    return [...messages, {
        id: makeId(),
        role: 'assistant',
        content,
        created_at: now,
    }];
}

export function isSameMessage(a: H5ChatMessage, b: H5ChatMessage) {
    if (a.role === 'tool_call' || b.role === 'tool_call') {
        if (a.role !== b.role) return false;
        if (a.toolCallId && a.toolCallId === b.toolCallId) return true;
        const aDelivery = fileDeliveryFromMessage(a);
        const bDelivery = fileDeliveryFromMessage(b);
        return !!aDelivery
            && !!bDelivery
            && aDelivery.path === bDelivery.path
            && aDelivery.filename === bDelivery.filename
            && (aDelivery.message || '') === (bDelivery.message || '');
    }
    return a.role === b.role
        && a.content === b.content
        && (a.thinking || '') === (b.thinking || '')
        && (a.toolCallId || '') === (b.toolCallId || '');
}

export function mergeHistoryMessages(prev: H5ChatMessage[], history: H5ChatMessage[]) {
    if (history.length === 0) return prev;

    const usedHistoryIndexes = new Set<number>();
    const localOnly: H5ChatMessage[] = [];

    for (const local of prev) {
        const historyIndex = history.findIndex((item, index) => (
            !usedHistoryIndexes.has(index) && isSameMessage(local, item)
        ));
        if (historyIndex >= 0) {
            usedHistoryIndexes.add(historyIndex);
            continue;
        }

        if (local.streaming && history.some((item) => (
            item.role === 'assistant'
            && !!item.content
            && (!local.content || item.content.includes(local.content))
        ))) {
            continue;
        }

        localOnly.push(local);
    }

    return [...history, ...localOnly];
}

export function getToolTargetKey(args: any): string {
    const parsed = parseToolArgs(args);
    const value = parsed.path
        || parsed.file_path
        || parsed.output_path
        || parsed.target_path
        || parsed.filename
        || parsed.url
        || parsed.query
        || parsed.name
        || '';
    return typeof value === 'string' ? value.trim() : '';
}

export function upsertToolCallMessage(messages: H5ChatMessage[], toolMsg: H5ChatMessage) {
    const incomingTarget = getToolTargetKey(toolMsg.toolArgs);
    const exactIdMatch = (msg: H5ChatMessage) => (
        msg.role === 'tool_call'
        && !!toolMsg.toolCallId
        && msg.toolCallId === toolMsg.toolCallId
    );
    const sameTool = (msg: H5ChatMessage) => (
        exactIdMatch(msg)
        || (
            msg.role === 'tool_call'
            && msg.toolName === toolMsg.toolName
            && msg.toolStatus === 'running'
            && (
                (!!incomingTarget && getToolTargetKey(msg.toolArgs) === incomingTarget)
                || (!toolMsg.toolCallId && !incomingTarget)
            )
        )
    );
    const runningIdx = [...messages].reverse().findIndex(sameTool);
    if (runningIdx < 0) return [...messages, toolMsg];

    const idx = messages.length - 1 - runningIdx;
    const previous = messages[idx];
    const nextToolArgs = Object.keys(parseToolArgs(toolMsg.toolArgs)).length > 0
        ? toolMsg.toolArgs
        : previous.toolArgs;
    const merged = {
        ...previous,
        ...toolMsg,
        toolArgs: nextToolArgs,
        id: previous.id || toolMsg.id,
        created_at: previous.created_at || toolMsg.created_at,
    };
    return [...messages.slice(0, idx), merged, ...messages.slice(idx + 1)];
}

export function toolCallMessageFromEvent(data: any, makeId: () => string = defaultMakeId, now = new Date().toISOString()): H5ChatMessage {
    const toolName = data.name || data.toolName || 'tool';
    const callId = String(data.call_id || data.toolCallId || data.id || `${toolName}-${data.index ?? 0}`);
    const status = normalizeToolStatus(data.status);
    return {
        id: callId || makeId(),
        role: 'tool_call',
        content: '',
        created_at: now,
        streaming: status === 'running',
        toolCallId: callId,
        toolName,
        toolArgs: parseToolArgs(data.args ?? data.toolArgs),
        toolStatus: status,
        toolResult: normalizeToolResult(data.result ?? data.toolResult) || '',
        toolThinking: data.reasoning_content || data.toolThinking || '',
    };
}

export function isConfirmationToolCall(msg: H5ChatMessage) {
    if (msg.role !== 'tool_call') return false;
    const parsed = parseStoredToolPayload(msg.content);
    return (msg.toolName || parsed.name || '').toLowerCase() === CONFIRMATION_TOOL;
}

function pushThinking(items: H5AnalysisItem[], content?: string) {
    const text = (content || '').trim();
    if (!text) return;
    const previous = items[items.length - 1];
    if (previous?.type === 'thinking' && previous.content === text) return;
    items.push({ type: 'thinking', content: text });
}

function toolItemFromMessage(msg: H5ChatMessage): Extract<H5AnalysisItem, { type: 'tool' }> {
    const parsed = parseStoredToolPayload(msg.content);
    const name = msg.toolName || parsed.name || 'tool';
    const args = msg.toolArgs ?? parsed.args ?? {};
    const result = normalizeToolResult(msg.toolResult ?? parsed.result);
    return {
        type: 'tool',
        name,
        args,
        status: msg.toolStatus === 'running' ? 'running' : 'done',
        result: result || undefined,
    };
}

function fileDeliveryFromMessage(msg: H5ChatMessage): ChatFileDelivery | null {
    if (msg.role !== 'tool_call') return null;
    const parsed = parseStoredToolPayload(msg.content);
    const toolName = msg.toolName || parsed.name || '';
    const toolArgs = msg.toolArgs ?? parsed.args ?? {};
    const toolResult = msg.toolResult || parsed.result || (!parsed.name ? msg.content : undefined);
    return parseFileDeliveryToolResult(toolName, toolResult, toolArgs, msg.toolCallId);
}

export function buildH5ConversationEntries(messages: H5ChatMessage[]): H5ConversationEntry[] {
    const msgClass: ('analysis' | 'final')[] = new Array(messages.length).fill('final');
    let hasFutureTool = false;

    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const msg = messages[i];
        if (fileDeliveryFromMessage(msg)) {
            msgClass[i] = 'final';
        } else if (msg.role === 'tool_call' && !isConfirmationToolCall(msg)) {
            msgClass[i] = 'analysis';
            hasFutureTool = true;
        } else if (msg.role === 'user') {
            hasFutureTool = false;
        } else if (msg.role === 'assistant' && hasFutureTool) {
            msgClass[i] = 'analysis';
        }
    }

    const grouped: H5ConversationEntry[] = [];
    let currentGroup: H5AnalysisItem[] | null = null;
    let groupStartIndex = 0;

    const flushGroup = () => {
        if (!currentGroup || currentGroup.length === 0) {
            currentGroup = null;
            return;
        }
        grouped.push({
            type: 'analysis_group',
            items: currentGroup,
            key: `analysis-${groupStartIndex}`,
            running: currentGroup.some((item) => item.type === 'tool' && item.status === 'running'),
        });
        currentGroup = null;
    };

    for (let i = 0; i < messages.length; i += 1) {
        const msg = messages[i];
        const fileDelivery = fileDeliveryFromMessage(msg);
        if (fileDelivery) {
            flushGroup();
            grouped.push({
                type: 'file_delivery',
                delivery: fileDelivery,
                msg,
                key: `file-delivery-${fileDelivery.id}`,
            });
            continue;
        }

        if (msgClass[i] === 'analysis') {
            if (!currentGroup) {
                currentGroup = [];
                groupStartIndex = i;
            }
            if (msg.role === 'tool_call') {
                pushThinking(currentGroup, msg.toolThinking);
                currentGroup.push(toolItemFromMessage(msg));
            } else if (msg.role === 'assistant') {
                pushThinking(currentGroup, msg.thinking);
                pushThinking(currentGroup, msg.content);
            }
            continue;
        }

        if (msg.role === 'assistant' && msg.thinking && currentGroup?.some((item) => item.type === 'tool')) {
            pushThinking(currentGroup, msg.thinking);
            const contentText = msg.content?.trim() || '';
            flushGroup();
            if (contentText) grouped.push({ type: 'message', msg: { ...msg, thinking: undefined }, key: msg.id || `msg-${i}` });
            continue;
        }

        flushGroup();
        grouped.push({ type: 'message', msg, key: msg.id || `msg-${i}` });
    }

    flushGroup();
    return grouped;
}

function messageAnchor(msg: H5ChatMessage) {
    return [
        msg.role,
        msg.id,
        msg.streaming ? 'streaming' : 'done',
        msg.content.length,
        (msg.thinking || '').length,
        msg.toolStatus || '',
        msg.toolResult?.length || 0,
    ].join(':');
}

function analysisAnchor(items: H5AnalysisItem[]) {
    return items.map((item) => {
        if (item.type === 'thinking') return `thinking:${item.content.length}`;
        return `tool:${item.name}:${item.status}:${item.result?.length || 0}`;
    }).join('|');
}

export function getH5ScrollAnchor(entries: H5ConversationEntry[], isWaiting: boolean) {
    const last = entries[entries.length - 1];
    const lastAnchor = !last
        ? 'empty'
        : last.type === 'analysis_group'
            ? `${last.key}:${analysisAnchor(last.items)}`
            : last.type === 'file_delivery'
                ? `${last.key}:${last.delivery.path}:${last.delivery.size || 0}:${last.delivery.message?.length || 0}`
                : `${last.key}:${messageAnchor(last.msg)}`;
    return `${entries.length}:${isWaiting ? 'waiting' : 'idle'}:${lastAnchor}`;
}
