import { useEffect } from 'react';

import Button from '../../../../components/ui/Button';
import ConversationTimeline from '../../../../features/conversation/web/ConversationTimeline';
import { formatReflectionTitle } from '../../shared';
import type { Translate } from './types';

const PAGE_SIZE = 10;

function executionSourceLabel(source: string, t: Translate) {
    return t(`agent.aware.workspace.executionSources.${source}`, { defaultValue: source });
}

type Props = {
    sessions: any[];
    page: number;
    setPage: (update: number | ((page: number) => number)) => void;
    selectedId: string | null;
    setSelectedId: (id: string | null) => void;
    messages: Record<string, any[]>;
    loadMessages: (conversationId: string) => Promise<any>;
    agentId: string;
    agentName: string;
    locale?: string;
    t: Translate;
    openSubagentSession: (run: any) => void;
    unavailableAttachmentKeys: Set<string>;
    handleAttachmentDownload: (path: string, name: string) => Promise<void>;
    markAttachmentUnavailable: (key: string) => void;
    setChatImagePreview: (value: any) => void;
    upsertToolCallMessage: (message: any) => void;
};

export default function AwareExecutionsPane({
    sessions,
    page,
    setPage,
    selectedId,
    setSelectedId,
    messages,
    loadMessages,
    agentId,
    agentName,
    locale,
    t,
    openSubagentSession,
    unavailableAttachmentKeys,
    handleAttachmentDownload,
    markAttachmentUnavailable,
    setChatImagePreview,
    upsertToolCallMessage,
}: Props) {
    const totalPages = Math.max(1, Math.ceil(sessions.length / PAGE_SIZE));
    const visible = sessions.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
    const selected = sessions.find((session) => (session.record_id || session.id) === selectedId) || visible[0];
    const selectedRecordId = selected?.record_id || selected?.id;
    const conversationId = selected?.conversation_id || (!selected?.conversation_missing ? selected?.id : null);
    const timeline = conversationId ? messages[conversationId] || [] : [];
    const timelineLoaded = !conversationId || Object.prototype.hasOwnProperty.call(messages, conversationId);

    useEffect(() => {
        if (!selectedRecordId) {
            setSelectedId(null);
            return;
        }
        if (selectedId !== selectedRecordId) setSelectedId(selectedRecordId);
        if (conversationId && !Object.prototype.hasOwnProperty.call(messages, conversationId)) void loadMessages(conversationId);
    }, [conversationId, loadMessages, messages, selectedId, selectedRecordId, setSelectedId]);

    const choose = async (session: any) => {
        const id = session.record_id || session.id;
        setSelectedId(id);
        const nextConversationId = session.conversation_id || (!session.conversation_missing ? session.id : null);
        if (nextConversationId) await loadMessages(nextConversationId);
    };
    const execution = selected?.execution;
    const selectedStatus = execution?.status || 'completed';

    return (
        <div className="aware-split aware-executions">
            <section className="aware-pane aware-execution-list">
                <header className="aware-pane-header">
                    <h3>{t('agent.aware.workspace.executions')}</h3>
                    <span className="aware-pane-summary">{sessions.length}</span>
                </header>
                <div className="aware-execution-rows">
                    {visible.map((session) => {
                        const recordId = session.record_id || session.id;
                        const status = session.execution?.status || 'completed';
                        return (
                            <button type="button" className={`aware-execution-row${recordId === selectedRecordId ? ' active' : ''}`} key={recordId} onClick={() => choose(session)}>
                                <span className={`aware-run-dot aware-run-dot--${status}`} />
                                <span><strong>{session.execution ? `${session.execution.trigger_name} · ${executionSourceLabel(session.execution.source, t)}` : formatReflectionTitle(session.title, !!locale?.startsWith('zh'))}</strong><small>{new Date(session.created_at).toLocaleString(locale)}{session.message_count ? ` · ${t('agent.aware.workspace.messageCount', { count: session.message_count })}` : ''}</small></span>
                                <span className={`aware-status-pill aware-status-pill--${status}`}>{t(`agent.aware.workspace.status.${status}`, { defaultValue: status })}</span>
                            </button>
                        );
                    })}
                    {visible.length === 0 && <div className="aware-empty">{t('agent.aware.workspace.noExecutions')}</div>}
                </div>
                {totalPages > 1 && (
                    <div className="aware-pagination">
                        <Button variant="secondary" disabled={page === 0} onClick={() => { setPage(Math.max(0, page - 1)); setSelectedId(null); }}>{t('agent.aware.workspace.previous')}</Button>
                        <span>{page + 1} / {totalPages}</span>
                        <Button variant="secondary" disabled={page >= totalPages - 1} onClick={() => { setPage(Math.min(totalPages - 1, page + 1)); setSelectedId(null); }}>{t('agent.aware.workspace.next')}</Button>
                    </div>
                )}
            </section>
            <section className="aware-pane aware-execution-detail">
                {!selected ? <div className="aware-empty">{t('agent.aware.workspace.noExecutions')}</div> : (
                    <>
                        <header className="aware-detail-header">
                            <div className="aware-detail-title-line"><h3>{execution?.trigger_name || formatReflectionTitle(selected.title, !!locale?.startsWith('zh'))}</h3><span className={`aware-status-pill aware-status-pill--${selectedStatus}`}>{t(`agent.aware.workspace.status.${selectedStatus}`, { defaultValue: selectedStatus })}</span></div>
                        </header>
                        {execution && (
                            <div className="aware-provenance">
                                <span><small>{t('agent.aware.workspace.provenance.source')}</small><strong>{executionSourceLabel(execution.source, t)}</strong></span>
                                <span><small>{t('agent.aware.workspace.provenance.scheduled')}</small><strong>{new Date(execution.scheduled_at).toLocaleString(locale)}</strong></span>
                                <span><small>{t('agent.aware.workspace.provenance.started')}</small><strong>{execution.started_at ? new Date(execution.started_at).toLocaleString(locale) : '—'}</strong></span>
                                <span><small>{t('agent.aware.workspace.provenance.finished')}</small><strong>{execution.finished_at ? new Date(execution.finished_at).toLocaleString(locale) : '—'}</strong></span>
                            </div>
                        )}
                        {execution?.last_error && <div className="aware-execution-error" role="alert"><strong>{t('agent.aware.workspace.lastError')}</strong><span>{execution.last_error}</span></div>}
                        <div className="aware-timeline">
                            {!timelineLoaded ? <div className="aware-empty aware-empty--compact">{t('common.loading')}</div> : (
                                <ConversationTimeline
                                    agentId={agentId}
                                    agentName={agentName}
                                    messages={timeline}
                                    provenance={execution ? { ...execution, source: executionSourceLabel(execution.source, t) } : execution}
                                    viewOf={(message) => ({
                                        isLeft: message.role !== 'user',
                                        senderLabel: message.role === 'user' ? t('agent.aware.workspace.triggerEvent') : agentName,
                                        avatarText: message.role === 'user' ? 'T' : (agentName || 'A')[0],
                                        forceSenderLabel: true,
                                    })}
                                    unavailableAttachmentKeys={unavailableAttachmentKeys}
                                    onAttachmentDownload={handleAttachmentDownload}
                                    onAttachmentUnavailable={markAttachmentUnavailable}
                                    onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                    onOpenSubagentSession={openSubagentSession}
                                    onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result })}
                                />
                            )}
                            {timelineLoaded && timeline.length === 0 && <div className="aware-empty aware-empty--compact">{t('agent.aware.workspace.noConversation')}</div>}
                        </div>
                    </>
                )}
            </section>
        </div>
    );
}
