import {
    normalizeChatQuotedMessage,
    type ChatMessageAttachment,
    type ChatPreviewImage,
    type ChatQuotedMessage,
} from '../../../utils/chatAttachments';
import { createClientId } from '../../../utils/clientId';
import { getChatToolRenderIdentity, getChatToolRenderType } from '../../../components/ChatToolCallRenderer';

export type ConversationToolStatus = 'running' | 'done';

export type ConversationMessage = {
    id: string;
    role: 'user' | 'assistant' | 'system' | 'tool_call';
    content: string;
    thinking?: string;
    streaming?: boolean;
    created_at?: string | null;
    display_content?: string;
    attachments?: ChatMessageAttachment[];
    quoted_message?: ChatQuotedMessage;
    toolCallId?: string;
    toolName?: string;
    toolArgs?: any;
    toolStatus?: ConversationToolStatus;
    toolResult?: string;
    toolThinking?: string;
    fileName?: string;
    imageUrl?: string;
    previewImages?: ChatPreviewImage[];
    timestamp?: string;
    sender_name?: string;
    sender_user_id?: string;
    sender_agent_id?: string;
    confirmationToolCalls?: ConversationMessage[];
    _streaming?: boolean;
};

export type ConversationAnalysisItem =
    | { type: 'thinking'; content: string }
    | { type: 'tool'; name: string; args: any; status: ConversationToolStatus; result?: string };

export type ConversationEntry =
    | { type: 'analysis_group'; items: ConversationAnalysisItem[]; key: string; running: boolean }
    | { type: 'special_render'; renderType: string; msg: ConversationMessage; key: string }
    | { type: 'message'; msg: ConversationMessage; key: string };

export type AssistantStreamMessage = {
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

function normalizeToolStatus(status: any): ConversationToolStatus {
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

export function mapHistoryMessage(raw: any, makeId: () => string = defaultMakeId): ConversationMessage | null {
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
            timestamp: raw.timestamp || raw.created_at || undefined,
            sender_name: raw.sender_name || undefined,
            sender_user_id: raw.sender_user_id || undefined,
            sender_agent_id: raw.sender_agent_id || undefined,
        };
    }

    const quotedMessage = normalizeChatQuotedMessage(raw.quoted_message);
    return {
        id: String(raw.id || makeId()),
        role: raw.role,
        content: raw.content || '',
        thinking: raw.thinking || undefined,
        created_at: raw.created_at || null,
        timestamp: raw.timestamp || raw.created_at || undefined,
        sender_name: raw.sender_name || undefined,
        sender_user_id: raw.sender_user_id || undefined,
        sender_agent_id: raw.sender_agent_id || undefined,
        ...(Object.prototype.hasOwnProperty.call(raw, 'display_content') ? { display_content: raw.display_content || '' } : {}),
        ...(Object.prototype.hasOwnProperty.call(raw, 'attachments') ? { attachments: raw.attachments || [] } : {}),
        ...(quotedMessage ? { quoted_message: quotedMessage } : {}),
    };
}

export function hasPendingConfirmation(messages: ConversationMessage[]): boolean {
    return messages.some((message) => (
        message.role === 'tool_call'
        && message.toolName === CONFIRMATION_TOOL
        && message.toolStatus === 'running'
        && parseToolArgs(message.toolArgs).force_confirmation !== false
    ));
}

export function findStreamingAssistantIndex(messages: ConversationMessage[]) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const msg = messages[i];
        if (msg.role === 'assistant' && (msg.streaming || (msg as any)._streaming)) return i;
    }
    return -1;
}

