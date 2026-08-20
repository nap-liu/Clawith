import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import {
    IconArrowUpRight,
    IconMessages,
    IconRefresh,
    IconX,
} from '@tabler/icons-react';

import ConversationTimeline, { type ConversationTimelineProps } from '../features/conversation/web/ConversationTimeline';
import { mapHistoryMessage, type ConversationMessage } from '../features/conversation/core/chatTimeline';
import { chatSessionApi } from '../services/api';
import type { ChatPreviewImage } from '../utils/chatAttachments';

export type SessionViewerTarget = {
    sessionId: string;
    agentId?: string;
    title?: string;
    status?: string;
    mode?: string;
    model?: string;
};

type SessionViewerDrawerProps = {
    agentId: string;
    agentName: string;
    target: SessionViewerTarget | null;
    routeMode?: 'pc' | 'h5';
    portalContainer?: HTMLElement | null;
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
    const scrollerRef = useRef<HTMLDivElement>(null);
    const drawerRef = useRef<HTMLElement>(null);
    const closeButtonRef = useRef<HTMLButtonElement>(null);
    const requestSequenceRef = useRef(0);
    const sessionId = target?.sessionId;
    const accessAgentId = target?.agentId || agentId;

    const loadSession = useCallback(async (background = false) => {
        if (!sessionId || !accessAgentId) return;
        const sequence = ++requestSequenceRef.current;
        if (!background) setLoading(true);
        try {
            const [detail, rows] = await Promise.all([
                chatSessionApi.get(accessAgentId, sessionId),
                chatSessionApi.messages(accessAgentId, sessionId, 500),
            ]);
            if (sequence !== requestSequenceRef.current) return;
            const normalized = (Array.isArray(rows) ? rows : [])
                .map((row) => mapHistoryMessage(row))
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
    }, [accessAgentId, sessionId, t]);

    useEffect(() => {
        if (!sessionId) return;
        setSession(null);
        setMessages([]);
        setError('');
        void loadSession(false);
        return () => {
            requestSequenceRef.current += 1;
        };
    }, [loadSession, sessionId]);

    const runtime = session?.runtime;
    const currentStatus = String(runtime?.status || target?.status || '').toLowerCase();
    const active = ACTIVE_STATUSES.has(currentStatus);

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
        const onKeyDown = (event: KeyboardEvent) => {
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
                        {routeMode === 'pc' && (
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
                    <span>{t('agent.sessionViewer.readOnly')}</span>
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
                            onPreviewImages={onPreviewImages}
                            unavailableAttachmentKeys={unavailableAttachmentKeys}
                            onAttachmentDownload={onAttachmentDownload}
                            onAttachmentUnavailable={onAttachmentUnavailable}
                            viewOf={(message) => ({
                                isLeft: message.role !== 'user',
                                senderLabel: message.role === 'user'
                                    ? (isSubagent ? t('agent.sessionViewer.caller') : participantName)
                                    : executionAgentName,
                                avatarText: message.role === 'user'
                                    ? (isSubagent ? t('agent.sessionViewer.callerAvatar') : (participantName[0] || 'U'))
                                    : (executionAgentName[0] || 'A'),
                                forceSenderLabel: true,
                            })}
                        />
                    )}
                </div>
            </aside>
        </div>,
        portalContainer || document.body,
    );
}
