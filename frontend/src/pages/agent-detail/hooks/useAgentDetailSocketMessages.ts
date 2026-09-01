import { useCallback, useEffect, useRef } from 'react';
import { appendLiveCodeOutput } from '../../../components/AgentBayLivePanel';
import { createClientId } from '../../../utils/clientId';
import { foldConversationTimelineEvent } from '../../../features/conversation/core/chatTimeline';
import {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    conversationTurnEventClosesStream,
    conversationTurnEventShouldBeHandled,
    conversationTurnIsStreaming,
    conversationTurnIsWaiting,
    reduceConversationTurnEvent,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import {
    bufferResumeEvent,
    resolveVisibleTerminalRecoveryAction,
} from '../../../features/conversation/core/resumeRecovery';
import {
    AWARE_TOOLS,
    WORKSPACE_TOOLS,
    isFocusPath,
    parseAgentBayTransferArgs,
    parseWorkspaceDraftArgs,
    workspaceActionForTool,
} from '../shared';

export function useAgentDetailSocketMessages({
    id,
    i18n,
    queryClient,
    chat,
    resources,
    sessionSelection,
    helpers,
}: any) {
    const {
        activeSession,
        chatScope,
        allSessions,
        canViewAllAgentChatSessions,
        wsMapRef,
        reconnectTimerRef,
        reconnectDisabledRef,
        reconnectAttemptsRef,
        sessionUiStateRef,
        sessionTurnRuntimeRef,
        activeSessionIdRef,
        activeReadOnlyRef,
        currentAgentIdRef,
        pcPageSuspendedRef,
        pcHiddenDroppedEventRef,
        pcRecoveryPollingNeededRef,
        startPcRecoveryPollingRef,
        pcResumeEventGateRef,
        setSessionUiState,
        clearUnreadForSession,
        setChatMessages,
        setHistoryMsgs,
        flushChatStreamBatch,
        discardChatStreamBatch,
        enqueueChatStreamEvent,
        chatInfoTimerRef,
        setChatInfoMsg,
        setLiveState,
        setWorkspaceActivePath,
        setWorkspaceActivities,
        setWorkspaceLiveDraft,
        setSidePanelTab,
        setLivePanelVisible,
        allowWorkspaceAutoSwitch,
        allowLivePanelAutoFocus,
        collapseSidebarsForLivePanel,
        openAwarePanel,
        setWsSessionId,
        setWsConnected,
        setIsWaiting,
        setIsStreaming,
        setIsStopping,
        setOnboardingKickoffRequest,
        pendingChatSendRef,
        wsRef,
        setAgentExpired,
        buildSessionRuntimeKey,
        clearReconnectTimer,
        closeSessionSocket,
    } = chat;
    const { fetchMySessions, fetchAllSessions } = sessionSelection;
    const { refetchTriggers, refetchFocusItems, workspacePath } = resources;
    const {
        onboardingRequestsRef,
        parseChatMsgRef,
        dispatchChatMessageRef,
        handleWorkspacePathDeletedRef,
        discardMonitorStreamBatchRef,
    } = helpers;

    const applyMonitorEvent = (prev: any[], d: any): any[] => {
        return foldConversationTimelineEvent(prev, d, {
            makeId: createClientId,
            preserveTransient: d._preserveTurnStream === true,
        }).messages;
    };
    const applyMonitorEventRef = useRef(applyMonitorEvent);
    applyMonitorEventRef.current = applyMonitorEvent;
    const monitorStreamBatchRef = useRef<any[]>([]);
    const monitorStreamBatchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const flushMonitorStreamBatch = useCallback(() => {
        if (monitorStreamBatchTimerRef.current) {
            clearTimeout(monitorStreamBatchTimerRef.current);
            monitorStreamBatchTimerRef.current = null;
        }
        const batch = monitorStreamBatchRef.current;
        if (batch.length === 0) return;
        monitorStreamBatchRef.current = [];
        setHistoryMsgs((prev: any[]) => batch.reduce((next, event) => applyMonitorEventRef.current(next, event), prev));
    }, [setHistoryMsgs]);
    const discardMonitorStreamBatch = useCallback(() => {
        if (monitorStreamBatchTimerRef.current) clearTimeout(monitorStreamBatchTimerRef.current);
        monitorStreamBatchTimerRef.current = null;
        monitorStreamBatchRef.current = [];
    }, []);
    const enqueueMonitorStreamEvent = useCallback((event: any) => {
        const last = monitorStreamBatchRef.current[monitorStreamBatchRef.current.length - 1];
        if (last && last.type === event.type && String(last.message_id || '') === String(event.message_id || '') && String(last.turn?.turn_anchor_id || '') === String(event.turn?.turn_anchor_id || '')) last.content = `${last.content || ''}${event.content || ''}`;
        else monitorStreamBatchRef.current.push({ ...event });
        if (!monitorStreamBatchTimerRef.current) monitorStreamBatchTimerRef.current = setTimeout(flushMonitorStreamBatch, 40);
    }, [flushMonitorStreamBatch]);
    discardMonitorStreamBatchRef.current = discardMonitorStreamBatch;

    const ensureSessionSocket = (sess: any, agentId: string, authToken: string) => {
        const sessionId = String(sess.id);
        const key = buildSessionRuntimeKey(agentId, sessionId);
        const existing = wsMapRef.current[key];
        if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return existing;
        reconnectDisabledRef.current[key] = false;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const sessionParam = `&session_id=${sessionId}`;
        const scheduleReconnect = () => {
            if (reconnectDisabledRef.current[key]) return;
            clearReconnectTimer(key);
            if (typeof document !== 'undefined' && document.hidden) return;
            const attempt = reconnectAttemptsRef.current[key] || 0;
            reconnectAttemptsRef.current[key] = attempt + 1;
            const base = Math.min(30000, 1000 * 2 ** attempt);
            const delay = Math.round(base * (0.75 + Math.random() * 0.5));
            reconnectTimerRef.current[key] = setTimeout(() => {
                reconnectTimerRef.current[key] = null;
                if (!reconnectDisabledRef.current[key]) ensureSessionSocket(sess, agentId, authToken);
            }, delay);
        };
        const lang = (i18n.language || 'en').toLowerCase().startsWith('zh') ? 'zh' : 'en';
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/chat/${agentId}?token=${authToken}${sessionParam}&lang=${lang}`);
        let settleServerConnection: ((connected: boolean) => void) | null = null;
        (ws as any)._serverConnectedPromise = new Promise<boolean>((resolve) => { settleServerConnection = resolve; });
        (ws as any)._settleServerConnection = (connected: boolean) => {
            settleServerConnection?.(connected);
            settleServerConnection = null;
        };
        wsMapRef.current[key] = ws;
        ws.onopen = () => {
            if (reconnectDisabledRef.current[key] || wsMapRef.current[key] !== ws) {
                ws.close();
                return;
            }
            clearReconnectTimer(key);
            (ws as any)._stableTimer = setTimeout(() => {
                if (wsMapRef.current[key] === ws) reconnectAttemptsRef.current[key] = 0;
            }, 3000);
            if (currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId) wsRef.current = ws;
        };
        ws.onclose = (e) => {
            (ws as any)._settleServerConnection?.(false);
            if ((ws as any)._stableTimer) {
                clearTimeout((ws as any)._stableTimer);
                (ws as any)._stableTimer = null;
            }
            const wasCurrent = wsMapRef.current[key] === ws;
            if (onboardingRequestsRef.current[key]?.socket === ws) delete onboardingRequestsRef.current[key];
            if (!wasCurrent) return;
            delete wsMapRef.current[key];
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            const runtimeBeforeClose = sessionUiStateRef.current[key];
            sessionTurnRuntimeRef.current[key] = beginConversationTurnRecovery(sessionTurnRuntimeRef.current[key] || IDLE_CONVERSATION_TURN);
            const turnWasActive = Boolean(runtimeBeforeClose && (runtimeBeforeClose.isWaiting || runtimeBeforeClose.isStreaming || runtimeBeforeClose.isStopping));
            if (turnWasActive && isActiveRuntime) pcRecoveryPollingNeededRef.current = true;
            setSessionUiState(key, { isWaiting: false, isStreaming: false, isStopping: false });
            if (isActiveRuntime) {
                wsRef.current = null;
                setWsConnected(false);
                setIsWaiting(false);
                setIsStreaming(false);
                setIsStopping(false);
                setOnboardingKickoffRequest(null);
                if (turnWasActive && !pcPageSuspendedRef.current) startPcRecoveryPollingRef.current(sess, activeReadOnlyRef.current ? 'all' : 'mine');
            }
            if (e.code === 4003 || e.code === 4002) {
                reconnectDisabledRef.current[key] = true;
                clearReconnectTimer(key);
                reconnectAttemptsRef.current[key] = 0;
                if (isActiveRuntime && e.code === 4003) setAgentExpired(true);
                return;
            }
            scheduleReconnect();
        };
        ws.onerror = (error) => {
            if (wsMapRef.current[key] !== ws) return;
            if (currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId) setWsConnected(false);
            console.warn(`WebSocket error for session ${sessionId}:`, error);
        };
        const handleSocketMessage = (d: any) => {
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            const turnReduction = reduceConversationTurnEvent(sessionTurnRuntimeRef.current[key] || IDLE_CONVERSATION_TURN, d);
            if (!conversationTurnEventShouldBeHandled(turnReduction)) return;
            sessionTurnRuntimeRef.current[key] = turnReduction.runtime;
            if (turnReduction.controlsLifecycle && turnReduction.hasSnapshot) {
                const nextWaiting = conversationTurnIsWaiting(turnReduction.runtime);
                const nextStreaming = conversationTurnIsStreaming(turnReduction.runtime);
                setSessionUiState(key, { isWaiting: nextWaiting, isStreaming: nextStreaming, ...(turnReduction.runtime.snapshot.phase !== 'active' ? { isStopping: false } : {}) });
                if (isActiveRuntime) {
                    setIsWaiting(nextWaiting);
                    setIsStreaming(nextStreaming);
                    if (turnReduction.runtime.snapshot.phase !== 'active') setIsStopping(false);
                }
            }
            const isTerminalEvent = conversationTurnEventClosesStream(turnReduction, d);
            if (isTerminalEvent && pcPageSuspendedRef.current && isActiveRuntime) {
                pcRecoveryPollingNeededRef.current = true;
            } else if (isTerminalEvent) {
                const recoveryAction = resolveVisibleTerminalRecoveryAction({ isActiveRuntime, recoveryNeeded: pcRecoveryPollingNeededRef.current });
                if (recoveryAction === 'continue') startPcRecoveryPollingRef.current(sess, activeReadOnlyRef.current ? 'all' : 'mine');
                else if (recoveryAction === 'clear') {
                    pcRecoveryPollingNeededRef.current = false;
                    chat.cancelPcRecoveryPolling();
                }
            }
            if (pcPageSuspendedRef.current && isActiveRuntime) {
                pcHiddenDroppedEventRef.current = true;
                if (isTerminalEvent) {
                    const currentRuntime = sessionUiStateRef.current[key] || { isWaiting: false, isStreaming: false, isStopping: false };
                    sessionUiStateRef.current[key] = { ...currentRuntime, isWaiting: false, isStreaming: false, isStopping: false };
                }
                return;
            }
            if (d.type !== 'thinking' && d.type !== 'chunk') {
                flushChatStreamBatch();
                flushMonitorStreamBatch();
            }
            if (d.type === 'connected' && d.session_id) {
                (ws as any)._serverConnected = true;
                (ws as any)._settleServerConnection?.(true);
                const request = { sessionId: String(d.session_id), required: d.onboarding_required === true, socket: ws };
                onboardingRequestsRef.current[key] = request;
                if (isActiveRuntime) {
                    wsRef.current = ws;
                    setWsConnected(true);
                    setWsSessionId(String(d.session_id));
                    setOnboardingKickoffRequest(request);
                    if (request.required) {
                        setSessionUiState(key, { isWaiting: true, isStreaming: false });
                        setIsWaiting(true);
                        setIsStreaming(false);
                    }
                }
                if (!request.required && pendingChatSendRef.current?.runtimeKey === key) {
                    const pending = pendingChatSendRef.current;
                    pendingChatSendRef.current = null;
                    setChatInfoMsg(null);
                    dispatchChatMessageRef.current(ws, key, pending);
                }
                return;
            }
            if (d.type === 'onboarded') {
                delete onboardingRequestsRef.current[key];
                if (isActiveRuntime) setOnboardingKickoffRequest(null);
                queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                return;
            }
            if (d.type === 'onboarding_skipped') {
                delete onboardingRequestsRef.current[key];
                setSessionUiState(key, { isWaiting: false, isStreaming: false, isStopping: false });
                if (isActiveRuntime) {
                    setOnboardingKickoffRequest(null);
                    setIsWaiting(false);
                    setIsStreaming(false);
                    setIsStopping(false);
                }
                queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                return;
            }
            if (d.type === 'turn_receipt') return;
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot && ['thinking', 'chunk', 'workspace_draft', 'tool_call', 'confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                const nextStreaming = ['thinking', 'chunk', 'workspace_draft', 'tool_call'].includes(d.type);
                const endStreaming = ['confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type);
                setSessionUiState(key, { isWaiting: false, isStreaming: endStreaming ? false : nextStreaming, ...(endStreaming ? { isStopping: false } : {}) });
            }
            if (!isActiveRuntime) {
                if (['done', 'error', 'quota_exceeded', 'trigger_notification'].includes(d.type)) {
                    fetchMySessions(true, agentId);
                    queryClient.invalidateQueries({ queryKey: ['agents'] });
                }
                if (conversationTurnEventClosesStream(turnReduction, d)) closeSessionSocket(key, true);
                if (!['confirmation_card', 'confirmation_update'].includes(d.type)) return;
            }
            if (activeReadOnlyRef.current) {
                if (['channel_user_message', 'user_message_committed', 'assistant_message_committed', 'thinking', 'chunk', 'tool_call', 'done'].includes(d.type)) {
                    if (d.type === 'thinking' || d.type === 'chunk') enqueueMonitorStreamEvent(d);
                    else setHistoryMsgs((prev: any[]) => applyMonitorEvent(prev, { ...d, _preserveTurnStream: !turnReduction.controlsLifecycle }));
                    if (d.type === 'done') {
                        const sid = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                        if (sid) clearUnreadForSession(sid);
                        fetchMySessions(true, agentId);
                    }
                }
                return;
            }
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot && ['thinking', 'chunk', 'workspace_draft', 'tool_call', 'confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                setIsWaiting(false);
                if (['thinking', 'chunk', 'workspace_draft', 'tool_call'].includes(d.type)) setIsStreaming(true);
                if (['confirmation_required', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                    setIsStreaming(false);
                    setIsStopping(false);
                }
            }
            if (d.type === 'confirmation_required') {
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId }).messages as any[]);
            } else if (d.type === 'thinking') {
                enqueueChatStreamEvent({ type: 'thinking', content: d.content || '', messageId: d.message_id ? String(d.message_id) : undefined, turnAnchorId: d.turn?.turn_anchor_id ? String(d.turn.turn_anchor_id) : undefined, turnGeneration: Number.isInteger(d.turn?.generation) ? Number(d.turn.generation) : undefined, producerScope: d.producer_scope ? String(d.producer_scope) : undefined });
            } else if (d.type === 'workspace_draft') {
                if (WORKSPACE_TOOLS.has(d.name)) {
                    const parsedDraft = parseWorkspaceDraftArgs(d.name, d.arguments || '');
                    const draft = { id: d.id || `${d.name}-${d.index || 0}`, tool: d.name, action: workspaceActionForTool(d.name), status: 'drafting', ...parsedDraft };
                    setWorkspaceLiveDraft(draft);
                    if (allowWorkspaceAutoSwitch(draft.path)) setWorkspaceActivePath(draft.path!);
                    if (isFocusPath(draft.path)) openAwarePanel();
                    else if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('workspace');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
            } else if (d.type === 'tool_call') {
                if (AWARE_TOOLS.has(d.name)) {
                    openAwarePanel();
                    if (d.status === 'done') {
                        refetchTriggers();
                        refetchFocusItems();
                        queryClient.invalidateQueries({ queryKey: ['focus', id] });
                    }
                }
                if (d.name === 'agentbay_file_transfer') {
                    const transfer = parseAgentBayTransferArgs(d.args);
                    setLiveState((prev: any) => ({
                        ...prev,
                        transfer: {
                            ...prev.transfer,
                            ...transfer,
                            status: d.status === 'done' ? 'done' : 'running',
                            result: d.status === 'done' && typeof d.result === 'string' ? d.result : prev.transfer?.result,
                            updatedAt: Date.now(),
                        },
                    }));
                    if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('transfer');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
                if (WORKSPACE_TOOLS.has(d.name)) {
                    if (d.status === 'running') {
                        const rawArgs = typeof d.args === 'string' ? d.args : JSON.stringify(d.args || {});
                        const parsedDraft = parseWorkspaceDraftArgs(d.name, rawArgs);
                        const draft = { id: d.id || `${d.name}-running`, tool: d.name, action: workspaceActionForTool(d.name), status: 'running', ...parsedDraft };
                        setWorkspaceLiveDraft(draft);
                        if (allowWorkspaceAutoSwitch(draft.path)) setWorkspaceActivePath(draft.path!);
                        if (isFocusPath(draft.path)) openAwarePanel();
                        else if (allowLivePanelAutoFocus()) {
                            setSidePanelTab('workspace');
                            setLivePanelVisible(true);
                            collapseSidebarsForLivePanel();
                        }
                    } else if (d.status === 'done') {
                        setWorkspaceLiveDraft(null);
                    }
                }
                if (d.live_preview) {
                    const lp = d.live_preview;
                    setLiveState((prev: any) => {
                        const next = { ...prev };
                        if ((lp.env === 'desktop' || lp.env === 'browser') && lp.screenshot_url) {
                            if (lp.env === 'desktop') next.desktop = { screenshotUrl: lp.screenshot_url };
                            else next.browser = { screenshotUrl: lp.screenshot_url };
                            if (allowLivePanelAutoFocus()) setSidePanelTab(lp.env === 'desktop' ? 'desktop' : 'browser');
                        } else if (lp.env === 'code' && lp.output) {
                            const existing = prev.code?.output || '';
                            next.code = { output: existing + (existing ? '\n---\n' : '') + lp.output };
                            if (allowLivePanelAutoFocus()) setSidePanelTab('code');
                        }
                        return next;
                    });
                    if (allowLivePanelAutoFocus()) {
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
                if (d.workspace_activity) {
                    const activity = d.workspace_activity;
                    setWorkspaceLiveDraft(null);
                    setWorkspaceActivities((prev: any[]) => [activity, ...prev.filter((item: any) => item.path !== activity.path)].slice(0, 20));
                    if (activity.action === 'delete' && activity.ok !== false && !activity.pendingApproval) handleWorkspacePathDeletedRef.current(activity.path);
                    if (activity.action !== 'delete' && activity.ok !== false && allowWorkspaceAutoSwitch(activity.path)) setWorkspaceActivePath(activity.path);
                    if (isFocusPath(activity.path)) {
                        openAwarePanel();
                        refetchFocusItems();
                        queryClient.invalidateQueries({ queryKey: ['focus', id] });
                    } else if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('workspace');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                    queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                }
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId }).messages as any[]);
                if (d.status === 'done') {
                    const currentSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                    if (currentSessionId) clearUnreadForSession(currentSessionId);
                    queryClient.invalidateQueries({ queryKey: ['agents'] });
                }
            } else if (d.type === 'user_message_committed') {
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId }).messages as any[]);
            } else if (d.type === 'assistant_message_committed') {
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId, preserveTransient: !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot }).messages as any[]);
            } else if (d.type === 'channel_user_message') {
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId }).messages as any[]);
                const cuSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                if (cuSessionId) clearUnreadForSession(cuSessionId);
            } else if (d.type === 'chunk') {
                enqueueChatStreamEvent({ type: 'chunk', content: d.content || '', messageId: d.message_id ? String(d.message_id) : undefined, turnAnchorId: d.turn?.turn_anchor_id ? String(d.turn.turn_anchor_id) : undefined, turnGeneration: Number.isInteger(d.turn?.generation) ? Number(d.turn.generation) : undefined, producerScope: d.producer_scope ? String(d.producer_scope) : undefined });
            } else if (d.type === 'done') {
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId, preserveTransient: !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot }).messages as any[]);
                const currentSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                if (currentSessionId) clearUnreadForSession(currentSessionId);
                fetchMySessions(true, agentId);
                if (canViewAllAgentChatSessions && (chat.scopeDropdownOpen || chatScope === 'all' || allSessions.length > 0)) fetchAllSessions(true, false, agentId);
                queryClient.invalidateQueries({ queryKey: ['agents'] });
            } else if (d.type === 'error' || d.type === 'quota_exceeded') {
                setChatMessages((prev: any[]) => foldConversationTimelineEvent(prev as any, d, { makeId: createClientId }).messages as any[]);
                const msg = d.content || d.detail || d.message || 'Request denied';
                const isNoModelError = msg.includes('no LLM model') || msg.includes('No model');
                if (isNoModelError) {
                    reconnectDisabledRef.current[key] = true;
                    return;
                }
                setChatMessages((prev: any[]) => {
                    const last = prev[prev.length - 1];
                    const warningText = `Warning: ${msg}`;
                    if (last && last.role === 'assistant' && last.content === warningText) return prev;
                    return [...prev, parseChatMsgRef.current({ msg: { role: 'assistant', content: warningText }, id, activeSession })];
                });
                if (msg.includes('expired') || msg.includes('Setup failed')) {
                    reconnectDisabledRef.current[key] = true;
                    if (msg.includes('expired')) setAgentExpired(true);
                }
            } else if (d.type === 'trigger_notification') {
                const targetSessionId = d.session_id ? String(d.session_id) : '';
                const currentSessionId = activeSessionIdRef.current ? String(activeSessionIdRef.current) : '';
                if (targetSessionId && currentSessionId === targetSessionId) {
                    setChatMessages((prev: any[]) => [...prev, parseChatMsgRef.current({ msg: { role: 'assistant', content: d.content }, id, activeSession })]);
                    clearUnreadForSession(targetSessionId);
                }
                fetchMySessions(true, agentId);
                queryClient.invalidateQueries({ queryKey: ['agents'] });
            } else if (d.type === 'info') {
                setChatInfoMsg(d.content || '');
                if (chatInfoTimerRef.current) clearTimeout(chatInfoTimerRef.current);
                chatInfoTimerRef.current = setTimeout(() => setChatInfoMsg(null), 6000);
            } else if (d.type === 'agentbay_live') {
                if ((d.env === 'desktop' || d.env === 'browser') && d.screenshot_url) {
                    setLiveState((prev: any) => ({ ...prev, [d.env]: { screenshotUrl: d.screenshot_url } }));
                    if (allowLivePanelAutoFocus()) {
                        setSidePanelTab(d.env === 'desktop' ? 'desktop' : 'browser');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                } else if (d.env === 'code' && d.output) {
                    setLiveState((prev: any) => ({
                        ...prev,
                        code: { output: appendLiveCodeOutput(prev.code?.output || '', `${d.stream === 'stderr' ? '⚠️ ' : ''}${d.output}`) },
                    }));
                    if (allowLivePanelAutoFocus()) {
                        setSidePanelTab('code');
                        setLivePanelVisible(true);
                        collapseSidebarsForLivePanel();
                    }
                }
            } else if (['user', 'assistant', 'system'].includes(String(d.role || '')) && typeof d.content === 'string') {
                setChatMessages((prev: any[]) => [...prev, parseChatMsgRef.current({ msg: { role: d.role, content: d.content }, id, activeSession })]);
            }
        };
        ws.onmessage = (e) => {
            if (wsMapRef.current[key] !== ws) return;
            const d = JSON.parse(e.data);
            if (d.type !== 'connected' && bufferResumeEvent(pcResumeEventGateRef.current, key, { data: d, consume: handleSocketMessage })) return;
            handleSocketMessage(d);
        };
        return ws;
    };

    chat.ensureSessionSocketRef.current = ensureSessionSocket;
    dispatchChatMessageRef.current = dispatchChatMessageRef.current;

    useEffect(() => {
        return () => {
            discardChatStreamBatch();
            discardMonitorStreamBatch();
        };
    }, [discardChatStreamBatch, discardMonitorStreamBatch]);

    return {
        ensureSessionSocket,
        flushMonitorStreamBatch,
        discardMonitorStreamBatch,
        enqueueMonitorStreamEvent,
    };
}
