import { IconDownload, IconPhoto } from '@tabler/icons-react';
import { fileApi } from '../services/api';
import { isPreviewableImageName, type ChatPreviewImage } from '../utils/chatAttachments';
import type { ChatFileDelivery } from '../utils/chatFileDelivery';
import { formatFileSize } from '../utils/formatFileSize';
import ChatAttachmentIcon from './ChatAttachmentIcon';
import ChatMediaCard from './ChatMediaCard';
import './ChatFileDeliveryCard.css';

type Props = {
    agentId: string;
    messageId: string;
    delivery: ChatFileDelivery;
    mode?: 'pc' | 'h5';
    onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
};

function isImageDelivery(delivery: ChatFileDelivery) {
    return (delivery.mimeType || '').toLowerCase().startsWith('image/')
        || isPreviewableImageName(delivery.filename);
}

export default function ChatFileDeliveryCard({
    agentId,
    messageId,
    delivery,
    mode = 'pc',
    onPreviewImages,
}: Props) {
    const downloadUrl = delivery.url
        || (agentId && delivery.path ? fileApi.downloadUrl(agentId, delivery.path) : '');
    const previewUrl = agentId && delivery.path
        ? fileApi.downloadUrl(agentId, delivery.path, { inline: true })
        : '';
    const isImage = isImageDelivery(delivery);
    const protectImage = mode === 'h5' && isImage;
    const mediaKind = delivery.mediaKind
        || ((delivery.mimeType || '').startsWith('audio/') ? 'audio' : undefined)
        || ((delivery.mimeType || '').startsWith('video/') ? 'video' : undefined);
    const mediaDisplayTitle = delivery.title || delivery.filename;
    const details = [
        delivery.size !== undefined ? formatFileSize(delivery.size) : '',
        delivery.mimeType || '',
    ].filter(Boolean).join(' · ');

    const openPreview = () => {
        if (!isImage || !previewUrl || !onPreviewImages) return;
        onPreviewImages([{
            src: previewUrl,
            downloadUrl: downloadUrl || previewUrl,
            filename: delivery.filename,
            alt: delivery.filename,
        }], 0);
    };

    if (mediaKind) {
        return (
            <div className={`chat-file-delivery chat-file-delivery--media chat-file-delivery--${mode}`}>
                {delivery.message ? <div className="chat-file-delivery__message">{delivery.message}</div> : null}
                <ChatMediaCard
                    agentId={agentId}
                    messageId={messageId}
                    mode={mode}
                    attachment={{
                        display_name: mediaDisplayTitle,
                        path: delivery.path || '',
                        kind: mediaKind,
                        ...(delivery.mimeType ? { mime_type: delivery.mimeType } : {}),
                        ...(delivery.size !== undefined ? { size_bytes: delivery.size } : {}),
                    }}
                    externalUrl={delivery.url}
                    onDownload={delivery.allowDownload && downloadUrl
                        ? () => { window.location.assign(downloadUrl); }
                        : undefined}
                />
            </div>
        );
    }

    return (
        <div className={`chat-file-delivery chat-file-delivery--${mode}`}>
            {delivery.message ? (
                <div className="chat-file-delivery__message">{delivery.message}</div>
            ) : null}
            <div className="chat-file-delivery__body">
                {isImage && previewUrl ? (
                    <button
                        type="button"
                        className="chat-file-delivery__thumb chat-file-delivery__thumb--image"
                        onClick={openPreview}
                        aria-label="预览图片"
                    >
                        <img
                            src={previewUrl}
                            alt={delivery.filename}
                            loading="lazy"
                            draggable={!protectImage}
                        />
                    </button>
                ) : (
                    <div className="chat-file-delivery__thumb" aria-hidden="true">
                        {isImage ? (
                            <IconPhoto size={20} stroke={1.8} />
                        ) : (
                            <ChatAttachmentIcon
                                name={delivery.filename}
                                mimeType={delivery.mimeType}
                                size={20}
                                stroke={1.8}
                            />
                        )}
                    </div>
                )}
                <div className="chat-file-delivery__meta">
                    <div className="chat-file-delivery__name" title={delivery.filename}>{delivery.filename}</div>
                    {details ? <div className="chat-file-delivery__details">{details}</div> : null}
                </div>
                {downloadUrl && !protectImage ? (
                    <a
                        className="chat-file-delivery__download"
                        href={downloadUrl}
                        download={delivery.filename}
                        aria-label="下载文件"
                        title="下载文件"
                    >
                        <IconDownload size={18} stroke={1.8} />
                    </a>
                ) : null}
            </div>
        </div>
    );
}
