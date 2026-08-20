import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type ClipboardEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import {
    IconArrowUpRight,
    IconPaperclip,
    IconPlayerStopFilled,
    IconMessages,
    IconRefresh,
    IconSend,
    IconTrash,
    IconX,
} from '@tabler/icons-react';

import ConversationTimeline, { type ConversationTimelineProps } from '../features/conversation/web/ConversationTimeline';
import ChatImageLightbox from './ChatImageLightbox';
import MultiSelectDropdown from './ui/MultiSelectDropdown';
import {
    applyAssistantDoneMessage,
    applyAssistantStreamMessage,
    mapHistoryMessage,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
    type ConversationMessage,
} from '../features/conversation/core/chatTimeline';
import { chatSessionApi, fileApi, uploadFileWithProgress } from '../services/api';
import {
    buildChatAttachmentPayload,
    downloadChatAttachment,
    normalizeChatAttachmentFields,
    type ChatAttachedFile,
    type ChatPreviewImage,
} from '../utils/chatAttachments';
import { createClientId } from '../utils/clientId';

export type SessionViewerTarget = {
    sessionId: string;
    agentId?: string;
    title?: string;
    status?: string;
    mode?: string;
    model?: string;
};

export type SessionViewerGroupConfig = {
    members: Array<{ agentId: string; name: string }>;
    currentAgentId?: string;
    maxMentions?: number;
    loadMessages: (sessionId: string) => Promise<unknown[]>;
    sendMessage: (sessionId: string, payload: {
        content: string;
        llm_content?: string;
        mentions: string[];
        attachments: ReturnType<typeof buildChatAttachmentPayload>['attachments'];
    }) => Promise<{
        message?: Record<string, any>;
        awakened_agent_ids?: string[];
        subagent_runs?: Array<{ run_id: string; session_id: string; agent_id: string; status: string }>;
    }>;
};

type SessionViewerDrawerProps = {
    agentId: string;
    agentName: string;
    target: SessionViewerTarget | null;
    routeMode?: 'pc' | 'h5';
    portalContainer?: HTMLElement | null;
    interactive?: boolean;
    groupConfig?: SessionViewerGroupConfig;
    onClose: () => void;
    onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
    unavailableAttachmentKeys?: ReadonlySet<string>;
    onAttachmentDownload?: ConversationTimelineProps['onAttachmentDownload'];
    onAttachmentUnavailable?: ConversationTimelineProps['onAttachmentUnavailable'];
};

const ACTIVE_STATUSES = new Set(['queued', 'pending', 'running', 'processing']);

