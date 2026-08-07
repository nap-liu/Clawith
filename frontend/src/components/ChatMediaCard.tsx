import { IconAlertTriangle, IconDownload, IconPlayerPlayFilled, IconRefresh } from '@tabler/icons-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { fileApi } from '../services/api';
import type { ChatMessageAttachment } from '../utils/chatAttachments';
import { formatFileSize } from '../utils/formatFileSize';
import ChatAttachmentIcon from './ChatAttachmentIcon';
import './ChatMediaCard.css';

type Props = {
    agentId: string;
    messageId: string;
    attachment: ChatMessageAttachment;
    mode?: 'pc' | 'h5';
    onDownload?: () => void;
    onUnavailable?: () => void;
};

type Ticket = Awaited<ReturnType<typeof fileApi.createPlaybackTicket>>;

let activeMediaElement: HTMLMediaElement | null = null;

function readableError(error: unknown) {
    const value = error as any;
    const detail = value?.detail?.error || value?.detail;
    return detail?.message || value?.message || '媒体暂时无法播放';
}

export default function ChatMediaCard({
    agentId,
    messageId,
    attachment,
    mode = 'pc',
    onDownload,
    onUnavailable,
}: Props) {
    const mediaRef = useRef<HTMLMediaElement | null>(null);
    const recoveryRef = useRef(false);
    const automaticRecoveryCountRef = useRef(0);
    const resumeAtRef = useRef(0);
    const [ticket, setTicket] = useState<Ticket | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');

    const requestPlayback = useCallback(async (resumeAt = 0, manual = false) => {
        if (!agentId || loading) return;
        if (manual) automaticRecoveryCountRef.current = 0;
        setLoading(true);
        setError('');
        try {
            const next = await fileApi.createPlaybackTicket(agentId, attachment.path, messageId);
            resumeAtRef.current = resumeAt;
            setTicket(next);
        } catch (nextError) {
            setError(readableError(nextError));
            onUnavailable?.();
        } finally {
            setLoading(false);
        }
    }, [agentId, attachment.path, loading, messageId, onUnavailable]);

    const handleMediaError = useCallback(async () => {
        const media = mediaRef.current;
        const resumeAt = media?.currentTime || 0;
        if (!ticket || recoveryRef.current || automaticRecoveryCountRef.current >= 1) {
            setError('播放中断，请重新加载');
            return;
        }
        if (media?.error?.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
            setError('当前浏览器不支持此媒体格式，可下载后播放');
            return;
        }
        recoveryRef.current = true;
        try {
            const url = new URL(ticket.playback_url, window.location.origin);
            const signature = url.searchParams.get('signature') || '';
            const status = await fileApi.playbackStatus(agentId, ticket.playback_session_id, signature);
            if (status.status === 'ready') {
                setError('媒体源暂时无法播放，请重新加载或下载文件');
                return;
            }
        } catch {
            automaticRecoveryCountRef.current += 1;
            await requestPlayback(resumeAt, false);
        } finally {
            recoveryRef.current = false;
        }
    }, [agentId, requestPlayback, ticket]);

    const handleLoadedMetadata = useCallback(() => {
        const media = mediaRef.current;
        const resumeAt = resumeAtRef.current;
        if (media && resumeAt > 0 && Number.isFinite(resumeAt)) {
            media.currentTime = Math.min(resumeAt, Number.isFinite(media.duration) ? media.duration : resumeAt);
        }
        resumeAtRef.current = 0;
    }, []);

    const handlePlay = useCallback(() => {
        const media = mediaRef.current;
        if (!media) return;
        if (activeMediaElement && activeMediaElement !== media) activeMediaElement.pause();
        activeMediaElement = media;
        setError('');
    }, []);

    useEffect(() => () => {
        if (mediaRef.current) {
            mediaRef.current.pause();
            mediaRef.current.removeAttribute('src');
        }
        if (activeMediaElement === mediaRef.current) activeMediaElement = null;
    }, []);

    const details = [
        attachment.size_bytes !== undefined ? formatFileSize(attachment.size_bytes) : '',
        attachment.mime_type || '',
    ].filter(Boolean).join(' · ');
    const isVideo = attachment.kind === 'video';

    return (
        <section className={`chat-media-card chat-media-card--${mode} chat-media-card--${attachment.kind}`}>
            <div className="chat-media-card__header">
                <span className="chat-media-card__icon" aria-hidden="true">
                    <ChatAttachmentIcon
                        name={attachment.display_name}
                        kind={attachment.kind}
                        mimeType={attachment.mime_type}
                        size={19}
                    />
                </span>
                <span className="chat-media-card__identity">
                    <strong title={attachment.display_name}>{attachment.display_name}</strong>
                    {details ? <small>{details}</small> : null}
                </span>
                {onDownload ? (
                    <button type="button" className="chat-media-card__icon-button" onClick={onDownload} aria-label="下载媒体">
                        <IconDownload size={17} stroke={1.8} />
                    </button>
                ) : null}
            </div>

            {ticket ? (
                isVideo ? (
                    <video
                        ref={(node) => { mediaRef.current = node; }}
                        className="chat-media-card__video"
                        src={ticket.playback_url}
                        controls
                        playsInline
                        preload="metadata"
                        onError={() => void handleMediaError()}
                        onLoadedMetadata={handleLoadedMetadata}
                        onPlay={handlePlay}
                    />
                ) : (
                    <audio
                        ref={(node) => { mediaRef.current = node; }}
                        className="chat-media-card__audio"
                        src={ticket.playback_url}
                        controls
                        preload="metadata"
                        onError={() => void handleMediaError()}
                        onLoadedMetadata={handleLoadedMetadata}
                        onPlay={handlePlay}
                    />
                )
            ) : (
                <button
                    type="button"
                    className="chat-media-card__start"
                    disabled={loading}
                    onClick={() => void requestPlayback(0, true)}
                >
                    <span className="chat-media-card__play"><IconPlayerPlayFilled size={14} /></span>
                    <span>{loading ? '正在获取播放权限…' : isVideo ? '播放视频' : '播放音频'}</span>
                    <span className="chat-media-card__track" aria-hidden="true"><i /></span>
                </button>
            )}

            {error ? (
                <div className="chat-media-card__error" role="status">
                    <IconAlertTriangle size={15} stroke={1.8} />
                    <span>{error}</span>
                    <button type="button" onClick={() => void requestPlayback(mediaRef.current?.currentTime || 0, true)} aria-label="重新加载">
                        <IconRefresh size={15} stroke={1.8} />
                    </button>
                </div>
            ) : null}
        </section>
    );
}