function findStreamingAssistantIndexAfterLastTool(messages: ConversationMessage[]) {
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
    let identifiedIdx = event.messageId
        ? messages.findIndex((message) => String(message.id || '') === event.messageId)
        : -1;
    const content = event.content || '';
    const now = event.now || new Date().toISOString();
    let lastUserIdx = -1;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        if (messages[i].role === 'user') {
            lastUserIdx = i;
            break;
        }
    }
    const identifiedMessage = identifiedIdx >= 0 ? messages[identifiedIdx] : undefined;
    const isCurrentTurnStream = (message: T, index: number) => (
        index > lastUserIdx
        && message.role === 'assistant'
        && Boolean(message.streaming || message._streaming)
    );
    const streamed = messages.filter(isCurrentTurnStream);
    const streamedThinking = streamed
        .map((message) => message.thinking || '')
        .filter(Boolean)
        .join('\n\n');
    const streamedContent = streamed
        .map((message) => message.content || '')
        .filter((segment) => segment.trim())
        .join('\n\n');

    // Explicit ids address one committed message (currently onboarding and
    // server-committed rows). Preserve its full shape and position, but still
    // collapse every transient assistant row from the same turn. A committed
    // event can arrive before `done`; returning after updating only that row
    // would strand earlier streamed fragments until a full history reload.
    if (
        event.messageId
        && identifiedMessage
        && !identifiedMessage.streaming
        && !identifiedMessage._streaming
    ) {
        const next = messages.filter((message, index) => !isCurrentTurnStream(message, index));
        const nextIdentifiedIdx = next.findIndex((message) => (
            message === identifiedMessage
            || (
                identifiedMessage.id
                && String(message.id || '') === String(identifiedMessage.id)
            )
        ));
        if (nextIdentifiedIdx < 0) return next;
        next[nextIdentifiedIdx] = {
            ...identifiedMessage,
            content: content || identifiedMessage.content || '',
            ...(
                identifiedMessage.thinking || streamedThinking
                    ? { thinking: identifiedMessage.thinking || streamedThinking }
                    : {}
            ),
            streaming: false,
            _streaming: false,
            _canonicalDone: true,
            created_at: identifiedMessage.created_at || now,
            timestamp: identifiedMessage.timestamp || now,
        };
        return next;
    }

    // done.content is the canonical reply for the whole logical turn. Remove
    // all temporary stream bubbles so A + tool + B becomes one durable A+B
    // reply, while ordinary non-stream assistant rows (for example a media
    // caption) remain independent.
    const next = messages.filter((message, index) => (
        !isCurrentTurnStream(message, index)
        && !(identifiedIdx === index && message.role === 'assistant')
    ));
    const canonicalContent = content || streamedContent;
    if (!canonicalContent && !streamedThinking) return next;

    const canonical = {
        ...(identifiedMessage || {}),
        id: event.messageId || streamed[0]?.id || makeId(),
        role: 'assistant',
        content: canonicalContent,
        ...(
            streamedThinking || identifiedMessage?.thinking
                ? { thinking: streamedThinking || identifiedMessage?.thinking }
                : {}
        ),
        created_at: identifiedMessage?.created_at || streamed[0]?.created_at || now,
        timestamp: identifiedMessage?.timestamp || streamed[0]?.timestamp || now,
        streaming: false,
        _streaming: false,
        _canonicalDone: true,
    } as unknown as T;

    // A blank done denotes suspension. Put the normalized streamed intro just
    // before the pending confirmation card, matching its durable row order.
    if (!content) {
        let nextLastUserIdx = -1;
        for (let i = next.length - 1; i >= 0; i -= 1) {
            if (next[i].role === 'user') {
                nextLastUserIdx = i;
                break;
            }
        }
        const confirmationIdx = next.findIndex((message, index) => (
            index > nextLastUserIdx
            && message.role === 'tool_call'
            && message.toolName === CONFIRMATION_TOOL
            && normalizeToolStatus(message.toolStatus) === 'running'
        ));
        if (confirmationIdx >= 0) {
            return [
                ...next.slice(0, confirmationIdx),
                canonical,
                ...next.slice(confirmationIdx),
            ];
        }
    }

    const last = next[next.length - 1];
    if (
        !event.messageId
        && last?.role === 'assistant'
        && !last.streaming
        && !last._streaming
        && last._canonicalDone
        && last.content === content
    ) {
        return next;
    }
    return [...next, canonical];
}

export function applyAssistantStreamMessage(
    messages: ConversationMessage[],
    event: AssistantStreamMessage,
    makeId: () => string = defaultMakeId,
): ConversationMessage[] {
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
            next[idx] = {
                ...next[idx],
                thinking: (next[idx].thinking || '') + content,
                streaming: true,
                _streaming: true,
            };
            return next;
        }
        return [...messages, {
            id: event.messageId || makeId(),
            role: 'assistant',
            content: '',
            thinking: content,
            streaming: true,
            _streaming: true,
        }];
    }

    if (event.type === 'chunk') {
        if (idx >= 0) {
            const next = [...messages];
            next[idx] = {
                ...next[idx],
                content: next[idx].content + content,
                streaming: true,
                _streaming: true,
            };
            return next;
        }
        return [...messages, {
            id: event.messageId || makeId(),
            role: 'assistant',
            content,
            streaming: true,
            _streaming: true,
        }];
    }

    return applyAssistantDoneMessage(messages, event, makeId);
}

