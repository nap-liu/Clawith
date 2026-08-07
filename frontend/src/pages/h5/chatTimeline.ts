import type { ChatMessageAttachment, ChatPreviewImage } from '../../utils/chatAttachments';
import { createClientId } from '../../utils/clientId';
import { getChatToolRenderIdentity, getChatToolRenderType } from '../../components/ChatToolCallRenderer';

export type H5ToolStatus = 'running' | 'done';

export type H5ChatMessage = {
    id: string;
    role: 'user' | 'assistant' | 'system' | 'tool_call';
    content: string;
    thinking?: string;
    streaming?: boolean;
    created_at?: string | null;
    display_content?: string;
    attachments?: ChatMessageAttachment[];
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
    | { type: 'special_render'; renderType: string; msg: H5ChatMessage; key: string }
    | { type: 'message'; msg: H5ChatMessage; key: string };

export type H5AssistantStreamMessage = {
    type: 'thinking' | 'chunk' | 'done';
    content?: string;
    now?: string;
    messageId?: string;
};

const CONFIRMATION_TOOL = 'request_confirmation';

const defaultMakeId = createClientId;

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
    return status === 'running' || status === 'pending' ? 'running' : 'done';
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
        ...(Object.prototype.hasOwnProperty.call(raw, 'display_content') ? { display_content: raw.display_content || '' } : {}),
        ...(Object.prototype.hasOwnProperty.call(raw, 'attachments') ? { attachments: raw.attachments || [] } : {}),
    };
}

export function hasPendingConfirmation(messages: H5ChatMessage[]): boolean {
    return messages.some((message) => (
        message.role === 'tool_call'
        && message.toolName === CONFIRMATION_TOOL
        && message.toolStatus === 'running'
        && parseToolArgs(message.toolArgs).force_confirmation !== false
    ));
}

export function findStreamingAssistantIndex(messages: H5ChatMessage[]) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const msg = messages[i];
        if (msg.role === 'assistant' && (msg.streaming || (msg as any)._streaming)) return i;
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
        if (msg.role === 'assistant' && (msg.streaming || (msg as any)._streaming)) return i;
    }
    return -1;
}

export function applyAssistantDoneMessage<T extends Record<string, any>>(
    messages: T[],
    event: { content?: string; now?: string; messageId?: string },
    makeId: () => string = defaultMakeId,
): T[] {
    const identifiedIdx = event.messageId
        ? messages.findIndex((message) => String(message.id || '') === event.messageId)
        : -1;
    const content = event.content || '';
    const now = event.now || new Date().toISOString();
    const afterLastToolIdx = findStreamingAssistantIndexAfterLastTool(
        messages as unknown as H5ChatMessage[],
    );
    const fallbackStreamingIdx = !content
        ? findStreamingAssistantIndex(messages as unknown as H5ChatMessage[])
        : -1;
    const idx = identifiedIdx >= 0
        ? identifiedIdx
        : afterLastToolIdx >= 0
            ? afterLastToolIdx
            : fallbackStreamingIdx;

    if (idx >= 0) {
        const previous = messages[idx];
        const next = [...messages];
        next[idx] = {
            ...previous,
            content: content || previous.content || '',
            streaming: false,
            _streaming: false,
            created_at: previous.created_at || now,
            timestamp: previous.timestamp || now,
        };
        return next;
    }

    if (!content) return messages;
    const last = messages[messages.length - 1];
    if (
        !event.messageId
        && last?.role === 'assistant'
        && !last.streaming
        && !last._streaming
        && last.content === content
    ) {
        return messages;
    }
    return [...messages, {
        id: event.messageId || makeId(),
        role: 'assistant',
        content,
        created_at: now,
        timestamp: now,
    } as unknown as T];
}

export function applyAssistantStreamMessage(
    messages: H5ChatMessage[],
    event: H5AssistantStreamMessage,
    makeId: () => string = defaultMakeId,
): H5ChatMessage[] {
    const identifiedIdx = event.messageId
        ? messages.findIndex((message) => message.id === event.messageId)
        : -1;
    const idx = identifiedIdx >= 0
        ? identifiedIdx
        : findStreamingAssistantIndexAfterLastTool(messages);
    const content = event.content || '';
    const now = event.now || new Date().toISOString();

    if (event.type === 'thinking') {
        if (idx >= 0) {
            const next = [...messages];
            next[idx] = { ...next[idx], thinking: (next[idx].thinking || '') + content };
            return next;
        }
        return [...messages, {
            id: event.messageId || makeId(),
            role: 'assistant',
            content: '',
            thinking: content,
            streaming: true,
        }];
    }

    if (event.type === 'chunk') {
        if (idx >= 0) {
            const next = [...messages];
            next[idx] = { ...next[idx], content: next[idx].content + content };
            return next;
        }
        return [...messages, {
            id: event.messageId || makeId(),
            role: 'assistant',
            content,
            streaming: true,
        }];
    }

    return applyAssistantDoneMessage(messages, event, makeId);
}

