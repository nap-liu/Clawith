import React from 'react';
import AgentSidePanel from '../../../components/AgentSidePanel';
import ChatAttachmentIcon from '../../../components/ChatAttachmentIcon';
import SearchInput from '../../../components/ui/SearchInput';
import ModelSwitcher from '../../../components/ModelSwitcher';
import ReasoningEffortSelect, { type ReasoningEffortValue } from '../../../components/ReasoningEffortSelect';
import ConversationScrollToBottomButton from '../../../features/conversation/ConversationScrollToBottomButton';
import ConversationTimeline from '../../../features/conversation/web/ConversationTimeline';
import {
    IDLE_CONVERSATION_TURN,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import { isA2AMessageLeft } from '../../../features/conversation/core/chatTimeline';
import VirtualSessionList from './VirtualSessionList';
import AwarePreviewPanel from './AwarePreviewPanel';
import { IconPaperclip, IconRobot, IconSend } from '@tabler/icons-react';
import { formatFileSize } from '../../../utils/formatFileSize';

type Props = {
    activeTab: string;
    livePanelVisible: boolean;
    sessionListCollapsed: boolean;
    setSessionListCollapsed: React.Dispatch<React.SetStateAction<boolean>>;
    canViewAllAgentChatSessions: boolean;
    scopeDropdownRef: React.RefObject<HTMLDivElement | null>;
    scopeDropdownOpen: boolean;
    setScopeDropdownOpen: React.Dispatch<React.SetStateAction<boolean>>;
    chatScope: 'mine' | 'all';
    setChatScope: React.Dispatch<React.SetStateAction<'mine' | 'all'>>;
    sessionSearch: string;
    setSessionSearch: React.Dispatch<React.SetStateAction<string>>;
    onAdminTabMine: () => void;
    onAdminTabOthers: () => void;
    t: any;
    createNewSession: () => void;
    id: string | undefined;
    sessions: any[];
    sessionsHasMore: boolean;
    sessionsLoading: boolean;
    sessionsLoadingMore: boolean;
    fetchMySessions: (replace?: boolean, agentId?: string, append?: boolean) => Promise<any>;
    activeSession: any;
    selectSession: (session: any, scope?: 'mine' | 'all') => void;
    i18n: any;
    deleteSession: (sessionId: string) => void;
    othersListForPicker: any[];
    allSessionsHasMore: boolean;
    allSessionsLoading: boolean;
    allSessionsLoadingMore: boolean;
    fetchAllSessions: (replace?: boolean, append?: boolean, agentId?: string) => Promise<any>;
    liveState: any;
    workspaceActivePath: string | null;
    workspaceActivities: any[];
    workspaceLiveDraft: any;
    setLivePanelVisible: React.Dispatch<React.SetStateAction<boolean>>;
    sidePanelTab: any;
    setSidePanelTab: React.Dispatch<React.SetStateAction<any>>;
    focusRecords: any[];
    awareTriggers: any[];
    expandedFocusIds: Set<string>;
    toggleExpandedFocus: (id: string) => void;
    awareCalendarDate: Date;
    awareCalendarMode: any;
    setAwareCalendarDate: React.Dispatch<React.SetStateAction<Date>>;
    setAwareCalendarMode: React.Dispatch<React.SetStateAction<any>>;
    awareView: any;
    setAwareView: React.Dispatch<React.SetStateAction<any>>;
    showAllSideActive: boolean;
    setShowAllSideActive: React.Dispatch<React.SetStateAction<boolean>>;
    showAllSideSystem: boolean;
    setShowAllSideSystem: React.Dispatch<React.SetStateAction<boolean>>;
    showCompletedFocus: boolean;
    setShowCompletedFocus: React.Dispatch<React.SetStateAction<boolean>>;
    showAllSideCompleted: boolean;
    setShowAllSideCompleted: React.Dispatch<React.SetStateAction<boolean>>;
    reflectionSessions: any[];
    expandedReflection: string | null;
    setExpandedReflection: React.Dispatch<React.SetStateAction<string | null>>;
    reflectionMessages: Record<string, any[]>;
    loadReflectionMessages: (conversationId: string) => Promise<any>;
    agent: any;
    openSubagentSession: (run: any) => void;
    unavailableAttachmentKeys: Set<string>;
    handleAttachmentDownload: (path: string, name: string) => Promise<void>;
    markAttachmentUnavailable: (key: string) => void;
    setChatImagePreview: React.Dispatch<React.SetStateAction<any>>;
    upsertToolCallMessage: (message: any) => void;
    workspacePreviewLocked: boolean;
    handleWorkspaceSelectPath: (path: string) => void;
    handleWorkspaceToggleLock: () => void;
    handleWorkspaceEditingChange: (editing: boolean) => void;
    handleWorkspacePathDeleted: (path: string) => void;
    canManage: boolean;
    wsSessionId: string | null;
    setLiveState: React.Dispatch<React.SetStateAction<any>>;
    isViewingOtherUsersSessions: boolean;
    historyContainerRef: React.RefObject<HTMLDivElement | null>;
    handleHistoryScroll: (event: React.UIEvent<HTMLDivElement>) => void;
    historyAutoFollowInteractionProps: any;
    historyLoadingMore: boolean;
    historyHasMore: boolean;
    historyMsgs: any[];
    activeSessionExecution: any;
    readonlyGenerationActive: boolean;
    sessionTurnRuntimeRef: React.MutableRefObject<Record<string, any>>;
    currentUser: any;
    pcResumeMeasurementKey: any;
    showHistoryScrollBtn: boolean;
    scrollHistoryToBottom: () => void;
    isWritableSession: (session: any) => boolean;
    generationActive: boolean;
    chatDropProps: any;
    isChatDragging: boolean;
    showNoModelState: boolean;
    renderNoModelGuide: (variant?: 'empty' | 'floating') => React.ReactNode;
    chatContainerRef: React.RefObject<HTMLDivElement | null>;
    handleChatScroll: (event: React.UIEvent<HTMLDivElement>) => void;
    liveAutoFollowInteractionProps: any;
    chatMessages: any[];
    showScrollBtn: boolean;
    chatScrollBtnBottom: number;
    scrollToBottom: () => void;
    chatInfoMsg: string | null;
    setChatInfoMsg: React.Dispatch<React.SetStateAction<string | null>>;
    agentExpired: boolean;
    wsConnected: boolean;
    sessionUserIdStr: (session: any) => string | null;
    viewerUserIdStr: () => string | null;
    chatInputAreaRef: React.RefObject<HTMLDivElement | null>;
    chatUploadDrafts: any[];
    attachedFiles: any[];
    attachedImagePreviews: any[];
    dismissedWorkspaceRefPath: React.MutableRefObject<string | null>;
    setAttachedFiles: React.Dispatch<React.SetStateAction<any[]>>;
    chatInputRef: React.RefObject<HTMLTextAreaElement | null>;
    confirmationPending: boolean;
    chatInput: string;
    setChatInput: React.Dispatch<React.SetStateAction<string>>;
    isWaiting: boolean;
    isStreaming: boolean;
    isStopping: boolean;
    setIsWaiting: React.Dispatch<React.SetStateAction<boolean>>;
    setIsStreaming: React.Dispatch<React.SetStateAction<boolean>>;
    setIsStopping: React.Dispatch<React.SetStateAction<boolean>>;
    sendChatMsg: () => void;
    handlePaste: (event: React.ClipboardEvent<HTMLTextAreaElement>) => void;
    fileInputRef: React.RefObject<HTMLInputElement | null>;
    handleChatFile: (event: React.ChangeEvent<HTMLInputElement>) => void;
    overrideModelId: string | null;
    handleModelChange: (newModelId: string | null) => void;
    reasoningEffortOverride: string;
    setReasoningEffortOverride: React.Dispatch<React.SetStateAction<string>>;
    effectiveChatModelId: string | null;
    llmModels: any[];
    myTenant: any;
    chatUploadAbortRef: React.MutableRefObject<Map<string, () => void>>;
    wsMapRef: React.MutableRefObject<Record<string, WebSocket>>;
    setSessionUiState: (runtimeKey: string, next: any) => void;
};

export default function ChatTabContent(props: Props) {
    const {
        activeTab,
        livePanelVisible,
        sessionListCollapsed,
        setSessionListCollapsed,
        canViewAllAgentChatSessions,
        scopeDropdownRef,
        scopeDropdownOpen,
        setScopeDropdownOpen,
        chatScope,
        setChatScope,
        sessionSearch,
        setSessionSearch,
        onAdminTabMine,
        onAdminTabOthers,
        t,
        createNewSession,
        id,
        sessions,
        sessionsHasMore,
        sessionsLoading,
        sessionsLoadingMore,
        fetchMySessions,
        activeSession,
        selectSession,
        i18n,
        deleteSession,
        othersListForPicker,
        allSessionsHasMore,
        allSessionsLoading,
        allSessionsLoadingMore,
        fetchAllSessions,
        liveState,
        workspaceActivePath,
        workspaceActivities,
        workspaceLiveDraft,
        setLivePanelVisible,
        sidePanelTab,
        setSidePanelTab,
        focusRecords,
        awareTriggers,
        expandedFocusIds,
        toggleExpandedFocus,
        awareCalendarDate,
        awareCalendarMode,
        setAwareCalendarDate,
        setAwareCalendarMode,
        awareView,
        setAwareView,
        showAllSideActive,
        setShowAllSideActive,
        showAllSideSystem,
        setShowAllSideSystem,
        showCompletedFocus,
        setShowCompletedFocus,
        showAllSideCompleted,
        setShowAllSideCompleted,
        reflectionSessions,
        expandedReflection,
        setExpandedReflection,
        reflectionMessages,
        loadReflectionMessages,
        agent,
        openSubagentSession,
        unavailableAttachmentKeys,
        handleAttachmentDownload,
        markAttachmentUnavailable,
        setChatImagePreview,
        upsertToolCallMessage,
        workspacePreviewLocked,
        handleWorkspaceSelectPath,
        handleWorkspaceToggleLock,
        handleWorkspaceEditingChange,
        handleWorkspacePathDeleted,
        canManage,
        wsSessionId,
        setLiveState,
        isViewingOtherUsersSessions,
        historyContainerRef,
        handleHistoryScroll,
        historyAutoFollowInteractionProps,
        historyLoadingMore,
        historyHasMore,
        historyMsgs,
        activeSessionExecution,
        readonlyGenerationActive,
        sessionTurnRuntimeRef,
        currentUser,
        pcResumeMeasurementKey,
        showHistoryScrollBtn,
        scrollHistoryToBottom,
        isWritableSession,
        generationActive,
        chatDropProps,
        isChatDragging,
        showNoModelState,
        renderNoModelGuide,
        chatContainerRef,
        handleChatScroll,
        liveAutoFollowInteractionProps,
        chatMessages,
        showScrollBtn,
        chatScrollBtnBottom,
        scrollToBottom,
        chatInfoMsg,
        setChatInfoMsg,
        agentExpired,
        wsConnected,
        sessionUserIdStr,
        viewerUserIdStr,
        chatInputAreaRef,
        chatUploadDrafts,
        attachedFiles,
        attachedImagePreviews,
        dismissedWorkspaceRefPath,
        setAttachedFiles,
        chatInputRef,
        confirmationPending,
        chatInput,
        setChatInput,
        isWaiting,
        isStreaming,
        isStopping,
        setIsWaiting,
        setIsStreaming,
        setIsStopping,
        sendChatMsg,
        handlePaste,
        fileInputRef,
        handleChatFile,
        overrideModelId,
        handleModelChange,
        reasoningEffortOverride,
        setReasoningEffortOverride,
        effectiveChatModelId,
        llmModels,
        myTenant,
        wsMapRef,
        setSessionUiState,
    } = props;
    const buildSessionRuntimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;

    return activeTab === 'chat' ? (
        <div className="agent-chat-shell" style={{ display: 'flex', gap: 0, flex: 1, minHeight: 0, height: 'calc(100vh - 100px)', margin: '0 8px 8px', border: '1px solid rgba(0, 0, 0, 0.06)', borderRadius: '12px', overflow: 'hidden', boxShadow: '0 2px 8px rgba(0, 0, 0, 0.04)' }}>
            <div className={`session-sidebar ${sessionListCollapsed ? 'collapsed' : ''}`} style={{ width: sessionListCollapsed ? '0px' : '220px', transition: 'width 0.2s ease', flexShrink: 0, minHeight: 0, borderRight: sessionListCollapsed ? 'none' : '1px solid var(--border-subtle)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                <div style={{ flexShrink: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '4px', padding: '10px 8px 8px 12px', minHeight: '40px', boxSizing: 'border-box' }}>
                        {canViewAllAgentChatSessions ? (
                            <div className="scope-dropdown" ref={scopeDropdownRef}>
                                <button className="scope-dropdown-trigger" onClick={() => setScopeDropdownOpen((open) => !open)}>
                                    <span className="scope-dropdown-label">{chatScope === 'mine' ? t('agent.chat.mySessions') : t('agent.chat.otherSessions', '其他会话')}</span>
                                    <svg className={`scope-dropdown-chevron${scopeDropdownOpen ? ' scope-dropdown-chevron--open' : ''}`} width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                                </button>
                                {scopeDropdownOpen && (
                                    <div className="scope-dropdown-menu">
                                        <div className={`scope-dropdown-item${chatScope === 'mine' ? ' scope-dropdown-item--active' : ''}`} onClick={() => { onAdminTabMine(); setScopeDropdownOpen(false); }}>{t('agent.chat.mySessions')}</div>
                                        <div className={`scope-dropdown-item${chatScope === 'all' ? ' scope-dropdown-item--active' : ''}`} onClick={() => { onAdminTabOthers(); setScopeDropdownOpen(false); }}>{t('agent.chat.otherSessions', '其他会话')}</div>
                                    </div>
                                )}
                            </div>
                        ) : (
                            <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)', lineHeight: '1.25', flex: 1, minWidth: 0 }}>{t('agent.chat.mySessions')}</span>
                        )}
                        {!sessionListCollapsed && <button type="button" onClick={() => setSessionListCollapsed(true)} className="session-sidebar-toggle-btn" title={t('agent.chat.collapseSidebar')}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg></button>}
                    </div>
                    {(!canViewAllAgentChatSessions || chatScope === 'mine') && <div style={{ padding: '0 12px 8px' }}><button type="button" onClick={createNewSession} className="new-session-btn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden style={{ display: 'block', flexShrink: 0 }}><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg><span>{t('agent.chat.newSession')}</span></button></div>}
                    <div style={{ padding: '0 12px 8px' }}><SearchInput value={sessionSearch} onChange={(event) => setSessionSearch(event.target.value)} maxLength={100} placeholder={t('agent.chat.searchPlaceholder')} aria-label={t('agent.chat.searchPlaceholder')} /></div>
                </div>
                <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                    {(!canViewAllAgentChatSessions || chatScope === 'mine') ? (
                        <VirtualSessionList
                            key={`${id || 'unknown'}:mine`}
                            items={sessions}
                            hasMore={sessionsHasMore}
                            initialLoading={sessionsLoading}
                            loadingMore={sessionsLoadingMore}
                            estimateSize={59}
                            loadingState={<div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>}
                            emptyState={<div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>{sessionSearch.trim() ? t('agent.chat.noSearchResults') : <>{t('agent.chat.noSessionsYet')}<br />{t('agent.chat.clickToStart')}</>}</div>}
                            loadMoreLabel={t('common.loading')}
                            onLoadMore={() => fetchMySessions(true, id, true)}
                            renderItem={(s: any) => {
                                const isActive = activeSession?.id === s.id && (chatScope === 'mine' || !canViewAllAgentChatSessions);
                                const channelLabel: Record<string, string> = {
                                    feishu: t('common.channels.feishu'),
                                    discord: t('common.channels.discord'),
                                    slack: t('common.channels.slack'),
                                    wechat: t('common.channels.wechat'),
                                    dingtalk: t('common.channels.dingtalk'),
                                    wecom: t('common.channels.wecom'),
                                };
                                const chLabel = channelLabel[s.source_channel];
                                return (
                                    <div key={s.id} onClick={() => { setChatScope('mine'); selectSession(s, 'mine'); }} className="session-item" style={{ padding: '8px 12px', cursor: 'pointer', borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent', background: isActive ? 'var(--bg-secondary)' : 'transparent', marginBottom: '1px', display: 'flex', alignItems: 'center', gap: '4px' }} onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = 'var(--bg-secondary)'; }} onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = 'transparent'; }}>
                                        <div style={{ flex: 1, minWidth: 0 }}>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '2px' }}>
                                                <div style={{ fontSize: '12px', fontWeight: isActive ? 600 : 400, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>{s.title}</div>
                                                {s.is_primary && <span style={{ fontSize: '9px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', flexShrink: 0, border: '1px solid var(--border-subtle)' }}>{i18n.language === 'zh' ? '主会话' : 'Primary'}</span>}
                                                {s.unread_count > 0 && <span style={{ minWidth: s.unread_count > 9 ? '18px' : '14px', height: s.unread_count > 9 ? '18px' : '14px', padding: s.unread_count > 9 ? '0 4px' : '0', borderRadius: '999px', background: 'var(--text-primary)', color: 'var(--bg-primary)', fontSize: '10px', fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{s.unread_count > 99 ? '99+' : s.unread_count}</span>}
                                                {chLabel && <span style={{ fontSize: '9px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-tertiary)', color: 'var(--text-tertiary)', flexShrink: 0 }}>{chLabel}</span>}
                                            </div>
                                            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                                                {s.last_message_at ? new Date(s.last_message_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : new Date(s.created_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric' })}
                                                {s.message_count > 0 && <span className="session-msg-count" style={{ marginLeft: 'auto' }}>{s.message_count}</span>}
                                            </div>
                                        </div>
                                        <button className="session-del-btn" onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }} title={t('chat.deleteSession', 'Delete session')}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg></button>
                                    </div>
                                );
                            }}
                        />
                    ) : (
                        <VirtualSessionList
                            key={`${id || 'unknown'}:all`}
                            items={othersListForPicker}
                            hasMore={allSessionsHasMore}
                            initialLoading={allSessionsLoading}
                            loadingMore={allSessionsLoadingMore}
                            estimateSize={48}
                            loadingState={<div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: '4px' }}>{[...Array(3)].map((_, i) => <div key={i} style={{ padding: '6px 0', animation: 'pulse 1.5s ease-in-out infinite', animationDelay: `${i * 0.1}s` }}><div style={{ height: '12px', width: `${70 + (i % 3) * 10}%`, background: 'var(--bg-tertiary)', borderRadius: '4px', marginBottom: '6px' }} /><div style={{ height: '10px', width: `${40 + (i % 4) * 8}%`, background: 'var(--bg-tertiary)', borderRadius: '3px', opacity: 0.6 }} /></div>)}</div>}
                            emptyState={<div style={{ padding: '16px 12px', fontSize: '12px', color: 'var(--text-tertiary)', textAlign: 'center' }}>{sessionSearch.trim() ? t('agent.chat.noSearchResults') : t('agent.chat.noSessionsYet')}</div>}
                            loadMoreLabel={t('common.loading')}
                            onLoadMore={() => fetchAllSessions(true, true, id)}
                            renderItem={(s: any) => {
                                const isActive = activeSession?.id === s.id && chatScope === 'all';
                                const channelLabel: Record<string, string> = {
                                    feishu: t('common.channels.feishu'),
                                    discord: t('common.channels.discord'),
                                    slack: t('common.channels.slack'),
                                    wechat: t('common.channels.wechat'),
                                    dingtalk: t('common.channels.dingtalk'),
                                    wecom: t('common.channels.wecom'),
                                };
                                const chLabel = channelLabel[s.source_channel];
                                return (
                                    <div key={s.id} onClick={() => selectSession(s, 'all')} className="session-item" style={{ padding: '6px 12px', cursor: 'pointer', borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent', background: isActive ? 'var(--bg-secondary)' : 'transparent', position: 'relative' }} onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = 'var(--bg-secondary)'; }} onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = 'transparent'; }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '1px' }}>
                                            <div style={{ fontSize: '11px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-primary)', flex: 1 }}>{s.title}</div>
                                            {s.is_primary && <span style={{ fontSize: '9px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', flexShrink: 0, border: '1px solid var(--border-subtle)' }}>{i18n.language === 'zh' ? '主会话' : 'Primary'}</span>}
                                            {s.unread_count > 0 && <span style={{ minWidth: s.unread_count > 9 ? '18px' : '14px', height: s.unread_count > 9 ? '18px' : '14px', padding: s.unread_count > 9 ? '0 4px' : '0', borderRadius: '999px', background: 'var(--text-primary)', color: 'var(--bg-primary)', fontSize: '10px', fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{s.unread_count > 99 ? '99+' : s.unread_count}</span>}
                                            {chLabel && <span style={{ fontSize: '9px', padding: '1px 4px', borderRadius: '3px', background: 'var(--bg-tertiary)', color: 'var(--text-tertiary)', flexShrink: 0 }}>{chLabel}</span>}
                                        </div>
                                        <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', display: 'flex', gap: '4px' }}>
                                            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{s.username || ''}</span>
                                            <span style={{ flexShrink: 0 }}>{s.last_message_at ? new Date(s.last_message_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}{s.message_count > 0 ? ` · ${s.message_count}` : ''}</span>
                                        </div>
                                    </div>
                                );
                            }}
                        />
                    )}
                </div>
            </div>

            <div className={`agent-chat-area ${livePanelVisible ? 'has-live-panel' : ''}`} style={{ flex: 1, display: 'flex', flexDirection: 'row', position: 'relative', minWidth: 0, overflow: 'hidden' }}>
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative', minWidth: 0, overflow: 'hidden' }}>
                    {sessionListCollapsed && <button onClick={() => setSessionListCollapsed(false)} className="session-sidebar-toggle-btn session-sidebar-toggle-btn--floating" title="Show chat sessions"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg></button>}
                    {!activeSession ? (
                        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-tertiary)', fontSize: '13px', flexDirection: 'column', gap: '8px' }}>
                            <div>{t('agent.chat.noSessionSelected')}</div>
                            {!isViewingOtherUsersSessions && <button className="btn btn-secondary" onClick={createNewSession} style={{ fontSize: '12px' }}>{t('agent.chat.startNewSession')}</button>}
                        </div>
                    ) : !isWritableSession(activeSession) ? (
                        <>
                            <div style={{ position: 'absolute', top: '12px', left: sessionListCollapsed ? '52px' : '16px', zIndex: 10, fontSize: '11px', color: 'var(--text-tertiary)', padding: '4px 8px', background: 'var(--bg-secondary)', borderRadius: '4px', pointerEvents: 'none' }}>
                                {activeSession.source_channel === 'agent' ? <><IconRobot size={13} stroke={1.8} /> Agent Conversation · {activeSession.username || 'Agents'}</> : activeSession.source_channel === 'subagent' ? <><IconRobot size={13} stroke={1.8} /> Read-only · Subagent</> : <>Read-only · {activeSession.username || 'User'}</>}
                            </div>
                            <div ref={historyContainerRef} data-conversation-scroller="web-history" tabIndex={0} aria-label={i18n.language?.startsWith('zh') ? '只读会话消息' : 'Read-only conversation messages'} onScroll={handleHistoryScroll} {...historyAutoFollowInteractionProps} style={{ flex: 1, overflowY: 'auto', padding: '48px 16px 12px' }}>
                                {historyLoadingMore && <div style={{ textAlign: 'center', padding: '12px', color: 'var(--text-tertiary)', fontSize: '13px' }}>Loading more messages...</div>}
                                {!historyHasMore && historyMsgs.length > 0 && <div style={{ textAlign: 'center', padding: '12px', color: 'var(--text-tertiary)', fontSize: '13px' }}>All messages loaded</div>}
                                {(() => {
                                    const isA2A = activeSession.source_channel === 'agent' || activeSession.participant_type === 'agent';
                                    const isGroupChat = !isA2A && !!activeSession.is_group;
                                    const isHumanReadonly = !isA2A && !activeSession.is_group;
                                    const viewerId = currentUser?.id != null ? String(currentUser.id) : null;
                                    return (
                                        <ConversationTimeline
                                            agentId={id!}
                                            agentName={agent?.name || 'Agent'}
                                            messages={historyMsgs as any}
                                            scrollerRef={historyContainerRef}
                                            resumeMeasurementKey={pcResumeMeasurementKey}
                                            provenance={activeSessionExecution}
                                            isRunning={readonlyGenerationActive}
                                            progressMessage={{ id: `conversation-turn-progress:${activeSession?.id || 'history'}:${sessionTurnRuntimeRef.current[`${id}:${activeSession?.id}`]?.snapshot.generation || 0}` }}
                                            unavailableAttachmentKeys={unavailableAttachmentKeys}
                                            onAttachmentDownload={handleAttachmentDownload}
                                            onAttachmentUnavailable={markAttachmentUnavailable}
                                            onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                            onOpenSubagentSession={openSubagentSession}
                                            onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                            viewOf={(m: any) => {
                                                let isLeft: boolean;
                                                if (isA2A) {
                                                    isLeft = isA2AMessageLeft(m);
                                                } else if (isGroupChat) {
                                                    if (m.role === 'assistant') {
                                                        isLeft = true;
                                                    } else {
                                                        isLeft = !(viewerId && m.sender_user_id && m.sender_user_id === viewerId);
                                                    }
                                                } else {
                                                    isLeft = m.role === 'assistant';
                                                }
                                                const senderLabel = isHumanReadonly
                                                    ? (isLeft ? (agent?.name || 'Agent') : (activeSession.username || 'User'))
                                                    : isGroupChat
                                                        ? (m.role === 'assistant' ? (agent?.name || 'Agent') : (m.sender_name || 'User'))
                                                        : undefined;
                                                const avatarText = isHumanReadonly
                                                    ? (isLeft ? ((agent?.name || 'Agent')[0]) : ((activeSession.username || 'User')[0]))
                                                    : isGroupChat
                                                        ? (m.role === 'assistant' ? (((agent?.name || 'Agent')[0]) || 'A') : ((m.sender_name && m.sender_name[0]) || 'U'))
                                                        : undefined;
                                                const avatarUrl = m.sender_avatar_url
                                                    || (m.sender_user_id === viewerId ? currentUser?.avatar_url : undefined)
                                                    || (m.sender_agent_id === id ? agent?.avatar_url : undefined);
                                                return { isLeft, senderLabel, avatarText, avatarUrl, forceSenderLabel: isHumanReadonly || isGroupChat };
                                            }}
                                        />
                                    );
                                })()}
                            </div>
                            {showHistoryScrollBtn && <ConversationScrollToBottomButton variant="web" onClick={scrollHistoryToBottom} label={i18n.language?.startsWith('zh') ? '滚动到底' : 'Scroll to bottom'} />}
                        </>
                    ) : (
                        <div {...chatDropProps} style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative', minHeight: 0, overflow: 'hidden' }}>
                            {isChatDragging && <div className="drop-zone-overlay"><div className="drop-zone-overlay__icon"><IconPaperclip size={28} stroke={1.8} /></div><div className="drop-zone-overlay__text">{t('agent.upload.dropToAttach', 'Drop files to attach (max 10)')}</div></div>}
                            {showNoModelState && renderNoModelGuide('floating')}
                            <div ref={chatContainerRef} data-conversation-scroller="web-live" tabIndex={0} aria-label={i18n.language?.startsWith('zh') ? '会话消息' : 'Conversation messages'} onScroll={handleChatScroll} {...liveAutoFollowInteractionProps} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
                                {chatMessages.length === 0 && !showNoModelState && <div className="chat-empty-state"><div className="chat-empty-state__title">{activeSession?.title || t('agent.chat.startChat')}</div><div className="chat-empty-state__subtitle">{t('agent.chat.startConversation', { name: agent.name })}</div><div className="chat-empty-state__hint">{t('agent.chat.fileSupport')}</div></div>}
                                {(() => {
                                    const visibleChatMessages = showNoModelState
                                        ? chatMessages.filter((msg: any) => {
                                            const content = String(msg?.content || msg?.message || '');
                                            return !(msg?.role === 'assistant' && (content.includes('no LLM model') || content.includes('No model')));
                                        })
                                        : chatMessages;
                                    return (
                                        <ConversationTimeline
                                            agentId={id!}
                                            agentName={agent?.name || 'Agent'}
                                            messages={visibleChatMessages as any}
                                            scrollerRef={chatContainerRef}
                                            resumeMeasurementKey={pcResumeMeasurementKey}
                                            provenance={activeSessionExecution}
                                            isRunning={generationActive}
                                            progressMessage={{ id: `conversation-turn-progress:${activeSession?.id || 'new'}:${sessionTurnRuntimeRef.current[`${id}:${activeSession?.id}`]?.snapshot.generation || 0}` }}
                                            unavailableAttachmentKeys={unavailableAttachmentKeys}
                                            onAttachmentDownload={handleAttachmentDownload}
                                            onAttachmentUnavailable={markAttachmentUnavailable}
                                            onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                            onOpenSubagentSession={openSubagentSession}
                                            onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                            viewOf={(m: any) => ({
                                                isLeft: m.role === 'assistant',
                                                senderLabel: m.role === 'assistant' ? (agent?.name || 'Agent') : (currentUser?.display_name || undefined),
                                                avatarText: m.role === 'assistant' ? ((agent?.name || 'Agent')[0]) : (currentUser?.display_name?.[0] || undefined),
                                                avatarUrl: m.sender_avatar_url || (m.role === 'assistant' ? agent?.avatar_url : currentUser?.avatar_url),
                                            })}
                                        />
                                    );
                                })()}
                            </div>
                            {showScrollBtn && <ConversationScrollToBottomButton variant="web" bottom={chatScrollBtnBottom} onClick={scrollToBottom} label={i18n.language?.startsWith('zh') ? '滚动到底' : 'Scroll to bottom'} />}
                            {chatInfoMsg && <div style={{ padding: '6px 14px', borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--text-secondary)', animation: 'fadeIn 0.2s ease' }}><span style={{ opacity: 0.7 }}>ℹ️</span><span style={{ flex: 1 }}>{chatInfoMsg}</span><button onClick={() => setChatInfoMsg(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '14px', lineHeight: 1, padding: '0 2px' }}>✕</button></div>}
                            {chatInfoMsg && <div style={{ padding: '6px 14px', borderTop: '1px solid rgba(99,102,241,0.25)', background: 'rgba(99,102,241,0.07)', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--text-secondary)', animation: 'fadeIn 0.2s ease' }}><span style={{ opacity: 0.7 }}>ℹ️</span><span style={{ flex: 1 }}>{chatInfoMsg}</span><button onClick={() => setChatInfoMsg(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '14px', lineHeight: 1, padding: '0 2px' }}>✕</button></div>}
                            {agentExpired ? (
                                <div style={{ padding: '7px 16px', borderTop: '1px solid rgba(245,158,11,0.3)', background: 'rgba(245,158,11,0.08)', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'rgb(180,100,0)' }}><span>⏸</span><span>This Agent has <strong>expired</strong> and is off duty. Contact your admin to extend its service.</span></div>
                            ) : !wsConnected && !!currentUser && sessionUserIdStr(activeSession) === viewerUserIdStr() ? (
                                <div style={{ padding: '3px 16px', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}><span style={{ display: 'inline-block', width: '5px', height: '5px', borderRadius: '50%', background: 'var(--accent-primary)', opacity: 0.8, animation: 'pulse 1.2s ease-in-out infinite' }} />Connecting...</div>
                            ) : null}
                            <div ref={chatInputAreaRef} className="chat-input-area" style={{ flexShrink: 0 }}>
                                <div className="chat-composer">
                                    {(chatUploadDrafts.length > 0 || attachedFiles.length > 0) && (
                                        <div className="chat-composer-attachments">
                                            {chatUploadDrafts.map((draft) => (
                                                <div key={draft.id} className="chat-file-pill">
                                                    <div className="chat-file-pill__fill" style={{ width: `${draft.percent}%` }} />
                                                    <div className="chat-file-pill__row">
                                                        {draft.previewUrl ? <img className="chat-file-pill__thumb" src={draft.previewUrl} alt="" /> : <span className="chat-file-pill__icon"><ChatAttachmentIcon name={draft.name} size={16} /></span>}
                                                        <span className="chat-file-pill__name">{draft.name}</span>
                                                        <span className="chat-file-pill__size">{formatFileSize(draft.sizeBytes)}</span>
                                                        <span className="chat-file-pill__pct">{draft.percent}%</span>
                                                        <button type="button" className="chat-file-pill__remove" onClick={() => { props.chatUploadAbortRef.current.get(draft.id)?.(); }} title="Cancel upload">×</button>
                                                    </div>
                                                </div>
                                            ))}
                                            {attachedFiles.map((file, idx) => {
                                                const imageIndex = attachedImagePreviews.findIndex((image: any) => image.src === file.imageUrl);
                                                return (
                                                    <div key={`a-${idx}-${file.name}`} className={`chat-file-pill ${file.source === 'workspace_auto' ? 'chat-file-pill--workspace' : ''}`} title={file.path || file.name}>
                                                        <div className="chat-file-pill__row">
                                                            {file.imageUrl ? (
                                                                <button type="button" className="chat-file-pill__thumb-button" onClick={() => { if (imageIndex >= 0) setChatImagePreview({ images: attachedImagePreviews, index: imageIndex }); }} title={t('common.preview', 'Preview')}>
                                                                    <img className="chat-file-pill__thumb" src={file.imageUrl} alt="" />
                                                                </button>
                                                            ) : (
                                                                <span className="chat-file-pill__icon"><ChatAttachmentIcon name={file.name} mimeType={file.mimeType} size={16} /></span>
                                                            )}
                                                            <span className="chat-file-pill__name">{file.name}</span>
                                                            {file.source === 'workspace_auto' && <span className="chat-file-pill__source">Workspace</span>}
                                                            <button type="button" className="chat-file-pill__remove" onClick={() => { if (file.source === 'workspace_auto' && file.path) dismissedWorkspaceRefPath.current = file.path; setAttachedFiles((prev) => prev.filter((_, i) => i !== idx)); }} title="Remove file">×</button>
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    )}
                                    <div className="chat-composer-input-block">
                                        <textarea ref={chatInputRef} className="chat-input" disabled={showNoModelState || confirmationPending} value={chatInput} onChange={(e) => { setChatInput(e.target.value); const el = e.target; el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; }} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !isStopping) { e.preventDefault(); sendChatMsg(); } }} onPaste={handlePaste} placeholder={confirmationPending ? '请先完成上方确认' : showNoModelState ? t('agent.chat.noModelPlaceholder', 'Configure a company model to start chatting') : (!wsConnected && !!currentUser && sessionUserIdStr(activeSession) === viewerUserIdStr() ? 'Connecting...' : t('chat.placeholder'))} rows={1} />
                                    </div>
                                    <div className="chat-composer-toolbar">
                                        <input type="file" multiple ref={fileInputRef} onChange={handleChatFile} style={{ display: 'none' }} />
                                        <button type="button" className="chat-composer-btn" onClick={() => fileInputRef.current?.click()} disabled={showNoModelState || confirmationPending || !wsConnected || chatUploadDrafts.length > 0 || isStopping || attachedFiles.length >= 10} title={t('agent.workspace.uploadFile')}><IconPaperclip size={16} stroke={1.75} /></button>
                                        <ModelSwitcher value={overrideModelId} onChange={handleModelChange} tenantDefaultId={myTenant?.default_model_id || null} disabled={showNoModelState || !wsConnected} />
                                        <ReasoningEffortSelect
                                            value={reasoningEffortOverride as ReasoningEffortValue}
                                            onChange={setReasoningEffortOverride}
                                            supportedEfforts={llmModels.find((model: any) => model.id === effectiveChatModelId)?.reasoning_efforts}
                                            compact
                                            disabled={showNoModelState || !wsConnected}
                                        />
                                        <div style={{ flex: 1 }} />
                                        {(isStreaming || isWaiting || isStopping) ? (
                                            <button
                                                type="button"
                                                className="btn btn-stop-generation"
                                                onClick={() => {
                                                    if (!id || !activeSession?.id) return;
                                                    const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
                                                    const activeSocket = wsMapRef.current[activeRuntimeKey];
                                                    if (activeSocket?.readyState === WebSocket.OPEN) {
                                                        const snapshot = (sessionTurnRuntimeRef.current[activeRuntimeKey] || IDLE_CONVERSATION_TURN).snapshot;
                                                        activeSocket.send(JSON.stringify({ type: 'abort', turn_anchor_id: snapshot.turnAnchorId, generation: snapshot.generation }));
                                                        setIsStopping(true);
                                                        props.setSessionUiState(activeRuntimeKey, { isStopping: true });
                                                    } else {
                                                        setIsStreaming(false);
                                                        setIsWaiting(false);
                                                        setIsStopping(false);
                                                        props.setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false, isStopping: false });
                                                    }
                                                }}
                                                title={t('chat.stop', 'Stop')}
                                            >
                                                <span className="stop-icon" />
                                            </button>
                                        ) : null}
                                        {(!agent || agent.agent_type !== "openclaw" || (!isWaiting && !isStreaming)) && (
                                            <button type="button" className="btn btn-primary chat-composer-send" onClick={sendChatMsg} disabled={showNoModelState || confirmationPending || isStopping || !wsConnected || (!chatInput.trim() && attachedFiles.length === 0)} title={t('chat.send')}><IconSend size={16} stroke={1.75} /></button>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </div>
                    )}
                </div>
                <AgentSidePanel
                    liveState={liveState}
                    workspaceActivePath={workspaceActivePath}
                    workspaceActivities={workspaceActivities}
                    workspaceLiveDraft={workspaceLiveDraft}
                    visible={livePanelVisible}
                    onToggle={() => setLivePanelVisible(false)}
                    activeTab={sidePanelTab}
                    onTabChange={setSidePanelTab}
                    awareContent={(
                        <AwarePreviewPanel {...(props as any)} />
                    )}
                    workspaceLocked={workspacePreviewLocked}
                    onWorkspaceSelectPath={handleWorkspaceSelectPath}
                    onWorkspaceToggleLock={handleWorkspaceToggleLock}
                    onWorkspaceEditingChange={handleWorkspaceEditingChange}
                    onWorkspacePathDeleted={handleWorkspacePathDeleted}
                    canManageWorkspace={canManage}
                    agentId={id}
                    sessionId={wsSessionId ?? undefined}
                    onLiveUpdate={(env, screenshotDataUri) => {
                        setLiveState((prev: any) => ({
                            ...prev,
                            [env]: { screenshotUrl: screenshotDataUri },
                        }));
                    }}
                />
            </div>
        </div>
    ) : null;
}
