export type ChatAttachedFile = {
    name: string;
    text: string;
    path?: string;
    imageUrl?: string;
    mimeType?: string;
    sizeBytes?: number;
    source?: 'upload' | 'workspace_auto';
};

export type ChatMessageAttachment = {
    display_name: string;
    path: string;
    kind: 'image' | 'file' | 'audio' | 'video';
    mime_type?: string;
    size_bytes?: number;
};

export type ChatAttachmentIconKind =
    | 'pdf'
    | 'word'
    | 'spreadsheet'
    | 'presentation'
    | 'archive'
    | 'text'
    | 'code'
    | 'audio'
    | 'video'
    | 'generic';

export type ChatPreviewImage = {
    src: string;
    alt?: string;
    filename?: string;
    downloadUrl?: string;
    path?: string;
};

export type ChatAttachmentPayload = {
    contentForLLM: string;
    userMsg: string;
    displayContent: string;
    fileName: string;
    imageUrl?: string;
    previewImages: ChatPreviewImage[];
    attachments: ChatMessageAttachment[];
};

export type NormalizedChatAttachmentFields = {
    displayContent: string;
    attachments: ChatMessageAttachment[];
    previewImages: ChatPreviewImage[];
    fileName: string;
    imageUrl?: string;
};

export type ChatModelOption = {
    id?: string | null;
    enabled?: boolean;
    supports_vision?: boolean;
};

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);
const IMAGE_DATA_MARKER_RE = /\[image_data:(data:image\/[^;]+;base64,[A-Za-z0-9+/=]+)\]/g;
const FILE_MARKER_RE = /\[file:([^\]\r\n]+)\]/gi;

const WORD_EXTENSIONS = new Set(['doc', 'docx', 'odt', 'rtf']);
const SPREADSHEET_EXTENSIONS = new Set(['xls', 'xlsx', 'csv', 'ods']);
const PRESENTATION_EXTENSIONS = new Set(['ppt', 'pptx', 'odp', 'key']);
const ARCHIVE_EXTENSIONS = new Set(['zip', 'rar', '7z', 'tar', 'gz', 'gzip', 'bz2', 'xz']);
const TEXT_EXTENSIONS = new Set(['txt', 'md', 'markdown', 'log']);
const CODE_EXTENSIONS = new Set([
    'c', 'cc', 'cpp', 'css', 'go', 'h', 'hpp', 'html', 'java', 'js', 'jsx',
    'json', 'php', 'py', 'rs', 'sh', 'sql', 'ts', 'tsx', 'vue', 'xml', 'yaml', 'yml',
]);

export function getChatAttachmentIconKind({
    name,
    mimeType,
    kind,
}: {
    name?: string | null;
    mimeType?: string | null;
    kind?: ChatMessageAttachment['kind'] | null;
}): ChatAttachmentIconKind {
    const mime = (mimeType || '').trim().toLowerCase();
    if (kind === 'audio' || mime.startsWith('audio/')) return 'audio';
    if (kind === 'video' || mime.startsWith('video/')) return 'video';

    const extension = (name || '').split('.').pop()?.toLowerCase() || '';
    if (extension === 'pdf' || mime === 'application/pdf') return 'pdf';
    if (WORD_EXTENSIONS.has(extension) || mime.includes('wordprocessingml') || mime.includes('msword')) return 'word';
    if (SPREADSHEET_EXTENSIONS.has(extension) || mime.includes('spreadsheetml') || mime.includes('ms-excel')) return 'spreadsheet';
    if (PRESENTATION_EXTENSIONS.has(extension) || mime.includes('presentationml') || mime.includes('ms-powerpoint')) return 'presentation';
    if (ARCHIVE_EXTENSIONS.has(extension) || mime.includes('zip') || mime.includes('compressed') || mime.includes('archive')) return 'archive';
    if (CODE_EXTENSIONS.has(extension) || mime.includes('json') || mime.includes('xml') || mime.includes('yaml')) return 'code';
    if (TEXT_EXTENSIONS.has(extension) || mime.startsWith('text/')) return 'text';
    return 'generic';
}

export function isPreviewableImageName(name?: string | null) {
    const ext = (name || '').split('.').pop()?.toLowerCase() || '';
    return IMAGE_EXTENSIONS.has(ext);
}

export function splitAttachmentFileNames(fileName?: string | null) {
    return (fileName || '')
        .split(',')
        .map((name) => name.trim())
        .filter(Boolean);
}

