import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useConversationAutoFollow } from '../../../features/conversation/useConversationAutoFollow';
import { buildConversationEntries, getConversationScrollAnchor } from '../../../features/conversation/core/chatTimeline';
import {
    CONVERSATION_HISTORY_RECONCILE_TURN_PAGE_SIZE,
    createConversationHistoryPageParams,
    resolveConversationHistoryHasMore,
} from '../../../features/conversation/historyPagination';
import {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import { shouldCompleteRecoveryPolling } from '../../../features/conversation/core/resumeRecovery';
import {
    buildChatAttachmentPayload,
} from '../../../utils/chatAttachments';
import { createClientId } from '../../../utils/clientId';
import { uploadFileWithProgress } from '../../../services/api';
import { useDropZone } from '../../../hooks/useDropZone';
import { normalizeChatTimelineMessages } from '../../../features/conversation/core/chatTimeline';

export function useAgentDetailResumeComposer({
    id,
    activeTab,
    token,
    effectiveChatModelId,
    reasoningEffortOverride,
    showNoModelState,
    toast,
    t,
    chat,
    sessionSelection,
    socketMessages,
    helpers,
}: any) {
    const {
        activeSession,
        chatMessages,
        setChatMessages,
        historyMsgs,
        setHistoryMsgs,
        historyOldestTs,
        setHistoryOldestTs,
        historyHasMore,
        setHistoryHasMore,
        historyLoadingMore,
        setHistoryLoadingMore,
        currentAgentIdRef,
        activeSessionIdRef,
        historyMoreAbortRef,
        sessionLoadSeqRef,
        pcPageSuspendedRef,
        pcHiddenDroppedEventRef,
        pcRecoveryPollingNeededRef,
        pcRecoveryPollTimerRef,
        pcRecoveryPollingGenerationRef,
        startPcRecoveryPollingRef,
        pcResumeEventGateRef,
        pcResumeReconcilePromiseRef,
        ensureSessionSocketRef,
        pcResumeHadActiveTurnRef,
        cancelPcAutoFollowRef,
        setPcPageActive,
        pcResumeMeasurementKey,
        setPcResumeMeasurementKey,
        buildSessionRuntimeKey,
        cancelPcRecoveryPolling,
        clearReconnectTimer,
        releasePcResumeReconcileOwner,
        setSessionUiState,
        sessionTurnRuntimeRef,
        sessionUiStateRef,
        clearUnreadForSession,
        isWritableSession,
        setAttachedFiles,
        dismissedWorkspaceRefPath,
        setChatInfoMsg,
        chatInfoTimerRef,
        setWorkspaceLockedPath,
        livePanelVisible,
        sidePanelTab,
        workspaceActivePath,
        chatContainerRef,
        chatInputRef,
        chatInputAreaRef,
        fileInputRef,
        wsMapRef,
        setWsConnected,
        setIsWaiting,
        setIsStreaming,
        setIsStopping,
        chatUploadDrafts,
        setChatUploadDrafts,
        chatUploadAbortRef,
        attachedFiles,
        setWsSessionId,
        wsConnected,
        isWaiting,
        isStreaming,
        isStopping,
        confirmationPending,
        chatInput,
        setChatInput,
        pendingChatSendRef,
        reconnectDisabledRef,
        reconnectAttemptsRef,
        wsRef,
        setAgentExpired,
        setLiveState,
        setSidePanelTab,
        setLivePanelVisible,
        livePanelAutoCollapsedRef,
        setWorkspaceActivePath,
        setWorkspaceActivities,
        setWorkspaceLiveDraft,
        setChatScope,
        setSessions,
        setAllSessions,
        setSessionsHasMore,
        setAllSessionsHasMore,
        setSessionsNextCursor,
        setAllSessionsNextCursor,
        sessionsListAbortRef,
        allSessionsListAbortRef,
        sessionsListGenerationRef,
        allSessionsListGenerationRef,
        setSessionsLoading,
        setAllSessionsLoading,
        setSessionsLoadingMore,
        setAllSessionsLoadingMore,
        setOnboardingKickoffRequest,
        activeReadOnlyRef,
        attachedImagePreviews,
        currentUser,
        pcPageActive,
    } = chat;
    const { fetchMySessions, selectSession } = sessionSelection;
    const { ensureSessionSocket } = socketMessages;
    const {
        parseChatMsgRef,
        historyAutoLoadCursorRef,
        scheduleComposerFocusRef,
        dispatchChatMessageRef,
        handleWorkspacePathDeletedRef,
        discardChatStreamBatchRef,
        discardMonitorStreamBatchRef,
        onboardingRequestsRef,
    } = helpers;

    const [chatScrollBtnBottom, setChatScrollBtnBottom] = useState(96);
    const historyContainerRef = useRef<HTMLDivElement>(null);
    const generationActive = isWaiting || isStreaming || isStopping;
    const liveScrollAnchor = useMemo(() => getConversationScrollAnchor(buildConversationEntries(chatMessages as any), generationActive), [chatMessages, generationActive]);
    const readonlyGenerationActive = Boolean(activeSession && !isWritableSession(activeSession) && generationActive);
    const historyScrollAnchor = useMemo(() => getConversationScrollAnchor(buildConversationEntries(historyMsgs as any), readonlyGenerationActive), [historyMsgs, readonlyGenerationActive]);
    const { showScrollToBottom: showScrollBtn, resumeAutoFollow: scrollToBottom, cancelPendingAutoFollow: cancelLiveAutoFollow, interactionProps: liveAutoFollowInteractionProps } = useConversationAutoFollow({
        scrollerRef: chatContainerRef,
        contentKey: liveScrollAnchor,
        resetKey: activeSession?.id,
        enabled: pcPageActive && activeTab === 'chat' && !!activeSession && isWritableSession(activeSession),
    });
    const { showScrollToBottom: showHistoryScrollBtn, resumeAutoFollow: scrollHistoryToBottom, cancelPendingAutoFollow: cancelHistoryAutoFollow, interactionProps: historyAutoFollowInteractionProps } = useConversationAutoFollow({
        scrollerRef: historyContainerRef,
        contentKey: historyScrollAnchor,
        resetKey: activeSession?.id,
        enabled: pcPageActive && activeTab === 'chat' && !!activeSession && !isWritableSession(activeSession),
    });

    useEffect(() => {
        cancelPcAutoFollowRef.current = () => {
            cancelLiveAutoFollow();
            cancelHistoryAutoFollow();
        };
        return () => {
            cancelPcAutoFollowRef.current = () => undefined;
        };
    }, [cancelHistoryAutoFollow, cancelLiveAutoFollow, cancelPcAutoFollowRef]);

    const scheduleComposerFocus = useCallback(() => {
        let attempts = 0;
        const focusWhenReady = () => {
            const el = chatInputRef.current;
            if (!el || activeTab !== 'chat') {
                if (attempts++ < 8) requestAnimationFrame(focusWhenReady);
                return;
            }
            el.focus({ preventScroll: true });
            const caret = el.value.length;
            try { el.setSelectionRange(caret, caret); } catch { }
        };
        requestAnimationFrame(focusWhenReady);
    }, [activeTab, chatInputRef]);
    scheduleComposerFocusRef.current = scheduleComposerFocus;

    const waitForPcSocketServerConnection = async (socket: WebSocket | undefined) => {
        if (!socket) return false;
        if ((socket as any)._serverConnected === true) return true;
        const connection = (socket as any)._serverConnectedPromise as Promise<boolean> | undefined;
        if (!connection) return false;
        let timeout: ReturnType<typeof setTimeout> | null = null;
        try {
            return await Promise.race([
                connection,
                new Promise<boolean>((resolve) => { timeout = setTimeout(() => resolve(false), 5000); }),
            ]);
        } finally {
            if (timeout) clearTimeout(timeout);
        }
    };

    const dispatchChatMessage = (socket: WebSocket, runtimeKey: string, payload: any) => {
        helpers.pendingPcRouteRecoveryRuntimeKeys.delete(runtimeKey);
        setIsWaiting(true);
        setIsStreaming(false);
        setIsStopping(false);
        setSessionUiState(runtimeKey, { isWaiting: true, isStreaming: false, isStopping: false });
        setChatMessages((prev: any[]) => [...prev, parseChatMsgRef.current({
            msg: {
                id: payload.messageId,
                role: 'user',
                content: payload.displayContent,
                display_content: payload.displayContent,
                fileName: payload.fileName,
                imageUrl: payload.imageUrl,
                previewImages: payload.previewImages,
                attachments: payload.attachments,
                timestamp: new Date().toISOString(),
            },
            id,
            activeSession,
        })]);
        socket.send(JSON.stringify({
            message_id: payload.messageId,
            content: payload.contentForLLM,
            display_content: payload.displayContent,
            file_name: payload.fileName,
            attachments: payload.attachments,
            model_id: payload.modelId,
            reasoning_effort: payload.reasoningEffort,
        }));
    };
    dispatchChatMessageRef.current = dispatchChatMessage;

    useEffect(() => {
        if (!id || !token || activeTab !== 'chat') return;
        if (document.hidden || pcPageSuspendedRef.current) return;
        if (!activeSession) {
            chat.syncActiveSocketState(null, id);
            return;
        }
        activeSessionIdRef.current = String(activeSession.id);
        activeReadOnlyRef.current = !isWritableSession(activeSession);
        ensureSessionSocket(activeSession, id, token);
        chat.syncActiveSocketState(activeSession, id);
    }, [activeSession?.id, activeTab, chat.canViewAllAgentChatSessions, chat.chatScope, ensureSessionSocket, id, isWritableSession, token]);

    useEffect(() => {
        const startRecoveryPolling = (session: any, scope: 'mine' | 'all') => {
            cancelPcRecoveryPolling();
            const pollingGeneration = pcRecoveryPollingGenerationRef.current;
            const delays = [1000, 2000, 4000, 8000, 16000, 30000];
            let attempt = 0;
            let successfulLoads = 0;
            const poll = () => {
                if (pollingGeneration !== pcRecoveryPollingGenerationRef.current || !pcRecoveryPollingNeededRef.current || pcPageSuspendedRef.current || document.hidden || String(activeSessionIdRef.current || '') !== String(session.id)) return;
                if (pcResumeReconcilePromiseRef.current) {
                    if (pollingGeneration !== pcRecoveryPollingGenerationRef.current) return;
                    pcRecoveryPollTimerRef.current = setTimeout(poll, 500);
                    return;
                }
                const runtimeKey = buildSessionRuntimeKey(String(id || ''), String(session.id));
                const gate = chat.beginPcResumeEventGate(runtimeKey);
                let loadedSuccessfully = false;
                const promise = selectSession(session, scope, { preserveLoadedHistory: true }).then((loaded: any) => {
                    loadedSuccessfully = loaded === true;
                    if (loadedSuccessfully) successfulLoads += 1;
                }).finally(() => {
                    const stillActive = !pcPageSuspendedRef.current && !document.hidden && String(activeSessionIdRef.current || '') === String(session.id);
                    chat.finishPcResumeEventGate(gate, stillActive);
                    releasePcResumeReconcileOwner(promise, session, String(id || ''), String(token || ''));
                    if (stillActive) setPcResumeMeasurementKey((current: number | null) => (current ?? 0) + 1);
                    if (pollingGeneration !== pcRecoveryPollingGenerationRef.current) return;
                    if (shouldCompleteRecoveryPolling({
                        pollingGeneration,
                        currentGeneration: pcRecoveryPollingGenerationRef.current,
                        loadedSuccessfully,
                        successfulLoads,
                        stillActive,
                        socketReadyState: wsMapRef.current[runtimeKey]?.readyState,
                    })) {
                        helpers.pendingPcRouteRecoveryRuntimeKeys.delete(runtimeKey);
                        pcRecoveryPollingNeededRef.current = false;
                        pcRecoveryPollTimerRef.current = null;
                        return;
                    }
                    if (!pcRecoveryPollingNeededRef.current || attempt >= delays.length) return;
                    pcRecoveryPollTimerRef.current = setTimeout(poll, delays[attempt++]);
                });
                pcResumeReconcilePromiseRef.current = promise;
            };
            pcRecoveryPollTimerRef.current = setTimeout(poll, delays[attempt++]);
        };
        startPcRecoveryPollingRef.current = startRecoveryPolling;
        const suspend = () => {
            if (pcPageSuspendedRef.current) return;
            pcPageSuspendedRef.current = true;
            setPcPageActive(false);
            discardChatStreamBatchRef.current();
            discardMonitorStreamBatchRef.current();
            cancelPcAutoFollowRef.current();
            chat.sessionMsgAbortRef.current?.abort();
            historyMoreAbortRef.current?.abort();
            cancelPcRecoveryPolling();
            const activeGate = pcResumeEventGateRef.current;
            if (activeGate) chat.finishPcResumeEventGate(activeGate, false);
            if (!id || !activeSession) return;
            const key = buildSessionRuntimeKey(id, String(activeSession.id));
            const runtime = sessionUiStateRef.current[key];
            const ws = wsMapRef.current[key];
            const turnWasActive = !!runtime && (runtime.isWaiting || runtime.isStreaming || runtime.isStopping);
            sessionTurnRuntimeRef.current[key] = beginConversationTurnRecovery(sessionTurnRuntimeRef.current[key] || IDLE_CONVERSATION_TURN);
            pcResumeHadActiveTurnRef.current = turnWasActive;
            if (turnWasActive) pcRecoveryPollingNeededRef.current = true;
            reconnectDisabledRef.current[key] = true;
            clearReconnectTimer(key);
            if (wsMapRef.current[key] === ws) delete wsMapRef.current[key];
            if (wsRef.current === ws) wsRef.current = null;
            if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, 'page hidden');
            setSessionUiState(key, { isWaiting: false, isStreaming: false, isStopping: false });
            setWsConnected(false);
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            setOnboardingKickoffRequest(null);
        };
        const resume = () => {
            if (!pcPageSuspendedRef.current || document.hidden) return;
            pcPageSuspendedRef.current = false;
            setPcPageActive(true);
            if (!id || !token || activeTab !== 'chat') return;
            if (!activeSession) return;
            const key = buildSessionRuntimeKey(id, String(activeSession.id));
            const resumeHadActiveTurn = pcResumeHadActiveTurnRef.current;
            pcResumeHadActiveTurnRef.current = false;
            pcHiddenDroppedEventRef.current = false;
            reconnectDisabledRef.current[key] = false;
            reconnectAttemptsRef.current[key] = 0;
            clearReconnectTimer(key);
            if (pcResumeReconcilePromiseRef.current) {
                pcRecoveryPollingNeededRef.current = true;
                startRecoveryPolling(activeSession, chat.chatScope);
                return;
            }
            const gate = chat.beginPcResumeEventGate(key);
            const socket = ensureSessionSocket(activeSession, id, token);
            const promise = (async () => {
                await waitForPcSocketServerConnection(socket);
                if (pcPageSuspendedRef.current || document.hidden || String(activeSessionIdRef.current || '') !== String(activeSession.id)) {
                    chat.finishPcResumeEventGate(gate, false);
                    return;
                }
                await selectSession(activeSession, chat.chatScope, { preserveLoadedHistory: true, prepareWebResume: resumeHadActiveTurn });
                const stillActive = !pcPageSuspendedRef.current && !document.hidden && String(activeSessionIdRef.current || '') === String(activeSession.id);
                chat.finishPcResumeEventGate(gate, stillActive);
                if (!stillActive) return;
                setPcResumeMeasurementKey((current: number | null) => (current ?? 0) + 1);
                if (pcRecoveryPollingNeededRef.current) startRecoveryPolling(activeSession, chat.chatScope);
            })().finally(() => {
                releasePcResumeReconcileOwner(promise, activeSession, id, token);
            });
            pcResumeReconcilePromiseRef.current = promise;
        };
        const onVisibility = () => {
            if (document.hidden) suspend();
            else resume();
        };
        const onPageHide = () => suspend();
        const onPageShow = () => resume();
        document.addEventListener('visibilitychange', onVisibility);
        window.addEventListener('pagehide', onPageHide);
        window.addEventListener('pageshow', onPageShow);
        if (document.hidden || activeTab !== 'chat') suspend();
        else resume();
        return () => {
            document.removeEventListener('visibilitychange', onVisibility);
            window.removeEventListener('pagehide', onPageHide);
            window.removeEventListener('pageshow', onPageShow);
            cancelPcRecoveryPolling();
            startPcRecoveryPollingRef.current = () => undefined;
        };
    }, [activeSession?.id, activeTab, cancelPcAutoFollowRef, cancelPcRecoveryPolling, clearReconnectTimer, discardChatStreamBatchRef, discardMonitorStreamBatchRef, ensureSessionSocket, historyMoreAbortRef, id, pcPageSuspendedRef, releasePcResumeReconcileOwner, selectSession, setPcPageActive, setPcResumeMeasurementKey, setSessionUiState, setWsConnected, setOnboardingKickoffRequest, setIsStopping, setIsStreaming, setIsWaiting, token, wsMapRef, wsRef]);

    useEffect(() => {
        if (!id || !activeSession || activeTab !== 'chat' || document.hidden) return;
        const runtimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        if (!helpers.pendingPcRouteRecoveryRuntimeKeys.has(runtimeKey)) return;
        pcRecoveryPollingNeededRef.current = true;
        startPcRecoveryPollingRef.current(activeSession, chat.chatScope);
    }, [activeSession?.id, activeTab, id, startPcRecoveryPollingRef]);

    const handleWorkspacePathDeleted = useCallback((path: string) => {
        let removedName = '';
        setAttachedFiles((prev: any[]) => prev.filter((file: any) => {
            const shouldRemove = file.source === 'workspace_auto' && file.path === path;
            if (shouldRemove) removedName = file.name;
            return !shouldRemove;
        }));
        setWorkspaceLockedPath((current: any) => current === path ? null : current);
        dismissedWorkspaceRefPath.current = path;
        if (removedName) {
            setChatInfoMsg(`Removed attachment: ${removedName} (file was deleted).`);
            if (chatInfoTimerRef.current) clearTimeout(chatInfoTimerRef.current);
            chatInfoTimerRef.current = setTimeout(() => {
                setChatInfoMsg(null);
                chatInfoTimerRef.current = null;
            }, 4000);
        }
    }, [chatInfoTimerRef, dismissedWorkspaceRefPath, setAttachedFiles, setChatInfoMsg, setWorkspaceLockedPath]);
    handleWorkspacePathDeletedRef.current = handleWorkspacePathDeleted;

    useEffect(() => {
        const shouldAutoReference = livePanelVisible && sidePanelTab === 'workspace' && !!workspaceActivePath;
        if (!shouldAutoReference) {
            dismissedWorkspaceRefPath.current = null;
            setAttachedFiles((prev: any[]) => prev.filter((file: any) => file.source !== 'workspace_auto'));
            return;
        }
        const path = workspaceActivePath!;
        if (dismissedWorkspaceRefPath.current === path) return;
        setAttachedFiles((prev: any[]) => {
            const withoutAuto = prev.filter((file: any) => file.source !== 'workspace_auto');
            return [...withoutAuto, { name: helpers.workspaceFileName(path), text: '', path, source: 'workspace_auto' }];
        });
    }, [dismissedWorkspaceRefPath, livePanelVisible, setAttachedFiles, sidePanelTab, workspaceActivePath, helpers.workspaceFileName]);

    useEffect(() => {
        return () => {
            const activeAgentId = String(currentAgentIdRef.current || '');
            const activeSessionId = String(activeSessionIdRef.current || '');
            if (activeAgentId && activeSessionId) {
                const runtimeKey = buildSessionRuntimeKey(activeAgentId, activeSessionId);
                const runtime = sessionUiStateRef.current[runtimeKey];
                sessionTurnRuntimeRef.current[runtimeKey] = beginConversationTurnRecovery(sessionTurnRuntimeRef.current[runtimeKey] || IDLE_CONVERSATION_TURN);
                if (pcRecoveryPollingNeededRef.current || runtime?.isWaiting || runtime?.isStreaming || runtime?.isStopping) helpers.pendingPcRouteRecoveryRuntimeKeys.add(runtimeKey);
            }
            pcPageSuspendedRef.current = true;
            pcResumeReconcilePromiseRef.current = null;
            const resumeGate = pcResumeEventGateRef.current;
            pcResumeEventGateRef.current = null;
            if (resumeGate) chat.finishPcResumeEventGate(resumeGate, false);
            ensureSessionSocketRef.current = () => undefined;
            chat.sessionMsgAbortRef.current?.abort();
            historyMoreAbortRef.current?.abort();
            cancelPcRecoveryPolling();
            Object.keys(reconnectDisabledRef.current).forEach((key) => { reconnectDisabledRef.current[key] = true; });
            Object.keys(chat.reconnectTimerRef.current).forEach((key) => clearReconnectTimer(key));
            Object.values(wsMapRef.current).forEach((ws: any) => {
                if (ws.readyState !== WebSocket.CLOSED) ws.close();
            });
            chat.wsMapRef.current = {};
            wsRef.current = null;
            discardChatStreamBatchRef.current();
            discardMonitorStreamBatchRef.current();
        };
    }, []);

    const loadMoreHistoryMessages = useCallback(async () => {
        if (historyLoadingMore || historyMoreAbortRef.current || !historyHasMore || !activeSession || !id) return;
        if (!historyOldestTs) {
            setHistoryHasMore(false);
            return;
        }
        const sess = activeSession;
        const targetAgentId = id;
        const loadSeq = sessionLoadSeqRef.current;
        const writable = isWritableSession(sess);
        const controller = new AbortController();
        historyMoreAbortRef.current = controller;
        setHistoryLoadingMore(true);
        try {
            const tkn = localStorage.getItem('token');
            const params = createConversationHistoryPageParams(historyOldestTs);
            const res = await fetch(`/api/agents/${targetAgentId}/sessions/${sess.id}/message-turns?${params}`, { headers: { Authorization: `Bearer ${tkn}` }, signal: controller.signal });
            if (!res.ok) return;
            const responseCursor = res.headers.get('X-Message-Next-Cursor');
            const responseHasMore = res.headers.get('X-Message-Has-More');
            const msgs = await res.json();
            if (loadSeq !== sessionLoadSeqRef.current || currentAgentIdRef.current !== targetAgentId || String(activeSessionIdRef.current) !== String(sess.id)) return;
            if (msgs.length === 0) {
                setHistoryHasMore(false);
                return;
            }
            const preParsed = msgs.map((m: any) => parseChatMsgRef.current({
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
                    ...(m.sender_avatar_url && { sender_avatar_url: m.sender_avatar_url }),
                },
                id,
                activeSession,
            }));
            const el = writable ? chatContainerRef.current : historyContainerRef.current;
            const oldScrollHeight = el?.scrollHeight ?? 0;
            const oldScrollTop = el?.scrollTop ?? 0;
            const prependPage = (prev: any[]) => {
                const newerMessageIds = new Set(prev.map((m: any) => m.id).filter(Boolean));
                return normalizeChatTimelineMessages([...preParsed.filter((m: any) => !m.id || !newerMessageIds.has(m.id)), ...prev]);
            };
            if (writable) setChatMessages(prependPage);
            else setHistoryMsgs(prependPage);
            const nextOldestTs = responseCursor || (msgs[0]?.created_at ? `${msgs[0].created_at}${msgs[0].id ? `|${msgs[0].id}` : ''}` : null);
            setHistoryOldestTs(nextOldestTs);
            setHistoryHasMore(Boolean(nextOldestTs && nextOldestTs !== historyOldestTs && resolveConversationHistoryHasMore(responseHasMore)));
            requestAnimationFrame(() => {
                if (el) {
                    const newScrollHeight = el.scrollHeight;
                    el.scrollTop = oldScrollTop + newScrollHeight - oldScrollHeight;
                }
            });
        } catch (err: any) {
            if (err?.name === 'AbortError') return;
            console.error('Failed to load more history messages:', err);
        } finally {
            if (historyMoreAbortRef.current === controller) historyMoreAbortRef.current = null;
            if (loadSeq === sessionLoadSeqRef.current) setHistoryLoadingMore(false);
        }
    }, [activeSession, historyHasMore, historyLoadingMore, historyOldestTs, id, isWritableSession, parseChatMsgRef, setChatMessages, setHistoryHasMore, setHistoryLoadingMore, setHistoryMsgs, setHistoryOldestTs]);

    useEffect(() => {
        if (!activeSession || historyLoadingMore || !historyHasMore || !historyOldestTs) return;
        const el = isWritableSession(activeSession) ? chatContainerRef.current : historyContainerRef.current;
        if (el && el.clientHeight > 0 && el.scrollHeight <= el.clientHeight + 1 && historyAutoLoadCursorRef.current !== historyOldestTs) {
            historyAutoLoadCursorRef.current = historyOldestTs;
            loadMoreHistoryMessages();
        }
    }, [activeSession?.id, chatMessages.length, historyMsgs.length, historyHasMore, historyLoadingMore, historyOldestTs, isWritableSession, loadMoreHistoryMessages]);

    const handleHistoryScroll = () => {
        const el = historyContainerRef.current;
        if (!el) return;
        if (el.scrollTop < 100 && historyHasMore && !historyLoadingMore) loadMoreHistoryMessages();
    };
    useEffect(() => {
        if (activeTab === 'chat' && activeSession && isWritableSession(activeSession)) scheduleComposerFocus();
    }, [activeSession?.id, activeTab, isWritableSession, scheduleComposerFocus]);
    const handleChatScroll = () => {
        const el = chatContainerRef.current;
        if (!el) return;
        if (el.scrollTop < 100 && historyHasMore && !historyLoadingMore) loadMoreHistoryMessages();
    };
    useEffect(() => {
        const gapAboveComposer = 14;
        const updateScrollButtonOffset = () => {
            const composerAreaHeight = chatInputAreaRef.current?.offsetHeight ?? 82;
            setChatScrollBtnBottom(composerAreaHeight + gapAboveComposer);
        };
        updateScrollButtonOffset();
        if (typeof ResizeObserver === 'undefined' || !chatInputAreaRef.current) return;
        const observer = new ResizeObserver(() => updateScrollButtonOffset());
        observer.observe(chatInputAreaRef.current);
        return () => observer.disconnect();
    }, [activeSession?.id, activeTab, attachedFiles.length, chatUploadDrafts.length, chatInputAreaRef]);

    const sendChatMsg = () => {
        if (!id || !activeSession?.id) return;
        if (showNoModelState) return;
        if (isWaiting || isStreaming || isStopping || confirmationPending) return;
        const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        const activeSocket = wsMapRef.current[activeRuntimeKey];
        if (!chatInput.trim() && attachedFiles.length === 0) return;
        const attachmentPayload = buildChatAttachmentPayload({ input: chatInput.trim(), attachments: attachedFiles });
        const payload = {
            runtimeKey: activeRuntimeKey,
            contentForLLM: attachmentPayload.contentForLLM,
            displayContent: attachmentPayload.displayContent,
            fileName: attachmentPayload.fileName,
            imageUrl: attachmentPayload.imageUrl,
            previewImages: attachmentPayload.previewImages,
            attachments: attachmentPayload.attachments,
            modelId: effectiveChatModelId,
            reasoningEffort: reasoningEffortOverride || null,
            messageId: createClientId(),
        };
        setChatInput('');
        scrollToBottom();
        if (chatInputRef.current) chatInputRef.current.style.height = 'auto';
        dismissedWorkspaceRefPath.current = null;
        setAttachedFiles((prev: any[]) => prev.filter((file: any) => file.source === 'workspace_auto'));
        if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN) {
            pendingChatSendRef.current = payload;
            if (token) ensureSessionSocket(activeSession, id, token);
            setChatInfoMsg('Connection is reconnecting. Your message will be sent automatically.');
            if (chatInfoTimerRef.current) clearTimeout(chatInfoTimerRef.current);
            chatInfoTimerRef.current = setTimeout(() => setChatInfoMsg(null), 4000);
            return;
        }
        dispatchChatMessage(activeSocket, activeRuntimeKey, payload);
    };

    const runUpload = async (files: File[], baseId: string) => {
        const allowedFiles = files.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            toast.warning('最多可附加 10 个文件');
            return;
        }
        const newDrafts = allowedFiles.map((file, i) => ({ id: `${baseId}-${i}-${file.name}`, name: file.name, percent: 0, previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined, sizeBytes: file.size }));
        setChatUploadDrafts((prev: any[]) => [...prev, ...newDrafts]);
        const runOne = async (file: File, draft: any) => {
            const { promise, abort } = uploadFileWithProgress('/chat/upload', file, (pct) => {
                setChatUploadDrafts((prev: any[]) => prev.map((d: any) => (d.id === draft.id ? { ...d, percent: pct >= 101 ? 100 : pct } : d)));
            }, id ? { agent_id: id } : undefined, 600_000);
            chatUploadAbortRef.current.set(draft.id, abort);
            try {
                const data = await promise;
                const uploadedName = data.saved_filename || data.filename || file.name;
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setChatUploadDrafts((prev: any[]) => prev.filter((d: any) => d.id !== draft.id));
                chatUploadAbortRef.current.delete(draft.id);
                setAttachedFiles((prev: any[]) => [...prev, { name: uploadedName, text: data.extracted_text, path: data.workspace_path, imageUrl: data.image_data_url || undefined, mimeType: file.type || undefined, sizeBytes: data.size ?? file.size }].slice(0, 10));
            } catch (err: any) {
                if (draft.previewUrl) URL.revokeObjectURL(draft.previewUrl);
                setChatUploadDrafts((prev: any[]) => prev.filter((d: any) => d.id !== draft.id));
                chatUploadAbortRef.current.delete(draft.id);
                if (err?.message !== 'Upload cancelled') toast.error(t('agent.upload.failed'), { details: String(err?.message || err) });
            }
        };
        await Promise.all(allowedFiles.map((file, i) => runOne(file, newDrafts[i])));
    };

    const handleChatFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        if (confirmationPending) {
            e.target.value = '';
            return;
        }
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        await runUpload(files, `up-${Date.now()}`);
        if (fileInputRef.current) fileInputRef.current.value = '';
    };
    const handlePaste = async (e: React.ClipboardEvent) => {
        if (confirmationPending) {
            e.preventDefault();
            return;
        }
        const items = e.clipboardData?.items;
        if (!items) return;
        const filesToUpload: File[] = [];
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.startsWith('image/')) {
                const blob = items[i].getAsFile();
                if (blob) {
                    const ext = blob.type.split('/')[1] || 'png';
                    filesToUpload.push(new File([blob], `paste-${Date.now()}-${i}.${ext}`, { type: blob.type }));
                }
            }
        }
        if (!filesToUpload.length) return;
        e.preventDefault();
        await runUpload(filesToUpload, `paste-${Date.now()}`);
    };
    const handleDroppedChatFiles = useCallback(async (files: File[]) => {
        if (confirmationPending || !wsConnected || chatUploadDrafts.length > 0 || isWaiting || isStreaming || isStopping || attachedFiles.length >= 10) return;
        const availableSlots = Math.max(0, 10 - attachedFiles.length);
        const filesToProcess = files.slice(0, availableSlots);
        for (const file of filesToProcess) {
            const draftId = Math.random().toString(36).slice(2, 9);
            const previewUrl = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined;
            setChatUploadDrafts((prev: any[]) => [...prev, { id: draftId, name: file.name, percent: 0, previewUrl, sizeBytes: file.size }]);
            try {
                const { promise } = uploadFileWithProgress('/chat/upload', file, (pct) => {
                    setChatUploadDrafts((prev: any[]) => prev.map((d: any) => d.id === draftId ? { ...d, percent: pct >= 101 ? 100 : pct } : d));
                }, id ? { agent_id: id } : undefined, 600_000);
                const data = await promise;
                setAttachedFiles((prev: any[]) => [...prev, { name: data.filename, text: data.extracted_text, path: data.workspace_path, imageUrl: data.image_data_url || undefined, mimeType: file.type || undefined, sizeBytes: data.size ?? file.size }]);
            } catch (err: any) {
                if (err?.message !== 'Upload cancelled') toast.error(t('agent.upload.failed'), { details: String(err?.message || '') });
            } finally {
                if (previewUrl) URL.revokeObjectURL(previewUrl);
                setChatUploadDrafts((prev: any[]) => prev.filter((d: any) => d.id !== draftId));
            }
        }
    }, [attachedFiles.length, chatUploadDrafts.length, confirmationPending, id, isStopping, isStreaming, isWaiting, setAttachedFiles, setChatUploadDrafts, t, toast, wsConnected]);
    const { isDragging: isChatDragging, dropZoneProps: chatDropProps } = useDropZone({
        onDrop: handleDroppedChatFiles,
        disabled: confirmationPending || !wsConnected || chatUploadDrafts.length > 0 || isWaiting || isStreaming || isStopping || attachedFiles.length >= 10 || !activeSession || !isWritableSession(activeSession),
    });

    return {
        historyContainerRef,
        showScrollBtn,
        scrollToBottom,
        liveAutoFollowInteractionProps,
        showHistoryScrollBtn,
        scrollHistoryToBottom,
        historyAutoFollowInteractionProps,
        scheduleComposerFocus,
        handleHistoryScroll,
        handleChatScroll,
        chatScrollBtnBottom,
        sendChatMsg,
        handleChatFile,
        handlePaste,
        isChatDragging,
        chatDropProps,
        generationActive,
        readonlyGenerationActive,
    };
}