export function isSameMessage(a: ConversationMessage, b: ConversationMessage) {
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

export function mergeHistoryMessages(prev: ConversationMessage[], history: ConversationMessage[]) {
    if (history.length === 0) return prev;

    const mergeKeys = (message: ConversationMessage) => {
        if (message.role === 'tool_call') {
            const keys: string[] = [];
            if (message.toolCallId) keys.push(`tool-call:${message.toolCallId}`);
            const renderIdentity = getChatToolRenderIdentity(message);
            if (renderIdentity) keys.push(`tool-render:${renderIdentity}`);
            return keys;
        }
        const keys: string[] = [];
        if (message.id) keys.push(`message:${message.id}`);
        keys.push(JSON.stringify([
            message.role,
            message.content,
            message.thinking || '',
            message.toolCallId || '',
        ]));
        return keys;
    };
    const historyBuckets = new Map<string, number[]>();
    history.forEach((message, index) => {
        mergeKeys(message).forEach((key) => {
            const bucket = historyBuckets.get(key);
            if (bucket) bucket.push(index);
            else historyBuckets.set(key, [index]);
        });
    });
    const bucketOffsets = new Map<string, number>();
    const usedHistoryIndexes = new Set<number>();
    const assistantContents = history
        .filter((item) => item.role === 'assistant' && !!item.content)
        .map((item) => item.content);
    const localOnly: Array<{
        message: ConversationMessage;
        localIndex: number;
        previousHistoryIndex: number;
    }> = [];
    const matchedHistoryByLocalIndex = new Map<number, number>();
    let previousHistoryIndex = -1;

    for (let localIndex = 0; localIndex < prev.length; localIndex += 1) {
        const local = prev[localIndex];
        let matchedHistoryIndex = -1;
        for (const key of mergeKeys(local)) {
            const bucket = historyBuckets.get(key);
            let offset = bucketOffsets.get(key) || 0;
            while (bucket && offset < bucket.length && usedHistoryIndexes.has(bucket[offset])) offset += 1;
            bucketOffsets.set(key, offset);
            if (bucket && offset < bucket.length) {
                matchedHistoryIndex = bucket[offset];
                bucketOffsets.set(key, offset + 1);
                break;
            }
        }
        if (matchedHistoryIndex >= 0) {
            usedHistoryIndexes.add(matchedHistoryIndex);
            matchedHistoryByLocalIndex.set(localIndex, matchedHistoryIndex);
            previousHistoryIndex = matchedHistoryIndex;
            continue;
        }

        if (local.streaming && assistantContents.some((content) => (
            !local.content || content.includes(local.content)
        ))) {
            continue;
        }

        localOnly.push({ message: local, localIndex, previousHistoryIndex });
    }

    if (localOnly.length === 0) return history;

    const messageTime = (message: ConversationMessage) => {
        const value = message.created_at || message.timestamp;
        const parsed = value ? Date.parse(value) : Number.NaN;
        return Number.isFinite(parsed) ? parsed : null;
    };
    const buckets = new Map<number, ConversationMessage[]>();
    for (const local of localOnly) {
        let nextHistoryIndex = -1;
        for (let index = local.localIndex + 1; index < prev.length; index += 1) {
            const matched = matchedHistoryByLocalIndex.get(index);
            if (matched != null) {
                nextHistoryIndex = matched;
                break;
            }
        }

        const lowerBound = Math.max(0, local.previousHistoryIndex + 1);
        const upperBound = nextHistoryIndex >= 0 ? nextHistoryIndex : history.length;
        const localTime = messageTime(local.message);
        // A transient without a server timestamp was already visible before
        // this history request began. Durable rows that appear only in the new
        // snapshot were committed during the disconnect/recovery window, so
        // keep the transient immediately after its previous matched anchor.
        // Non-transient local rows retain the conservative upper-bound policy.
        let insertionIndex = localTime == null
            && (local.message.streaming || local.message._streaming)
            ? lowerBound
            : upperBound;
        if (localTime != null) {
            for (let index = lowerBound; index < upperBound; index += 1) {
                const historyTime = messageTime(history[index]);
                if (historyTime != null && historyTime > localTime) {
                    insertionIndex = index;
                    break;
                }
            }
        }
        const bucket = buckets.get(insertionIndex);
        if (bucket) bucket.push(local.message);
        else buckets.set(insertionIndex, [local.message]);
    }

    const merged: ConversationMessage[] = [];
    for (let index = 0; index <= history.length; index += 1) {
        const localBucket = buckets.get(index);
        if (localBucket) merged.push(...localBucket);
        if (index < history.length) merged.push(history[index]);
    }
    return merged;
}

function stableMessageKeys(message: ConversationMessage): string[] {
    const keys: string[] = [];
    if (message.id) keys.push(`message:${message.id}`);
    if (message.toolCallId) keys.push(`tool-call:${message.toolCallId}`);
    const renderIdentity = getChatToolRenderIdentity(message);
    if (renderIdentity) keys.push(`tool-render:${renderIdentity}`);
    return keys;
}

/**
 * Reconcile a freshly fetched latest-N window without discarding an already
 * loaded older prefix. This is used after reconnect/resume; unlike
 * mergeHistoryMessages(), the fetched history is not the complete timeline.
 */
function latestHistoryOverlapIndex(
    prev: ConversationMessage[],
    latestWindow: ConversationMessage[],
) {
    const latestStableKeys = new Set<string>();
    latestWindow.forEach((message) => {
        stableMessageKeys(message).forEach((key) => latestStableKeys.add(key));
    });
    let overlapIndex = prev.findIndex((message) => (
        stableMessageKeys(message).some((key) => latestStableKeys.has(key))
    ));

    // Optimistic/live rows can have a temporary id that differs from the
    // durable row. Limit content fallback to the recent tail so an old repeated
    // message cannot be mistaken for the latest-window overlap boundary.
    if (overlapIndex < 0) {
        const fallbackStart = Math.max(0, prev.length - latestWindow.length * 2);
        const fallbackOffset = prev.slice(fallbackStart).findIndex((message) => (
            latestWindow.some((candidate) => isSameMessage(message, candidate))
        ));
        if (fallbackOffset >= 0) overlapIndex = fallbackStart + fallbackOffset;
    }
    return overlapIndex;
}

export function latestHistoryWindowOverlaps(
    prev: ConversationMessage[],
    latestWindow: ConversationMessage[],
) {
    return prev.length === 0 || latestWindow.length === 0
        || latestHistoryOverlapIndex(prev, latestWindow) >= 0;
}

export function reconcileLatestHistoryWindow(
    prev: ConversationMessage[],
    latestWindow: ConversationMessage[],
) {
    if (prev.length === 0) return latestWindow;
    if (latestWindow.length === 0) return prev;

    const overlapIndex = latestHistoryOverlapIndex(prev, latestWindow);

    // No overlap means more than one window may have arrived while the page was
    // suspended. Reset to the latest complete window instead of creating a
    // permanent middle gap; callers reset the older-page cursor accordingly.
    if (overlapIndex < 0) return latestWindow;
    return [
        ...prev.slice(0, overlapIndex),
        ...mergeHistoryMessages(prev.slice(overlapIndex), latestWindow),
    ];
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

export function upsertToolCallMessage(messages: ConversationMessage[], toolMsg: ConversationMessage) {
    const incomingTarget = getToolTargetKey(toolMsg.toolArgs);
    const exactIdMatch = (msg: ConversationMessage) => (
        msg.role === 'tool_call'
        && !!toolMsg.toolCallId
        && msg.toolCallId === toolMsg.toolCallId
    );
    const sameTool = (msg: ConversationMessage) => (
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

export function toolCallMessageFromEvent(data: any, makeId: () => string = defaultMakeId, now = new Date().toISOString()): ConversationMessage {
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

export function isConfirmationToolCall(msg: ConversationMessage) {
    return getChatToolRenderType(msg) === 'confirmation';
}

function pushThinking(items: ConversationAnalysisItem[], content?: string) {
    const text = (content || '').trim();
    if (!text) return;
    const previous = items[items.length - 1];
    if (previous?.type === 'thinking' && previous.content === text) return;
    items.push({ type: 'thinking', content: text });
}

function toolItemFromMessage(msg: ConversationMessage): Extract<ConversationAnalysisItem, { type: 'tool' }> {
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

export function buildConversationEntries(messages: ConversationMessage[]): ConversationEntry[] {
    messages = normalizeChatTimelineMessages(messages);

    const grouped: ConversationEntry[] = [];
    let currentGroup: ConversationAnalysisItem[] | null = null;
    let groupStartIndex = 0;
    let groupStartKey = '';

    const flushGroup = () => {
        if (!currentGroup || currentGroup.length === 0) {
            currentGroup = null;
            return;
        }
        grouped.push({
            type: 'analysis_group',
            items: currentGroup,
            key: `analysis-${groupStartKey || groupStartIndex}`,
            running: currentGroup.some((item) => item.type === 'tool' && item.status === 'running'),
        });
        currentGroup = null;
        groupStartKey = '';
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
                groupStartKey = msg.id || msg.toolCallId || String(i);
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
                    groupStartKey = msg.id || msg.toolCallId || String(i);
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

function messageAnchor(msg: ConversationMessage) {
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

function analysisAnchor(items: ConversationAnalysisItem[]) {
    return items.map((item) => {
        if (item.type === 'thinking') return `thinking:${item.content.length}`;
        return `tool:${item.name}:${item.status}:${item.result?.length || 0}`;
    }).join('|');
}

export function getConversationScrollAnchor(entries: ConversationEntry[], isWaiting: boolean) {
    const last = entries[entries.length - 1];
    const lastAnchor = !last
        ? 'empty'
        : last.type === 'analysis_group'
            ? `${last.key}:${analysisAnchor(last.items)}`
            : `${last.key}:${messageAnchor(last.msg)}`;
    return `${entries.length}:${isWaiting ? 'waiting' : 'idle'}:${lastAnchor}`;
}