export function buildPreviewImage(src: string, filename?: string, alt?: string, path?: string): ChatPreviewImage {
    return {
        src,
        alt: alt || filename || 'image',
        filename: filename || alt || 'image',
        downloadUrl: src,
        ...(path ? { path } : {}),
    };
}

export function buildPreviewImagesFromAttachments(attachments: ChatAttachedFile[]) {
    return attachments
        .filter((file) => !!file.imageUrl)
        .map((file) => buildPreviewImage(file.imageUrl || '', file.name));
}

function inferMessageAttachmentKind(file: Pick<ChatAttachedFile, 'name' | 'imageUrl' | 'mimeType'>): ChatMessageAttachment['kind'] {
    const mime = (file.mimeType || '').toLowerCase();
    if (file.imageUrl || mime.startsWith('image/') || isPreviewableImageName(file.name)) return 'image';
    if (mime.startsWith('audio/')) return 'audio';
    if (mime.startsWith('video/')) return 'video';
    return 'file';
}

export function buildMessageAttachments(attachments: ChatAttachedFile[]): ChatMessageAttachment[] {
    return attachments
        .filter((file): file is ChatAttachedFile & { path: string } => !!file.path)
        .map((file) => ({
            display_name: file.name,
            path: file.path,
            kind: inferMessageAttachmentKind(file),
            ...(file.mimeType ? { mime_type: file.mimeType } : {}),
            ...(typeof file.sizeBytes === 'number' ? { size_bytes: file.sizeBytes } : {}),
        }));
}

function normalizeApiAttachments(raw: unknown): ChatMessageAttachment[] {
    if (!Array.isArray(raw)) return [];
    return raw.reduce<ChatMessageAttachment[]>((attachments, item) => {
        if (!item || typeof item !== 'object') return attachments;
        const value = item as Record<string, unknown>;
        const displayName = String(value.display_name || '').trim();
        const path = String(value.path || '').trim();
        const kind = String(value.kind || '').trim();
        if (!displayName || !path || !['image', 'file', 'audio', 'video'].includes(kind)) return attachments;
        attachments.push({
            display_name: displayName,
            path,
            kind: kind as ChatMessageAttachment['kind'],
            ...(value.mime_type ? { mime_type: String(value.mime_type) } : {}),
            ...(typeof value.size_bytes === 'number' ? { size_bytes: value.size_bytes } : {}),
        });
        return attachments;
    }, []);
}

function parseLegacyAttachmentFields(content: string, sourceChannel?: string): { displayContent: string; attachments: ChatMessageAttachment[] } {
    const raw = content || '';
    const names: string[] = [];
    let cursor = 0;
    while (true) {
        const match = raw.slice(cursor).match(/^\s*\[file:([^\]\r\n]+)\]/i);
        if (!match) break;
        names.push(match[1]);
        cursor += match[0].length;
    }
    if (names.length > 0) {
        const expanded = ['web', 'miniprogram', 'wechat_miniprogram'].includes(sourceChannel || '') && names.length === 1
            ? names[0].split(',').map((name) => name.trim()).filter(Boolean)
            : names;
        const attachments = expanded.map((name) => ({
            display_name: name.trim().split('/').pop() || name.trim(),
            path: `workspace/uploads/${name.trim().split('/').pop() || name.trim()}`,
            kind: isPreviewableImageName(name) ? 'image' as const : 'file' as const,
        }));
        const displayContent = stripChatImageDataMarkers(raw.slice(cursor))
            .replace(/^(?:\[Attachment: [^\]]+\]\s*)+/, '')
            .trim();
        return { displayContent, attachments };
    }

    if (sourceChannel === 'slack') {
        const suffix = raw.match(/((?:\s+\[file:[^\]\r\n]+\])+\s*)$/i);
        if (suffix) {
            FILE_MARKER_RE.lastIndex = 0;
            const attachments: ChatMessageAttachment[] = [];
            let match: RegExpExecArray | null;
            while ((match = FILE_MARKER_RE.exec(suffix[1])) !== null) {
                const name = match[1].trim().split('/').pop() || match[1].trim();
                attachments.push({
                    display_name: name,
                    path: `workspace/uploads/${name}`,
                    kind: isPreviewableImageName(name) ? 'image' : 'file',
                });
            }
            return { displayContent: raw.slice(0, suffix.index).trim(), attachments };
        }
    }
    return { displayContent: stripChatImageDataMarkers(raw), attachments: [] };
}

