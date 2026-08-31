import { fileApi } from '../../../services/api';
import {
    extractChatImageDataMarkers,
    normalizeChatAttachmentFields,
} from '../../../utils/chatAttachments';

export function parseAgentDetailChatMsg({
    msg,
    id,
    activeSession,
}: {
    msg: any;
    id: string | undefined;
    activeSession: any;
}) {
    const hasStructuredAttachments = Object.prototype.hasOwnProperty.call(msg, 'attachments');
    if (msg.role !== 'user' && !(msg.role === 'assistant' && hasStructuredAttachments)) return msg;
    if (!id) return msg;
    const normalized = normalizeChatAttachmentFields({
        raw: msg as Record<string, any>,
        sourceChannel: activeSession?.source_channel,
        buildDownloadUrl: (path, inline) => fileApi.downloadUrl(id, path, { inline }),
    });
    const markerImages = normalized.previewImages.length === 0 ? extractChatImageDataMarkers(msg.content || '') : [];
    const images = normalized.previewImages.length > 0 ? normalized.previewImages : markerImages;
    return {
        ...msg,
        content: normalized.displayContent,
        attachments: normalized.attachments,
        fileName: normalized.fileName || msg.fileName,
        previewImages: msg.previewImages || images,
        imageUrl: msg.imageUrl || (images.length === 1 ? images[0].src : undefined),
    };
}
