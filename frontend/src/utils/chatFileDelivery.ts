export type ChatFileDelivery = {
    id: string;
    path?: string;
    url?: string;
    sourceMode?: 'workspace' | 'managed_url' | 'external_url';
    filename: string;
    message?: string;
    mimeType?: string;
    size?: number;
    toolCallId?: string;
    mediaKind?: 'audio' | 'video';
    messageId?: string;
    allowDownload?: boolean;
};

export type ChatMediaDeliveryError = {
    deliveryError: true;
    status: 'failed' | 'unsupported' | 'unknown';
    code: string;
    message: string;
    retryable: boolean;
    mediaKind?: 'audio' | 'video';
};

const FILE_DELIVERY_TOOLS = new Set(['send_channel_file', 'send_media', 'send_audio', 'send_video']);
const FILE_DELIVERY_TYPE = 'platform_file_delivery';
const MEDIA_DELIVERY_TYPE = 'platform_media_delivery';

function toRecord(value: any): Record<string, any> {
    if (!value) return {};
    if (typeof value === 'object' && !Array.isArray(value)) return value;
    if (typeof value !== 'string') return {};
    try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch {
        return {};
    }
}

function firstString(...values: any[]) {
    for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
}

function normalizePlatformFilePath(rawPath: any): string | null {
    if (typeof rawPath !== 'string') return null;
    const value = rawPath.trim().replace(/\\/g, '/');
    if (!value) return null;
    if (value.startsWith('/') || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value)) return null;
    const parts = value.split('/').filter((part) => part && part !== '.');
    if (parts.length === 0 || parts.some((part) => part === '..')) return null;
    return parts.join('/');
}

function basename(path: string) {
    return path.split('/').filter(Boolean).pop() || 'download';
}

function normalizeExternalMediaUrl(rawUrl: any): string | null {
    if (typeof rawUrl !== 'string' || !rawUrl.trim()) return null;
    try {
        const url = new URL(rawUrl.trim());
        if (url.protocol !== 'https:' || url.username || url.password) return null;
        return url.toString();
    } catch {
        return null;
    }
}

function normalizeFilename(rawFilename: any, path: string) {
    const value = typeof rawFilename === 'string' && rawFilename.trim()
        ? rawFilename.trim().replace(/\\/g, '/')
        : basename(path);
    return basename(value);
}

function normalizeSize(value: any) {
    const size = Number(value);
    return Number.isFinite(size) && size >= 0 ? size : undefined;
}

function buildDelivery(payload: Record<string, any>, toolArgs: any, toolCallId?: string): ChatFileDelivery | null {
    const args = toRecord(toolArgs);
    const path = normalizePlatformFilePath(firstString(payload.path, payload.file_path, args.file_path));
    const sourceMode = firstString(payload.source_mode, payload.sourceMode);
    const url = sourceMode === 'external_url'
        ? normalizeExternalMediaUrl(firstString(payload.url, args.url))
        : null;
    if (!path && !url) return null;
    const sourceIdentity = path || url || '';
    const filename = normalizeFilename(firstString(payload.filename, args.filename), sourceIdentity);
    const id = toolCallId || `${sourceIdentity}:${filename}`;
    const message = firstString(payload.message);
    const mimeType = firstString(payload.mime_type, payload.mimeType);
    const size = normalizeSize(payload.size);
    const rawMediaKind = firstString(payload.media_kind, payload.mediaKind);
    const mediaKind = rawMediaKind === 'audio' || rawMediaKind === 'video' ? rawMediaKind : undefined;
    const messageId = firstString(
        payload.message_id,
        payload.messageId,
        payload.receipt_message_id,
        payload.receiptMessageId,
    );
    const allowDownload = payload.allow_download === true || payload.allowDownload === true;
    return {
        id,
        ...(path ? { path } : {}),
        ...(url ? { url } : {}),
        ...(sourceMode === 'external_url' || sourceMode === 'managed_url' || sourceMode === 'workspace'
            ? { sourceMode }
            : {}),
        filename,
        ...(message ? { message } : {}),
        ...(mimeType ? { mimeType } : {}),
        ...(size !== undefined ? { size } : {}),
        ...(toolCallId ? { toolCallId } : {}),
        ...(mediaKind ? { mediaKind } : {}),
        ...(messageId ? { messageId } : {}),
        ...(mediaKind ? { allowDownload } : {}),
    };
}

function parseStructuredResult(toolResult: any) {
    if (toolResult && typeof toolResult === 'object' && !Array.isArray(toolResult)) {
        return toolResult;
    }
    if (typeof toolResult !== 'string') return null;
    const text = toolResult.trim();
    if (!text.startsWith('{')) return null;
    try {
        const parsed = JSON.parse(text);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

export function parseMediaDeliveryErrorResult(
    toolName: string | undefined,
    toolResult: any,
): ChatMediaDeliveryError | null {
    if (!['send_media', 'send_audio', 'send_video'].includes((toolName || '').toLowerCase())) return null;
    const payload = parseStructuredResult(toolResult);
    if (!payload || !['media_delivery_result', MEDIA_DELIVERY_TYPE].includes(payload.type)) return null;
    const rawStatus = firstString(payload.status).toLowerCase();
    if (!['failed', 'unsupported', 'unknown'].includes(rawStatus)) return null;
    const rawMediaKind = firstString(payload.media_kind, payload.mediaKind);
    const mediaKind = rawMediaKind === 'audio' || rawMediaKind === 'video' ? rawMediaKind : undefined;
    return {
        deliveryError: true,
        status: rawStatus as ChatMediaDeliveryError['status'],
        code: firstString(payload.code) || 'MEDIA_DELIVERY_FAILED',
        message: firstString(payload.message) || '媒体发送未完成',
        retryable: payload.retryable === true,
        ...(mediaKind ? { mediaKind } : {}),
    };
}

function parseLegacyMarkdownResult(toolResult: any): Record<string, any> | null {
    if (typeof toolResult !== 'string') return null;
    const match = /File ready:\s*\[([^\]]+)]\(([^)]+)\)/i.exec(toolResult);
    if (!match) return null;
    const filename = match[1].trim();
    const rawUrl = match[2].trim();
    let path = '';
    try {
        const url = new URL(rawUrl, 'https://local.invalid');
        path = url.searchParams.get('path') || '';
    } catch {
        return null;
    }
    const message = toolResult.slice(0, match.index).trim();
    return {
        type: FILE_DELIVERY_TYPE,
        path,
        filename,
        message,
    };
}

export function parseFileDeliveryToolResult(
    toolName: string | undefined,
    toolResult: any,
    toolArgs: any = {},
    toolCallId?: string,
): ChatFileDelivery | null {
    if (!FILE_DELIVERY_TOOLS.has((toolName || '').toLowerCase())) return null;
    const structured = parseStructuredResult(toolResult);
    if (structured) {
        if (![FILE_DELIVERY_TYPE, MEDIA_DELIVERY_TYPE].includes(structured.type)) return null;
        if (
            structured.type === MEDIA_DELIVERY_TYPE
            && !['sent', 'already_sent'].includes(firstString(structured.status).toLowerCase())
        ) return null;
        return buildDelivery(structured, toolArgs, toolCallId);
    }
    const legacy = parseLegacyMarkdownResult(toolResult);
    return legacy ? buildDelivery(legacy, toolArgs, toolCallId) : null;
}