export function normalizeChatAttachmentFields({
    raw,
    sourceChannel,
    buildDownloadUrl,
}: {
    raw: Record<string, any>;
    sourceChannel?: string;
    buildDownloadUrl: (path: string, inline?: boolean) => string;
}): NormalizedChatAttachmentFields {
    const hasCanonicalDto = Object.prototype.hasOwnProperty.call(raw, 'attachments')
        && Object.prototype.hasOwnProperty.call(raw, 'display_content');
    const normalized = hasCanonicalDto
        ? {
            displayContent: String(raw.display_content || ''),
            attachments: normalizeApiAttachments(raw.attachments),
        }
        : parseLegacyAttachmentFields(String(raw.content || ''), sourceChannel);
    const previewImages = normalized.attachments
        .filter((attachment) => attachment.kind === 'image')
        .map((attachment) => buildPreviewImage(
            buildDownloadUrl(attachment.path, true),
            attachment.display_name,
            undefined,
            attachment.path,
        ));
    return {
        ...normalized,
        previewImages,
        fileName: normalized.attachments.map((attachment) => attachment.display_name).join(', '),
        imageUrl: previewImages.length === 1 ? previewImages[0].src : undefined,
    };
}

export async function downloadChatAttachment(url: string, filename: string) {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.rel = 'noreferrer';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
}

export function extractChatImageDataMarkers(content: string): ChatPreviewImage[] {
    const images: ChatPreviewImage[] = [];
    let match: RegExpExecArray | null;
    IMAGE_DATA_MARKER_RE.lastIndex = 0;
    while ((match = IMAGE_DATA_MARKER_RE.exec(content || '')) !== null) {
        images.push(buildPreviewImage(match[1], `image-${images.length + 1}.png`, 'attached image'));
    }
    return images;
}

export function stripChatImageDataMarkers(content: string) {
    IMAGE_DATA_MARKER_RE.lastIndex = 0;
    return (content || '').replace(IMAGE_DATA_MARKER_RE, '').trim();
}

export function resolveEffectiveChatModelId({
    preferredModelId,
    tenantDefaultModelId,
    models,
}: {
    preferredModelId?: string | null;
    tenantDefaultModelId?: string | null;
    models: ChatModelOption[];
}) {
    const preferred = preferredModelId || tenantDefaultModelId;
    if (preferred) return preferred;
    const firstEnabled = models.find((model) => model.enabled !== false && model.id);
    return firstEnabled?.id || null;
}

export function buildChatAttachmentPayload({
    input,
    attachments,
}: {
    input: string;
    attachments: ChatAttachedFile[];
}): ChatAttachmentPayload {
    let userMsg = input.trim();
    let contentForLLM = userMsg;
    let displayFiles = '';

    if (attachments.length > 0) {
        let filesPrompt = '';
        let filesDisplay = '';

        attachments.forEach((file) => {
            filesDisplay += `[Attachment: ${file.name}] `;
            if (!file.imageUrl) {
                if (file.source === 'workspace_auto') {
                    filesPrompt += `[Workspace reference: ${file.name}]\nUse read_file or read_document if you need the file contents.\n\n`;
                } else {
                    filesPrompt += `[File: ${file.name}]\n${file.text}\n\n`;
                }
            }
        });

        contentForLLM = userMsg
            ? `${filesPrompt}${filesPrompt ? '\n' : ''}${userMsg}`
            : `${filesPrompt}${filesPrompt ? '\n' : ''}请分析这些文件`;

        displayFiles = filesDisplay.trim();
        userMsg = userMsg ? `${displayFiles}\n${userMsg}` : displayFiles;
    }

    return {
        contentForLLM,
        userMsg,
        displayContent: input.trim(),
        fileName: attachments.map((file) => file.name).join(', '),
        imageUrl: attachments.length === 1 ? attachments[0].imageUrl : undefined,
        previewImages: buildPreviewImagesFromAttachments(attachments),
        attachments: buildMessageAttachments(attachments),
    };
}

export function collectMarkdownImages(content: string): ChatPreviewImage[] {
    const images: ChatPreviewImage[] = [];
    const re = /!\[([^\]]*)\]\(([^)]+)\)/g;
    let match: RegExpExecArray | null;
    while ((match = re.exec(content)) !== null) {
        const alt = match[1] || '';
        const src = match[2]?.trim().replace(/^<|>$/g, '') || '';
        if (!src) continue;
        images.push({
            src,
            alt,
            filename: alt || src.split('/').pop()?.split('?')[0] || 'image',
            downloadUrl: src,
        });
    }
    return images;
}
