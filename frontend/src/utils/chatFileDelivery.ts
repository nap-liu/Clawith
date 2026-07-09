export type ChatFileDelivery = {
    id: string;
    path: string;
    filename: string;
    message?: string;
    mimeType?: string;
    size?: number;
    toolCallId?: string;
};

const FILE_DELIVERY_TOOL = 'send_channel_file';
const FILE_DELIVERY_TYPE = 'platform_file_delivery';

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
    if (!path) return null;
    const filename = normalizeFilename(firstString(payload.filename, args.filename), path);
    const id = toolCallId || `${path}:${filename}`;
    const message = firstString(payload.message);
    const mimeType = firstString(payload.mime_type, payload.mimeType);
    const size = normalizeSize(payload.size);
    return {
        id,
        path,
        filename,
        ...(message ? { message } : {}),
        ...(mimeType ? { mimeType } : {}),
        ...(size !== undefined ? { size } : {}),
        ...(toolCallId ? { toolCallId } : {}),
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
    if ((toolName || '').toLowerCase() !== FILE_DELIVERY_TOOL) return null;
    const structured = parseStructuredResult(toolResult);
    if (structured) {
        if (structured.type !== FILE_DELIVERY_TYPE) return null;
        return buildDelivery(structured, toolArgs, toolCallId);
    }
    const legacy = parseLegacyMarkdownResult(toolResult);
    return legacy ? buildDelivery(legacy, toolArgs, toolCallId) : null;
}
