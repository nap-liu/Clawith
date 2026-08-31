import { useEffect } from 'react';
import { chatSessionApi } from '../../../services/api';
import {
    CONVERSATION_HISTORY_RECONCILE_TURN_PAGE_SIZE,
    createConversationHistoryPageParams,
    resolveConversationHistoryHasMore,
} from '../../../features/conversation/historyPagination';
import {
    latestHistoryWindowOverlaps,
    normalizeChatTimelineMessages,
    reconcileLatestHistoryWindow,
} from '../../../features/conversation/core/chatTimeline';
import { prepareMessagesForActiveTurnResume } from '../../../features/conversation/core/resumeRecovery';
import { mergeSessionsById, SESSION_PAGE_SIZE } from '../shared';
import { parseAgentDetailChatMsg } from '../utils/chatMessageParsing';

export function useAgentDetailSessionSelection({
    id,
    activeTab,
    currentUser,
    requestedSessionId,
    skipNextSessionUrlRestoreRef,
    writeSessionIdToUrl,
    t,
    toast,
    chat,
    helpers,
}: any) {
    const {
        sessions,
        setSessions,
        allSessions,
        setAllSessions,
        sessionsHasMore,
        setSessionsHasMore,
        allSessionsHasMore,
        setAllSessionsHasMore,
        sessionsNextCursor,
        setSessionsNextCursor,
        allSessionsNextCursor,
        setAllSessionsNextCursor,
        activeSession,
        setActiveSession,
        setHistoryMsgs,
        setHistoryOldestTs,
        setHistoryHasMore,
        setHistoryLoadingMore,
        setSessionsLoading,
        setAllSessionsLoading,
        setSessionsLoadingMore,
        setAllSessionsLoadingMore,
        setAgentExpired,
        token,
        canViewAllAgentChatSessions,
        currentAgentIdRef,
        activeSessionIdRef,
        sessionMsgAbortRef,
        historyMoreAbortRef,
        sessionsListAbortRef,
        allSessionsListAbortRef,
        sessionsListGenerationRef,
        allSessionsListGenerationRef,
        sessionLoadSeqRef,
        historyMsgsSnapshotRef,
        chatMessagesSnapshotRef,
        sessionUiStateRef,
        setChatScope,
        setChatMessages,
        setWsConnected,
        setIsStreaming,
        setIsWaiting,
        setIsStopping,
        setOnboardingKickoffRequest,
        setWorkspaceLockedPath,
        setWorkspaceActivePath,
        setWorkspaceActivities,
        setWorkspaceLiveDraft,
        setLiveState,
        setSidePanelTab,
        normalizeChatSession,
        clearChatSelection,
        clearUnreadForSession,
        isWritableSession,
        buildSessionRuntimeKey,
        syncActiveSocketState,
        sessionUserIdStr,
        viewerUserIdStr,
    } = chat;
    const {
        scheduleComposerFocusRef,
        discardChatStreamBatchRef,
        discardMonitorStreamBatchRef,
        historyAutoLoadCursorRef,
        wsRef,
        onboardingRequestsRef,
        parseChatMsgRef,
    } = helpers;

    const fetchMySessions = async (silent = false, agentId: string | undefined = id, append = false) => {
        if (!agentId) return [];
        const existingCount = currentAgentIdRef.current === agentId ? sessions.length : 0;
        const refreshLimit = silent && !append ? Math.min(200, Math.max(SESSION_PAGE_SIZE, existingCount)) : SESSION_PAGE_SIZE;
        const requestCursor = append ? sessionsNextCursor : null;
        if (append && requestCursor == null) return [];
        const generation = append ? sessionsListGenerationRef.current : ++sessionsListGenerationRef.current;
        sessionsListAbortRef.current?.abort();
        const controller = new AbortController();
        sessionsListAbortRef.current = controller;
        if (append) setSessionsLoadingMore(true);
        else {
            setSessionsLoadingMore(false);
            if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(true);
        }
        try {
            const page = await chatSessionApi.listPage(agentId, { scope: 'mine', limit: refreshLimit, cursor: requestCursor || undefined, signal: controller.signal });
            const data = page.items.map((row: any) => normalizeChatSession(row));
            if (currentAgentIdRef.current === agentId && generation === sessionsListGenerationRef.current) {
                if (append) {
                    setSessions((prev: any[]) => mergeSessionsById(prev, data));
                    setSessionsHasMore(page.has_more);
                    setSessionsNextCursor(page.next_cursor);
                } else if (silent && existingCount > 200) {
                    setSessions((prev: any[]) => mergeSessionsById(data, prev));
                } else {
                    setSessions(data);
                    setSessionsHasMore(page.has_more);
                    setSessionsNextCursor(page.next_cursor);
                }
            }
            return data;
        } catch (error: any) {
            if (error?.name !== 'AbortError') console.warn('[chat] failed to load session list', error);
            return [];
        } finally {
            if (sessionsListAbortRef.current === controller) {
                sessionsListAbortRef.current = null;
                if (append) setSessionsLoadingMore(false);
                else if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(false);
            }
        }
    };

    const fetchAllSessions = async (silent = false, append = false, agentId: string | undefined = id) => {
        if (!agentId || !canViewAllAgentChatSessions) return [];
        const existingCount = currentAgentIdRef.current === agentId ? allSessions.length : 0;
        const refreshLimit = silent && !append ? Math.min(200, Math.max(SESSION_PAGE_SIZE, existingCount)) : SESSION_PAGE_SIZE;
        const requestCursor = append ? allSessionsNextCursor : null;
        if (append && requestCursor == null) return [];
        const generation = append ? allSessionsListGenerationRef.current : ++allSessionsListGenerationRef.current;
        allSessionsListAbortRef.current?.abort();
        const controller = new AbortController();
        allSessionsListAbortRef.current = controller;
        if (append) setAllSessionsLoadingMore(true);
        else {
            setAllSessionsLoadingMore(false);
            if (!silent) setAllSessionsLoading(true);
        }
        try {
            const page = await chatSessionApi.listPage(agentId, { scope: 'all', exclude_mine: true, limit: refreshLimit, cursor: requestCursor || undefined, signal: controller.signal });
            if (currentAgentIdRef.current !== agentId || generation !== allSessionsListGenerationRef.current) return [];
            const data = page.items.map((row: any) => normalizeChatSession(row));
            if (append) {
                setAllSessions((prev: any[]) => mergeSessionsById(prev, data));
                setAllSessionsHasMore(page.has_more);
                setAllSessionsNextCursor(page.next_cursor);
            } else if (silent && existingCount > 200) {
                setAllSessions((prev: any[]) => mergeSessionsById(data, prev));
            } else {
                setAllSessions(data);
                setAllSessionsHasMore(page.has_more);
                setAllSessionsNextCursor(page.next_cursor);
            }
            return data;
        } catch (error: any) {
            if (currentAgentIdRef.current === agentId && !silent && !append && error?.name !== 'AbortError') {
                setAllSessions([]);
                setAllSessionsHasMore(false);
                setAllSessionsNextCursor(null);
                if (error?.status === 403) console.warn('[chat] scope=all sessions forbidden (need org/platform/agent admin)');
            }
            return [];
        } finally {
            if (allSessionsListAbortRef.current === controller) {
                allSessionsListAbortRef.current = null;
                if (append) setAllSessionsLoadingMore(false);
                else if (!silent) setAllSessionsLoading(false);
            }
        }
    };

    const selectSession = async (rawSess: any, scopeOverride: 'mine' | 'all' = chat.chatScope, options: { preserveLoadedHistory?: boolean; prepareWebResume?: boolean } = {}) => {
        const sess = normalizeChatSession(rawSess);
        const targetAgentId = id;
        if (!targetAgentId) return false;
        const preserveLoadedHistory = Boolean(options.preserveLoadedHistory && String(activeSessionIdRef.current || '') === String(sess.id));
        if (!preserveLoadedHistory) {
            chat.pcRecoveryPollingNeededRef.current = false;
            chat.cancelPcRecoveryPolling();
        }
        discardChatStreamBatchRef.current();
        discardMonitorStreamBatchRef.current();
        const runtimeKey = buildSessionRuntimeKey(targetAgentId, String(sess.id));
        const runtimeState = sessionUiStateRef.current[runtimeKey] || { isWaiting: false, isStreaming: false, isStopping: false };
        const writable = isWritableSession(sess, scopeOverride);
        activeSessionIdRef.current = sess.id;
        if (!preserveLoadedHistory) {
            setChatMessages([]);
            setHistoryMsgs([]);
            setHistoryOldestTs(null);
            setHistoryHasMore(true);
            historyAutoLoadCursorRef.current = null;
        }
        setHistoryLoadingMore(false);
        setIsStreaming(runtimeState.isStreaming);
        setIsWaiting(runtimeState.isWaiting);
        setIsStopping(runtimeState.isStopping);
        setActiveSession(sess);
        writeSessionIdToUrl(String(sess.id));
        setAgentExpired(false);
        syncActiveSocketState(sess, targetAgentId);
        if (writable) scheduleComposerFocusRef.current();

        sessionMsgAbortRef.current?.abort();
        historyMoreAbortRef.current?.abort();
        historyMoreAbortRef.current = null;
        const controller = new AbortController();
        sessionMsgAbortRef.current = controller;
        const historyTimeout = window.setTimeout(() => controller.abort(), 10000);
        const loadSeq = ++sessionLoadSeqRef.current;
        try {
            const tkn = localStorage.getItem('token');
            const parseHistoryRows = (rows: any[]) => rows.map((m: any) => parseChatMsgRef.current({
                msg: {
                    role: m.role,
                    content: m.content || '',
                    ...(Object.prototype.hasOwnProperty.call(m, 'display_content') && { display_content: m.display_content || '' }),
                    ...(Object.prototype.hasOwnProperty.call(m, 'attachments') && { attachments: m.attachments || [] }),
                    ...(m.quoted_message && { quoted_message: m.quoted_message }),
                    ...(m.toolName && { toolName: m.toolName, toolArgs: m.toolArgs, toolStatus: m.toolStatus, toolResult: m.toolResult, toolThinking: m.toolThinking }),
                    ...(m.toolCallId && { toolCallId: m.toolCallId }),
                    ...(typeof m.toolCallIdExplicit === 'boolean' && { _toolCallIdExplicit: m.toolCallIdExplicit }),
                    ...((m.turnAnchorId || m.message_meta?.turn_anchor_id) && { turnAnchorId: m.turnAnchorId || m.message_meta?.turn_anchor_id }),
                    ...((m.turnGeneration ?? m.message_meta?.turn_generation) != null && { turnGeneration: m.turnGeneration ?? m.message_meta?.turn_generation }),
                    ...((m.producerScope || m.producer_scope || m.message_meta?.producer_scope) && { producerScope: m.producerScope || m.producer_scope || m.message_meta?.producer_scope }),
                    ...(m.thinking && { thinking: m.thinking }),
                    ...(m.created_at && { timestamp: m.created_at }),
                    ...(m.id && { id: m.id }),
                    ...(m.sender_name && { sender_name: m.sender_name }),
                    ...(m.sender_user_id && { sender_user_id: m.sender_user_id }),
                    ...(m.sender_agent_id && { sender_agent_id: m.sender_agent_id }),
                },
                id,
                activeSession,
            }));
            const currentLoadedMessages = writable ? chatMessagesSnapshotRef.current : historyMsgsSnapshotRef.current;
            let collectedRows: any[] = [];
            let responseCursor: string | null = null;
            let responseHasMore: string | null = null;
            let overlapFound = !preserveLoadedHistory || currentLoadedMessages.length === 0;
            let before: string | null = null;
            do {
                const params = createConversationHistoryPageParams(before, preserveLoadedHistory && !before ? CONVERSATION_HISTORY_RECONCILE_TURN_PAGE_SIZE : undefined);
                const res = await fetch(`/api/agents/${targetAgentId}/sessions/${sess.id}/message-turns?${params}`, {
                    headers: { Authorization: `Bearer ${tkn}` },
                    signal: controller.signal,
                });
                if (!res.ok) return false;
                const pageCursor = res.headers.get('X-Message-Next-Cursor');
                const pageHasMore = res.headers.get('X-Message-Has-More');
                const pageRows = await res.json();
                if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return false;
                if (currentAgentIdRef.current !== targetAgentId) return false;
                const normalizedPageRows = normalizeChatTimelineMessages(pageRows);
                collectedRows = before ? [...normalizedPageRows, ...collectedRows] : normalizedPageRows;
                responseCursor = pageCursor;
                responseHasMore = pageHasMore;
                if (preserveLoadedHistory && currentLoadedMessages.length > 0) {
                    overlapFound = latestHistoryWindowOverlaps(collectedRows, currentLoadedMessages);
                    if (!overlapFound && pageCursor) before = pageCursor;
                } else {
                    overlapFound = true;
                }
                if (!preserveLoadedHistory || overlapFound || !pageCursor) break;
            } while (before);

            const parsed = parseHistoryRows(collectedRows);
            const nextMessages = preserveLoadedHistory && currentLoadedMessages.length > 0
                ? reconcileLatestHistoryWindow(currentLoadedMessages as any[], parsed as any[])
                : parsed;
            const oldestTs = collectedRows.length > 0 ? String(collectedRows[0]?.created_at || collectedRows[0]?.timestamp || '') : null;
            const hasMore = Boolean(responseCursor && resolveConversationHistoryHasMore(responseHasMore));
            if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return false;
            if (currentAgentIdRef.current !== targetAgentId || String(activeSessionIdRef.current || '') !== String(sess.id)) return false;
            if (writable) {
                const prepared = options.prepareWebResume ? prepareMessagesForActiveTurnResume(nextMessages as any[]) : nextMessages;
                setChatMessages(prepared as any);
            } else {
                setHistoryMsgs(nextMessages as any);
            }
            setHistoryOldestTs(oldestTs);
            setHistoryHasMore(hasMore);
            clearUnreadForSession(sess.id);
            return true;
        } catch (error: any) {
            if (error?.name !== 'AbortError') console.warn('[chat] failed to select session', error);
            return false;
        } finally {
            window.clearTimeout(historyTimeout);
            if (sessionMsgAbortRef.current === controller) sessionMsgAbortRef.current = null;
        }
    };

    useEffect(() => {
        currentAgentIdRef.current = id;
    }, [id, currentAgentIdRef]);
    useEffect(() => {
        sessionMsgAbortRef.current?.abort();
        historyMoreAbortRef.current?.abort();
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setHistoryMsgs([]);
        setIsStreaming(false);
        setIsWaiting(false);
        setIsStopping(false);
        setWsConnected(false);
        wsRef.current = null;
        onboardingRequestsRef.current = {};
        setOnboardingKickoffRequest(null);
        setWorkspaceLockedPath(null);
        setWorkspaceActivePath(null);
        setWorkspaceActivities([]);
        setWorkspaceLiveDraft(null);
        setLiveState({});
        setSidePanelTab('workspace');
        setChatScope('mine');
        setSessions([]);
        setAllSessions([]);
        setSessionsHasMore(false);
        setAllSessionsHasMore(false);
        setSessionsNextCursor(null);
        setAllSessionsNextCursor(null);
        sessionsListAbortRef.current?.abort();
        allSessionsListAbortRef.current?.abort();
        sessionsListGenerationRef.current += 1;
        allSessionsListGenerationRef.current += 1;
        setSessionsLoadingMore(false);
        setAllSessionsLoadingMore(false);
        setAgentExpired(false);
    }, [id]);
    useEffect(() => {
        setSessions([]);
        setAllSessions([]);
        setSessionsHasMore(false);
        setAllSessionsHasMore(false);
        setSessionsNextCursor(null);
        setAllSessionsNextCursor(null);
        sessionsListAbortRef.current?.abort();
        allSessionsListAbortRef.current?.abort();
        sessionsListGenerationRef.current += 1;
        allSessionsListGenerationRef.current += 1;
        setChatScope('mine');
        sessionMsgAbortRef.current?.abort();
        historyMoreAbortRef.current?.abort();
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setHistoryMsgs([]);
        setWsConnected(false);
        setIsStreaming(false);
        setIsWaiting(false);
        setIsStopping(false);
        setSessionsLoading(false);
        setAllSessionsLoading(false);
        setSessionsLoadingMore(false);
        setAllSessionsLoadingMore(false);
        Object.keys(chat.reconnectDisabledRef.current).forEach((k) => { chat.reconnectDisabledRef.current[k] = true; });
        Object.keys(chat.wsMapRef.current).forEach((k) => {
            const ws = chat.wsMapRef.current[k];
            if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
        });
        chat.wsMapRef.current = {};
        wsRef.current = null;
        onboardingRequestsRef.current = {};
        setOnboardingKickoffRequest(null);
    }, [currentUser?.id, token]);
    useEffect(() => {
        if (!id || !token || activeTab !== 'chat') return;
        if (skipNextSessionUrlRestoreRef.current) {
            skipNextSessionUrlRestoreRef.current = false;
            return;
        }
        if (requestedSessionId && activeSessionIdRef.current === requestedSessionId) return;
        let cancelled = false;
        const restoreSessionFromUrl = async () => {
            const mySessions = await fetchMySessions(false, id);
            if (cancelled || currentAgentIdRef.current !== id) return;
            setSessionsLoading(false);
            if (requestedSessionId) {
                const listedSession = mySessions.find((session: any) => String(session.id) === requestedSessionId);
                if (listedSession) {
                    setChatScope('mine');
                    await selectSession(listedSession, 'mine');
                    return;
                }
                try {
                    const resolvedSession = normalizeChatSession(await chatSessionApi.get(id, requestedSessionId));
                    if (cancelled || currentAgentIdRef.current !== id) return;
                    const resolvedScope: 'mine' | 'all' = resolvedSession.view_scope === 'all' ? 'all' : 'mine';
                    setChatScope(resolvedScope);
                    const isSubagentSession = String(resolvedSession.source_channel || '').toLowerCase() === 'subagent';
                    if (!isSubagentSession && resolvedScope === 'mine') {
                        setSessions((prev: any[]) => prev.some((item: any) => String(item.id) === requestedSessionId) ? prev : [resolvedSession, ...prev]);
                    } else if (!isSubagentSession) {
                        setAllSessions((prev: any[]) => prev.some((item: any) => String(item.id) === requestedSessionId) ? prev : [resolvedSession, ...prev]);
                        void fetchAllSessions().then((rows) => {
                            if (cancelled || currentAgentIdRef.current !== id) return;
                            if (!rows.some((item: any) => String(item.id) === requestedSessionId)) {
                                setAllSessions((prev: any[]) => prev.some((item: any) => String(item.id) === requestedSessionId) ? prev : [resolvedSession, ...prev]);
                            }
                        });
                    }
                    await selectSession(resolvedSession, resolvedScope);
                    return;
                } catch (error: any) {
                    if (cancelled) return;
                    console.warn('[chat] unable to restore session from URL:', error);
                    toast.warning(t('chat.sessionLinkUnavailable', 'The linked session is unavailable. Opened your latest session instead.'));
                }
            }
            const webSessions = mySessions.filter((session: any) => String(session.source_channel || 'web').toLowerCase() === 'web' && !session.is_group);
            const defaultWebSession = webSessions.find((session: any) => session.is_primary) || webSessions.reduce((latest: any | null, session: any) => {
                if (!latest) return session;
                const createdAt = String(session.created_at || '');
                const latestCreatedAt = String(latest.created_at || '');
                if (createdAt !== latestCreatedAt) return createdAt > latestCreatedAt ? session : latest;
                return String(session.id) > String(latest.id) ? session : latest;
            }, null);
            if (defaultWebSession) {
                setChatScope('mine');
                await selectSession(defaultWebSession, 'mine');
            } else {
                clearChatSelection();
            }
        };
        void restoreSessionFromUrl();
        return () => { cancelled = true; };
    }, [activeSessionIdRef, activeTab, clearChatSelection, currentAgentIdRef, fetchAllSessions, id, requestedSessionId, setSessionsLoading, setChatScope, t, toast, token]);

    parseChatMsgRef.current = ({ msg, id: messageAgentId = id, activeSession: messageSession = activeSession }: any) => parseAgentDetailChatMsg({
        msg,
        id: messageAgentId,
        activeSession: messageSession,
    });

    return {
        fetchMySessions,
        fetchAllSessions,
        selectSession,
    };
}