export default function SessionViewerDrawer({
    agentId,
    agentName,
    target,
    routeMode = 'pc',
    portalContainer,
    interactive = false,
    groupConfig,
    onClose,
    onPreviewImages,
    unavailableAttachmentKeys,
    onAttachmentDownload,
    onAttachmentUnavailable,
}: SessionViewerDrawerProps) {
    const { t } = useTranslation();
    const [session, setSession] = useState<Record<string, any> | null>(null);
    const [messages, setMessages] = useState<ConversationMessage[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [composerError, setComposerError] = useState('');
    const [draft, setDraft] = useState('');
    const [attachedFiles, setAttachedFiles] = useState<ChatAttachedFile[]>([]);
    const [uploads, setUploads] = useState<Array<{ id: string; name: string; percent: number }>>([]);
    const [connected, setConnected] = useState(false);
    const [sending, setSending] = useState(false);
    const [serverReadOnly, setServerReadOnly] = useState(false);
    const [mentions, setMentions] = useState<string[]>([]);
    const [internalUnavailableAttachments, setInternalUnavailableAttachments] = useState<Set<string>>(() => new Set());
    const [internalPreview, setInternalPreview] = useState<{ images: ChatPreviewImage[]; index: number } | null>(null);
    const scrollerRef = useRef<HTMLDivElement>(null);
    const drawerRef = useRef<HTMLElement>(null);
    const closeButtonRef = useRef<HTMLButtonElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const socketRef = useRef<WebSocket | null>(null);
    const uploadAbortRef = useRef(new Map<string, () => void>());
    const requestSequenceRef = useRef(0);
    const sessionId = target?.sessionId;
    const accessAgentId = target?.agentId || agentId;
    const canCompose = interactive && !serverReadOnly;
    const mentionLimit = Math.max(1, groupConfig?.maxMentions || 8);
    const mentionOptions = useMemo(() => groupConfig?.members
        .filter((member) => member.agentId !== groupConfig.currentAgentId)
        .map((member) => ({ value: member.agentId, label: member.name })) || [], [groupConfig]);

    const loadSession = useCallback(async (background = false) => {
        if (!sessionId || !accessAgentId) return;
        const sequence = ++requestSequenceRef.current;
        if (!background) setLoading(true);
        try {
            const [detail, rows] = await Promise.all([
                chatSessionApi.get(accessAgentId, sessionId).catch(() => ({ id: sessionId })),
                groupConfig
                    ? groupConfig.loadMessages(sessionId)
                    : chatSessionApi.messages(accessAgentId, sessionId, 500),
            ]);
            if (sequence !== requestSequenceRef.current) return;
            const normalized = (Array.isArray(rows) ? rows : [])
                .map((row) => {
                    const raw = row && typeof row === 'object' ? row as Record<string, any> : {};
                    if (raw.role === 'tool_call') return mapHistoryMessage(raw);
                    const attachmentFields = normalizeChatAttachmentFields({
                        raw,
                        sourceChannel: String((detail as Record<string, any> | null)?.source_channel || ''),
                        buildDownloadUrl: (path, inline) => fileApi.downloadUrl(accessAgentId, path, { inline }),
                    });
                    const mapped = mapHistoryMessage({
                        ...raw,
                        display_content: attachmentFields.displayContent,
                        attachments: attachmentFields.attachments,
                    });
                    return mapped ? {
                        ...mapped,
                        previewImages: attachmentFields.previewImages,
                        fileName: attachmentFields.fileName,
                        imageUrl: attachmentFields.imageUrl,
                    } : null;
                })
                .filter((message): message is ConversationMessage => Boolean(message));
            setSession(detail);
            setMessages(normalized);
            setError('');
        } catch (loadError: any) {
            if (sequence !== requestSequenceRef.current) return;
            setError(loadError?.message || t('agent.sessionViewer.loadError'));
        } finally {
            if (sequence === requestSequenceRef.current && !background) setLoading(false);
        }
    }, [accessAgentId, groupConfig, sessionId, t]);

    useEffect(() => {
        if (!sessionId) return;
        setSession(null);
        setMessages([]);
        setError('');
        setComposerError('');
        setDraft('');
        setAttachedFiles([]);
        setUploads([]);
        setConnected(false);
        setSending(false);
        setServerReadOnly(false);
        setMentions([]);
        setInternalUnavailableAttachments(new Set());
        return () => {
            requestSequenceRef.current += 1;
        };
    }, [accessAgentId, sessionId]);

    // Refreshing the group configuration (for example after a workspace data
    // poll) must not clear an in-progress draft or its structured mentions.
    // Only the target-change effect above resets composer state.
    useEffect(() => {
        if (!sessionId) return;
        void loadSession(false);
    }, [loadSession, sessionId]);

    useEffect(() => {
        if (!interactive || groupConfig || !sessionId || !accessAgentId) return;
        const token = localStorage.getItem('token');
        if (!token) {
            setComposerError(t('agent.sessionViewer.authenticationRequired', '登录已失效，无法继续对话。'));
            return;
        }

        let disposed = false;
        let reconnectTimer: number | null = null;
        let reconnectAttempt = 0;
        const connect = () => {
            if (disposed || document.hidden) return;
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const language = document.documentElement.lang.toLowerCase().startsWith('zh') ? 'zh' : 'en';
            const socket = new WebSocket(`${protocol}//${window.location.host}/ws/chat/${accessAgentId}?token=${encodeURIComponent(token)}&session_id=${encodeURIComponent(sessionId)}&lang=${language}`);
            socketRef.current = socket;
            socket.onopen = () => {
                if (disposed || socketRef.current !== socket) return;
                reconnectAttempt = 0;
                setComposerError('');
            };
            socket.onmessage = (event) => {
                if (disposed || socketRef.current !== socket) return;
                let payload: Record<string, any>;
                try {
                    payload = JSON.parse(String(event.data));
                } catch {
                    return;
                }
                const messageId = payload.message_id ? String(payload.message_id) : undefined;
                if (payload.type === 'connected') {
                    setConnected(true);
                    setServerReadOnly(payload.read_only === true);
                    if (payload.read_only === true) {
                        setComposerError(t('agent.sessionViewer.readOnlySession', '该会话仅允许查看，不能继续发送消息。'));
                    }
                    return;
                }
                if (payload.type === 'thinking' || payload.type === 'chunk') {
                    setSending(true);
                    setMessages((previous) => applyAssistantStreamMessage(previous, {
                        type: payload.type,
                        content: String(payload.content || ''),
                        messageId,
                    }));
                    return;
                }
                if (payload.type === 'workspace_draft' || payload.type === 'tool_call' || payload.type === 'confirmation_required') {
                    setSending(true);
                    setMessages((previous) => upsertToolCallMessage(previous, toolCallMessageFromEvent({
                        ...payload,
                        status: payload.type === 'confirmation_required' ? 'running' : payload.status,
                    })));
                    return;
                }
                if (payload.type === 'assistant_message_committed') {
                    const committed = mapHistoryMessage({
                        id: payload.id,
                        role: 'assistant',
                        content: payload.content || '',
                        display_content: payload.display_content,
                        attachments: payload.attachments,
                        created_at: payload.created_at,
                    });
                    if (committed) {
                        setMessages((previous) => {
                            const index = previous.findIndex((message) => message.id === committed.id);
                            return index < 0
                                ? [...previous, committed]
                                : [...previous.slice(0, index), { ...previous[index], ...committed }, ...previous.slice(index + 1)];
                        });
                    }
                    return;
                }
                if (payload.type === 'user_message_committed') {
                    const clientId = String(payload.client_message_id || '');
                    const durableId = String(payload.message_id || '');
                    if (clientId && durableId) {
                        setMessages((previous) => previous.map((message) => message.id === clientId ? { ...message, id: durableId } : message));
                    }
                    return;
                }
                if (payload.type === 'channel_user_message') {
                    const committed = mapHistoryMessage({ ...payload, role: 'user', id: payload.id || createClientId() });
                    if (committed) setMessages((previous) => previous.some((message) => message.id === committed.id) ? previous : [...previous, committed]);
                    return;
                }
                if (payload.type === 'done') {
                    setMessages((previous) => applyAssistantDoneMessage(previous, {
                        content: String(payload.content || ''),
                        messageId,
                    }));
                    setSending(false);
                    window.setTimeout(() => void loadSession(true), 250);
                    return;
                }
                if (payload.type === 'error' || payload.type === 'quota_exceeded') {
                    setComposerError(String(payload.content || payload.detail || payload.message || t('agent.sessionViewer.sendError', '消息发送失败。')));
                    setSending(false);
                }
            };
            socket.onerror = () => setConnected(false);
            socket.onclose = (event) => {
                if (socketRef.current === socket) socketRef.current = null;
                setConnected(false);
                setSending(false);
                if (disposed || event.code === 4001 || event.code === 4002 || event.code === 4003) return;
                const delay = Math.min(12_000, 800 * (2 ** reconnectAttempt));
                reconnectAttempt += 1;
                reconnectTimer = window.setTimeout(connect, delay);
            };
        };
        const handleVisibility = () => {
            if (!document.hidden && !socketRef.current) connect();
        };
        connect();
        document.addEventListener('visibilitychange', handleVisibility);
        return () => {
            disposed = true;
            if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
            document.removeEventListener('visibilitychange', handleVisibility);
            const socket = socketRef.current;
            socketRef.current = null;
            if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'session drawer closed');
        };
    }, [accessAgentId, groupConfig, interactive, loadSession, sessionId, t]);

    useEffect(() => {
        if (!interactive || !groupConfig || !sessionId) return;
        setConnected(true);
        const timer = window.setInterval(() => void loadSession(true), 3000);
        return () => {
            setConnected(false);
            window.clearInterval(timer);
        };
    }, [groupConfig, interactive, loadSession, sessionId]);

    useEffect(() => () => {
        uploadAbortRef.current.forEach((abort) => abort());
        uploadAbortRef.current.clear();
    }, []);

    const runtime = session?.runtime;
    const currentStatus = String(runtime?.status || target?.status || '').toLowerCase();
    const active = ACTIVE_STATUSES.has(currentStatus) || sending;

    useEffect(() => {
        if (!sessionId || !active) return;
        const timer = window.setInterval(() => void loadSession(true), 3000);
        return () => window.clearInterval(timer);
    }, [active, loadSession, sessionId]);

    useEffect(() => {
        if (!target) return;
        const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        window.requestAnimationFrame(() => closeButtonRef.current?.focus());
        const onKeyDown = (event: globalThis.KeyboardEvent) => {
            if (event.key === 'Escape') {
                onClose();
                return;
            }
            if (event.key !== 'Tab' || !drawerRef.current) return;
            const focusable = Array.from(
                drawerRef.current.querySelectorAll<HTMLElement>(
                    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
                ),
            ).filter((element) => !element.hasAttribute('disabled'));
            if (!focusable.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => {
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', onKeyDown);
            previousFocus?.focus();
        };
    }, [onClose, target?.sessionId]);

    useEffect(() => {
        if (!messages.length) return;
        window.requestAnimationFrame(() => {
            const element = scrollerRef.current;
            if (element) element.scrollTop = element.scrollHeight;
        });
    }, [messages.length, sessionId]);

    if (!target || !sessionId) return null;

    const uploadFiles = async (files: File[]) => {
        if (!accessAgentId || files.length === 0) return;
        const allowed = files.slice(0, Math.max(0, 10 - attachedFiles.length - uploads.length));
        if (allowed.length === 0) {
            setComposerError(t('agent.sessionViewer.attachmentLimit', '每条消息最多附加 10 个文件。'));
            return;
        }
        setComposerError('');
        await Promise.all(allowed.map(async (file) => {
            const uploadId = `session-upload-${createClientId()}`;
            setUploads((previous) => [...previous, { id: uploadId, name: file.name, percent: 0 }]);
            const { promise, abort } = uploadFileWithProgress(
                '/chat/upload',
                file,
                (progress) => setUploads((previous) => previous.map((upload) => upload.id === uploadId ? { ...upload, percent: Math.min(progress, 100) } : upload)),
                { agent_id: accessAgentId },
                600_000,
            );
            uploadAbortRef.current.set(uploadId, abort);
            try {
                const result = await promise;
                setAttachedFiles((previous) => [...previous, {
                    name: result.saved_filename || result.filename || file.name,
                    text: result.extracted_text || '',
                    path: result.workspace_path,
                    imageUrl: result.image_data_url || undefined,
                    mimeType: file.type || undefined,
                    sizeBytes: result.size ?? file.size,
                    source: 'upload' as const,
                }].slice(0, 10));
            } catch (uploadError: any) {
                if (uploadError?.message !== 'Upload cancelled') {
                    setComposerError(uploadError?.message || t('agent.sessionViewer.uploadError', '附件上传失败。'));
                }
            } finally {
                uploadAbortRef.current.delete(uploadId);
                setUploads((previous) => previous.filter((upload) => upload.id !== uploadId));
            }
        }));
    };

    const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(event.target.files || []);
        event.target.value = '';
        void uploadFiles(files);
    };

    const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
        const images = Array.from(event.clipboardData?.items || [])
            .filter((item) => item.type.startsWith('image/'))
            .map((item, index) => {
                const file = item.getAsFile();
                if (!file) return null;
                const extension = file.type.split('/')[1] || 'png';
                return new File([file], `paste-${Date.now()}-${index}.${extension}`, { type: file.type });
            })
            .filter((file): file is File => Boolean(file));
        if (!images.length) return;
        event.preventDefault();
        void uploadFiles(images);
    };

    const sendMessage = async () => {
        const socket = socketRef.current;
        if (!canCompose || !connected || sending) return;
        if (!draft.trim() && attachedFiles.length === 0) return;
        const attachmentPayload = buildChatAttachmentPayload({ input: draft.trim(), attachments: attachedFiles });
        const clientMessageId = createClientId();
        setMessages((previous) => [...previous, {
            id: clientMessageId,
            role: 'user',
            content: attachmentPayload.displayContent,
            display_content: attachmentPayload.displayContent,
            attachments: attachmentPayload.attachments,
            previewImages: attachmentPayload.previewImages,
            fileName: attachmentPayload.fileName,
            imageUrl: attachmentPayload.imageUrl,
            created_at: new Date().toISOString(),
        }]);
        if (groupConfig) {
            setSending(true);
            try {
                const result = await groupConfig.sendMessage(sessionId, {
                    content: attachmentPayload.displayContent || t('agent.sessionViewer.attachmentOnlyMessage', '请查看附件'),
                    llm_content: attachmentPayload.contentForLLM,
                    mentions,
                    attachments: attachmentPayload.attachments,
                });
                const committed = result.message ? mapHistoryMessage(result.message) : null;
                if (committed) {
                    setMessages((previous) => previous.map((message) => message.id === clientMessageId ? committed : message));
                }
                setDraft('');
                setAttachedFiles([]);
                setMentions([]);
                setComposerError('');
                if (textareaRef.current) textareaRef.current.style.height = 'auto';
                await loadSession(true);
            } catch (sendError: any) {
                setMessages((previous) => previous.filter((message) => message.id !== clientMessageId));
                setComposerError(sendError?.message || t('agent.sessionViewer.sendError', '消息发送失败。'));
            } finally {
                setSending(false);
            }
            return;
        }
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            setMessages((previous) => previous.filter((message) => message.id !== clientMessageId));
            setComposerError(t('agent.sessionViewer.reconnecting', '连接正在恢复，草稿与附件已保留。'));
            return;
        }
        try {
            socket.send(JSON.stringify({
                message_id: clientMessageId,
                content: attachmentPayload.contentForLLM,
                display_content: attachmentPayload.displayContent,
                file_name: attachmentPayload.fileName,
                attachments: attachmentPayload.attachments,
            }));
            setDraft('');
            setAttachedFiles([]);
            setComposerError('');
            setSending(true);
            if (textareaRef.current) textareaRef.current.style.height = 'auto';
        } catch {
            setMessages((previous) => previous.filter((message) => message.id !== clientMessageId));
            setComposerError(t('agent.sessionViewer.reconnecting', '连接正在恢复，草稿与附件已保留。'));
        }
    };

    const handleComposerKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            void sendMessage();
        }
    };

    const abortTurn = () => {
        if (groupConfig) return;
        const socket = socketRef.current;
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'abort' }));
    };

    const effectiveUnavailableAttachments = unavailableAttachmentKeys || internalUnavailableAttachments;
    const handleAttachmentUnavailable = onAttachmentUnavailable || ((key: string) => {
        setInternalUnavailableAttachments((previous) => new Set(previous).add(key));
    });
    const handleAttachmentDownload = onAttachmentDownload || (async (path: string, displayName: string) => {
        try {
            await downloadChatAttachment(fileApi.downloadUrl(accessAgentId, path), displayName);
        } catch {
            handleAttachmentUnavailable(path);
        }
    });
    const handlePreviewImages = onPreviewImages || ((images: ChatPreviewImage[], index: number) => setInternalPreview({ images, index }));

    const executionAgentId = String(runtime?.execution_agent_id || session?.agent_id || target.agentId || agentId);
    const executionAgentName = String(runtime?.execution_agent_name || agentName || 'Agent');
    const routePrefix = routeMode === 'h5' ? '/h5/agents' : '/agents';
    const fullSessionHref = `${routePrefix}/${executionAgentId}/chat?session_id=${encodeURIComponent(sessionId)}`;
    const isSubagent = runtime?.kind === 'subagent' || session?.source_channel === 'subagent';
    const participantName = String(session?.username || t('agent.sessionViewer.participant'));

    return createPortal(
        <div className={`session-viewer-drawer-layer session-viewer-drawer-layer--${routeMode}`} role="presentation">
            <button
                type="button"
                className="session-viewer-drawer-backdrop"
                aria-label={t('agent.sessionViewer.close')}
                onClick={onClose}
            />
            <aside
                ref={drawerRef}
                className={`session-viewer-drawer session-viewer-drawer--${routeMode}`}
                role="dialog"
                aria-modal="true"
                aria-labelledby="session-viewer-drawer-title"
            >
                <header className="session-viewer-drawer__header">
                    <span className="session-viewer-drawer__mark"><IconMessages size={19} stroke={1.8} /></span>
                    <span className="session-viewer-drawer__heading">
                        <strong id="session-viewer-drawer-title">{t('agent.sessionViewer.title')}</strong>
                        <span>{session?.title || target.title || `#${sessionId.slice(0, 8)}`}</span>
                    </span>
                    <span className="session-viewer-drawer__header-actions">
                        {currentStatus && (
                            <span className={`session-viewer-drawer__live${active ? '' : ' session-viewer-drawer__live--terminal'}`}>
                                {active && <i />}
                                {t(`agent.sessionViewer.status.${currentStatus}`, { defaultValue: currentStatus })}
                            </span>
                        )}
                        {routeMode === 'pc' && !groupConfig && (
                            <a href={fullSessionHref} title={t('agent.sessionViewer.openFullSession')} onClick={onClose}>
                                <IconArrowUpRight size={17} stroke={1.8} />
                            </a>
                        )}
                        <button ref={closeButtonRef} type="button" onClick={onClose} title={t('agent.sessionViewer.close')}>
                            <IconX size={18} stroke={1.8} />
                        </button>
                    </span>
                </header>
                <div className="session-viewer-drawer__context">
                    <span>{canCompose ? t('agent.sessionViewer.interactive', '可继续对话') : t('agent.sessionViewer.readOnly')}</span>
                    {(runtime?.mode || target.mode) && <span>{String(runtime?.mode || target.mode)}</span>}
                    {(runtime?.model || target.model) && <span>{String(runtime?.model || target.model)}</span>}
                    <code>{sessionId}</code>
                </div>
                <div ref={scrollerRef} className="session-viewer-drawer__messages" tabIndex={0}>
                    {loading ? (
                        <div className="session-viewer-drawer__state">
                            <IconRefresh className="subagent-run-card__spin" size={20} stroke={1.8} />
                            <span>{t('agent.sessionViewer.loading')}</span>
                        </div>
                    ) : error ? (
                        <div className="session-viewer-drawer__state session-viewer-drawer__state--error">
                            <strong>{t('agent.sessionViewer.loadError')}</strong>
                            <span>{error}</span>
                            <button type="button" onClick={() => void loadSession(false)}>{t('agent.sessionViewer.retry')}</button>
                        </div>
                    ) : messages.length === 0 ? (
                        <div className="session-viewer-drawer__state">{t('agent.sessionViewer.empty')}</div>
                    ) : (
                        <ConversationTimeline
                            agentId={executionAgentId}
                            agentName={executionAgentName}
                            messages={messages}
                            scrollerRef={scrollerRef}
                            isRunning={active}
                            mode={routeMode}
                            onPreviewImages={handlePreviewImages}
                            unavailableAttachmentKeys={effectiveUnavailableAttachments}
                            onAttachmentDownload={handleAttachmentDownload}
                            onAttachmentUnavailable={handleAttachmentUnavailable}
                            onToolResolved={(message, result) => setMessages((previous) => upsertToolCallMessage(previous, { ...message, toolStatus: 'done', toolResult: result }))}
                            viewOf={(message) => {
                                const groupAgent = groupConfig?.members.find((member) => member.agentId === message.sender_agent_id);
                                const groupSenderName = message.sender_name || groupAgent?.name;
                                if (groupConfig) {
                                    const senderName = groupSenderName || (message.sender_agent_id ? '项目 Agent' : '项目成员');
                                    return {
                                        isLeft: Boolean(message.sender_agent_id),
                                        senderLabel: senderName,
                                        avatarText: senderName[0] || (message.sender_agent_id ? 'A' : 'U'),
                                        forceSenderLabel: true,
                                    };
                                }
                                return {
                                    isLeft: message.role !== 'user',
                                    senderLabel: message.role === 'user'
                                        ? (message.sender_name || (isSubagent ? t('agent.sessionViewer.caller') : participantName))
                                        : executionAgentName,
                                    avatarText: message.role === 'user'
                                        ? ((message.sender_name || (isSubagent ? t('agent.sessionViewer.callerAvatar') : participantName))[0] || 'U')
                                        : (executionAgentName[0] || 'A'),
                                    forceSenderLabel: true,
                                };
                            }}
                        />
                    )}
                </div>
                {interactive && (
                    <footer className="session-viewer-drawer__composer">
                        {(uploads.length > 0 || attachedFiles.length > 0) && (
                            <div className="session-viewer-drawer__attachments">
                                {uploads.map((upload) => (
                                    <span key={upload.id} className="session-viewer-drawer__attachment session-viewer-drawer__attachment--uploading">
                                        <span>{upload.name}</span><small>{upload.percent}%</small>
                                        <button type="button" onClick={() => uploadAbortRef.current.get(upload.id)?.()} aria-label={`${t('common.cancel', '取消')} ${upload.name}`}><IconX size={13} /></button>
                                    </span>
                                ))}
                                {attachedFiles.map((file, index) => (
                                    <span key={`${file.path || file.name}-${index}`} className="session-viewer-drawer__attachment">
                                        <span>{file.name}</span>
                                        <button type="button" onClick={() => setAttachedFiles((previous) => previous.filter((_, itemIndex) => itemIndex !== index))} aria-label={`${t('common.delete', '删除')} ${file.name}`}><IconTrash size={13} /></button>
                                    </span>
                                ))}
                            </div>
                        )}
                        {composerError && <div className="session-viewer-drawer__composer-error" role="status">{composerError}</div>}
                        {groupConfig && (
                            <div className="session-viewer-drawer__mentions">
                                <div className="session-viewer-drawer__mention-control">
                                    <MultiSelectDropdown
                                        options={mentionOptions}
                                        values={mentions}
                                        onChange={(values) => {
                                            if (values.length > mentionLimit) {
                                                setComposerError(`每条消息最多 @ ${mentionLimit} 个 Agent。`);
                                                return;
                                            }
                                            setMentions(Array.from(new Set(values)));
                                            setComposerError('');
                                        }}
                                        emptyLabel="@ 项目成员"
                                        selectedLabel={(count) => `已 @ ${count} 个 Agent`}
                                        searchPlaceholder="搜索项目成员"
                                        noOptionsLabel="没有可提及的 Agent"
                                        noMatchesLabel="没有匹配的 Agent"
                                        ariaLabel="选择要唤醒的项目 Agent"
                                    />
                                    <small>仅 @ 的 Agent 会被唤醒；普通发送只写入共享时间线。</small>
                                </div>
                                {mentions.length > 0 && <div className="session-viewer-drawer__mention-chips">{mentions.map((mentionedAgentId) => {
                                    const member = groupConfig.members.find((entry) => entry.agentId === mentionedAgentId);
                                    return <button key={mentionedAgentId} type="button" onClick={() => setMentions((previous) => previous.filter((id) => id !== mentionedAgentId))}><span>@{member?.name || mentionedAgentId}</span><IconX size={12} /></button>;
                                })}</div>}
                            </div>
                        )}
                        <div className="session-viewer-drawer__composer-row">
                            <input ref={fileInputRef} type="file" multiple hidden onChange={handleFileChange} />
                            <button type="button" className="session-viewer-drawer__composer-icon" onClick={() => fileInputRef.current?.click()} disabled={!canCompose || !connected || sending || uploads.length > 0 || attachedFiles.length >= 10} title={t('agent.workspace.uploadFile', '添加附件')}><IconPaperclip size={17} /></button>
                            <textarea
                                ref={textareaRef}
                                value={draft}
                                onChange={(event) => {
                                    setDraft(event.target.value);
                                    event.target.style.height = 'auto';
                                    event.target.style.height = `${Math.min(event.target.scrollHeight, 132)}px`;
                                }}
                                onKeyDown={handleComposerKeyDown}
                                onPaste={handlePaste}
                                rows={1}
                                disabled={!canCompose || sending}
                                placeholder={serverReadOnly
                                    ? t('agent.sessionViewer.readOnlySession', '该会话仅允许查看，不能继续发送消息。')
                                    : connected
                                        ? t('chat.placeholder', '输入消息…')
                                        : t('agent.sessionViewer.connecting', '正在连接会话…')}
                            />
                            {sending && !groupConfig ? (
                                <button type="button" className="session-viewer-drawer__composer-send session-viewer-drawer__composer-send--stop" onClick={abortTurn} title={t('chat.stop', '停止')}><IconPlayerStopFilled size={16} /></button>
                            ) : sending ? (
                                <button type="button" className="session-viewer-drawer__composer-send" disabled title="正在写入项目共享时间线"><IconRefresh className="subagent-run-card__spin" size={16} /></button>
                            ) : (
                                <button type="button" className="session-viewer-drawer__composer-send" onClick={() => void sendMessage()} disabled={!canCompose || !connected || uploads.length > 0 || (!draft.trim() && attachedFiles.length === 0)} title={t('chat.send', '发送')}><IconSend size={16} /></button>
                            )}
                        </div>
                        <small className="session-viewer-drawer__composer-status">
                            {serverReadOnly ? t('agent.sessionViewer.readOnly', '只读') : connected ? (groupConfig ? '已连接项目共享时间线' : t('agent.sessionViewer.connected', '已连接标准 Web Chat')) : t('agent.sessionViewer.connecting', '正在连接会话…')}
                        </small>
                    </footer>
                )}
            </aside>
            {!onPreviewImages && <ChatImageLightbox open={Boolean(internalPreview)} images={internalPreview?.images || []} index={internalPreview?.index || 0} mode={routeMode === 'h5' ? 'mobile' : 'desktop'} onClose={() => setInternalPreview(null)} onIndexChange={(index) => setInternalPreview((previous) => previous ? { ...previous, index } : previous)} />}
        </div>,
        portalContainer || document.body,
    );
}
