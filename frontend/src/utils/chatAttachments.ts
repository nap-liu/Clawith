export type ChatAttachedFile = {
    name: string;
    text: string;
    path?: string;
    imageUrl?: string;
    source?: 'upload' | 'workspace_auto';
};

export type ChatPreviewImage = {
    src: string;
    alt?: string;
    filename?: string;
    downloadUrl?: string;
};

export type ChatAttachmentPayload = {
    contentForLLM: string;
    userMsg: string;
    fileName: string;
    imageUrl?: string;
    previewImages: ChatPreviewImage[];
};

export type ChatModelOption = {
    id?: string | null;
    enabled?: boolean;
    supports_vision?: boolean;
};

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);
const IMAGE_DATA_MARKER_RE = /\[image_data:(data:image\/[^;]+;base64,[A-Za-z0-9+/=]+)\]/g;

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

export function buildPreviewImage(src: string, filename?: string, alt?: string): ChatPreviewImage {
    return {
        src,
        alt: alt || filename || 'image',
        filename: filename || alt || 'image',
        downloadUrl: src,
    };
}

export function buildPreviewImagesFromAttachments(attachments: ChatAttachedFile[]) {
    return attachments
        .filter((file) => !!file.imageUrl)
        .map((file) => buildPreviewImage(file.imageUrl || '', file.name));
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

export function modelSupportsVision(models: ChatModelOption[], modelId?: string | null) {
    if (!modelId) return false;
    return models.some((model) => model.id === modelId && model.supports_vision === true);
}

export function buildChatAttachmentPayload({
    input,
    attachments,
    supportsVision,
}: {
    input: string;
    attachments: ChatAttachedFile[];
    supportsVision: boolean;
}): ChatAttachmentPayload {
    let userMsg = input.trim();
    let contentForLLM = userMsg;
    let displayFiles = '';

    if (attachments.length > 0) {
        let filesPrompt = '';
        let filesDisplay = '';

        attachments.forEach((file) => {
            filesDisplay += `[Attachment: ${file.name}] `;
            if (file.imageUrl && supportsVision) {
                filesPrompt += `[image_data:${file.imageUrl}]\n`;
            } else if (file.imageUrl) {
                filesPrompt += `[图片文件已上传: ${file.name}，保存在 ${file.path || ''}]\n`;
            } else {
                const wsPath = file.path || '';
                const codePath = wsPath.replace(/^workspace\//, '');
                const fileLoc = wsPath
                    ? `\nFile location: ${wsPath} (for read_file/read_document tools)\nIn execute_code, use relative path: "${codePath}" (working directory is workspace/)\n`
                    : '';
                if (file.source === 'workspace_auto') {
                    filesPrompt += `[Workspace reference: ${file.name}]${fileLoc}\nUse read_file or read_document if you need the file contents.\n\n`;
                } else {
                    filesPrompt += `[File: ${file.name}]${fileLoc}\n${file.text}\n\n`;
                }
            }
        });

        if (supportsVision && attachments.some((file) => file.imageUrl)) {
            contentForLLM = userMsg ? `${filesPrompt}\n${userMsg}` : `${filesPrompt}\n请分析这些文件`;
        } else {
            contentForLLM = userMsg ? `${filesPrompt}\nQuestion: ${userMsg}` : `Please analyze these files:\n\n${filesPrompt}`;
        }

        displayFiles = filesDisplay.trim();
        userMsg = userMsg ? `${displayFiles}\n${userMsg}` : displayFiles;
    }

    return {
        contentForLLM,
        userMsg,
        fileName: attachments.map((file) => file.name).join(', '),
        imageUrl: attachments.length === 1 ? attachments[0].imageUrl : undefined,
        previewImages: buildPreviewImagesFromAttachments(attachments),
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
