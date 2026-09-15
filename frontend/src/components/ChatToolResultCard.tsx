import { useTranslation } from 'react-i18next';
import { IconTools } from '@tabler/icons-react';
import ChatFileDeliveryCard from './ChatFileDeliveryCard';
import ChatMediaCard from './ChatMediaCard';
import type { ChatPreviewImage } from '../utils/chatAttachments';
import type { ToolResultDisplay } from '../utils/chatToolResult';
import './ChatToolResultCard.css';

type Props = {
    agentId: string;
    messageId: string;
    name: string;
    args: unknown;
    result: ToolResultDisplay;
    mode: 'pc' | 'h5';
    onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
};

export default function ChatToolResultCard({ agentId, messageId, name, args, result, mode, onPreviewImages }: Props) {
    const { t } = useTranslation();
    return (
        <section className="chat-tool-result" aria-label={t('toolResult.title')}>
            <header className="chat-tool-result__header">
                <IconTools size={16} aria-hidden="true" />
                <strong>{name}</strong>
                {result.isError && <span role="status">{t('toolResult.failed')}</span>}
            </header>
            <div className="chat-tool-result__content">
                {result.content.map((block, index) => {
                    if (block.type === 'text') return <pre key={index} className="chat-tool-result__text">{block.text}</pre>;
                    if (block.type === 'unavailable') return <span key={index}>{t('toolResult.unavailable')}</span>;
                    const attachment = block.attachment;
                    if (attachment.kind === 'audio' || attachment.kind === 'video') return (
                        <ChatMediaCard key={index} agentId={agentId} messageId={messageId}
                            attachment={attachment} mode={mode} />
                    );
                    return (
                        <ChatFileDeliveryCard key={index} agentId={agentId} messageId={messageId}
                            delivery={{ id: attachment.path, path: attachment.path, filename: attachment.display_name,
                                mimeType: attachment.mime_type, size: attachment.size_bytes }}
                            mode={mode} inlineImage onPreviewImages={onPreviewImages} />
                    );
                })}
            </div>
            {args != null && Object.keys(Object(args)).length > 0 && (
                <details className="conversation-tool-details">
                    <summary>{t('agent.chat.viewDetails')}</summary>
                    <pre>{JSON.stringify(args, null, 2)}</pre>
                </details>
            )}
        </section>
    );
}