export function isSameMessage(a: H5ChatMessage, b: H5ChatMessage) {
    if (a.role === 'tool_call' || b.role === 'tool_call') {
        if (a.role !== b.role) return false;
        if (a.toolCallId && a.toolCallId === b.toolCallId) return true;
        const aRenderIdentity = getChatToolRenderIdentity(a);
        return !!aRenderIdentity && aRenderIdentity === getChatToolRenderIdentity(b);
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
    if (previous.toolStatus === 'done' && toolMsg.toolStatus !== 'done') {
        return messages;
    }
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

export function normalizeChatTimelineMessages<T extends Record<string, any>>(messages: T[]): T[] {
    const normalized: T[] = [];
    const toolIndexByCallId = new Map<string, number>();

    for (const message of messages) {
        if (message?.role === 'assistant') {
            const hasContent = typeof message.content === 'string'
                ? message.content.trim().length > 0
                : Boolean(message.content);
            const hasThinking = typeof message.thinking === 'string'
                ? message.thinking.trim().length > 0
                : Boolean(message.thinking);
            const hasAttachment = Boolean(
                message.fileName
                || message.imageUrl
                || (Array.isArray(message.previewImages) && message.previewImages.length > 0)
                || (
                    Array.isArray(message.attachments)
                    && message.attachments.some((attachment: ChatMessageAttachment) => (
                        attachment.kind !== 'audio' && attachment.kind !== 'video'
                    ))
                ),
            );
            const isStreaming = Boolean(message.streaming || message._streaming);
            if (!hasContent && !hasThinking && !hasAttachment && !isStreaming) {
                continue;
            }
        }

        if (message?.role !== 'tool_call') {
            normalized.push(message);
            continue;
        }

        const parsed = parseStoredToolPayload(message.content);
        const callId = String(message.toolCallId || parsed.call_id || parsed.id || '');
        if (!callId || !toolIndexByCallId.has(callId)) {
            if (callId) toolIndexByCallId.set(callId, normalized.length);
            normalized.push(message);
            continue;
        }

        const index = toolIndexByCallId.get(callId)!;
        const previous = normalized[index];
        const previousParsed = parseStoredToolPayload(previous.content);
        const previousStatus = normalizeToolStatus(previous.toolStatus || previousParsed.status);
        const incomingStatus = normalizeToolStatus(message.toolStatus || parsed.status);
        if (previousStatus === 'done' && incomingStatus !== 'done') continue;

        normalized[index] = {
            ...previous,
            ...message,
            id: previous.id || message.id,
            created_at: previous.created_at || message.created_at,
            toolArgs: Object.keys(parseToolArgs(message.toolArgs ?? parsed.args)).length > 0
                ? (message.toolArgs ?? parsed.args)
                : (previous.toolArgs ?? previousParsed.args),
        };
    }

    return normalized;
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
    return getChatToolRenderType(msg) === 'confirmation';
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

export function buildH5ConversationEntries(messages: H5ChatMessage[]): H5ConversationEntry[] {
    messages = normalizeChatTimelineMessages(messages);

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
        const renderType = getChatToolRenderType(msg);
        if (renderType) {
            flushGroup();
            grouped.push({
                type: 'special_render',
                renderType,
                msg,
                key: msg.id || `special-${i}`,
            });
            continue;
        }

        if (msg.role === 'tool_call') {
            if (!currentGroup) {
                currentGroup = [];
                groupStartIndex = i;
            }
            pushThinking(currentGroup, msg.toolThinking);
            currentGroup.push(toolItemFromMessage(msg));
            continue;
        }

        if (msg.role === 'assistant') {
            const contentText = msg.content?.trim() || '';
            const hasAttachments = Array.isArray(msg.attachments) && msg.attachments.length > 0;
            if (msg.thinking) {
                if (!currentGroup) {
                    currentGroup = [];
                    groupStartIndex = i;
                }
                pushThinking(currentGroup, msg.thinking);
            }
            if (!contentText && !hasAttachments) continue;
            flushGroup();
            grouped.push({
                type: 'message',
                msg: msg.thinking ? { ...msg, thinking: undefined } : msg,
                key: msg.id || `msg-${i}`,
            });
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
            : `${last.key}:${messageAnchor(last.msg)}`;
    return `${entries.length}:${isWaiting ? 'waiting' : 'idle'}:${lastAnchor}`;
}
