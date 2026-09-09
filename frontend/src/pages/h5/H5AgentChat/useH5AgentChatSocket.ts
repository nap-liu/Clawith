import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import {
    bufferResumeEvent,
    resolveVisibleTerminalRecoveryAction,
} from '../../../features/conversation/core/resumeRecovery';
import {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    conversationTurnEventClosesStream,
    conversationTurnEventShouldBeHandled,
    conversationTurnIsRunning,
    conversationTurnIsStreaming,
    conversationTurnIsWaiting,
    reduceConversationTurnEvent,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import { installH5PageLifecycle } from '../../../utils/h5PageLifecycle';
import { writeChatSessionIdToHref } from '../../../utils/chatUrlParams';
import { foldConversationTimelineEvent } from '../chatTimeline';
import { makeId } from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';
import type { useH5AgentChatHistory } from './useH5AgentChatHistory';
import type { useH5AgentChatLifecycle } from './useH5AgentChatLifecycle';

export function useH5AgentChatSocket(
    state: ReturnType<typeof useH5AgentChatState>,
    history: ReturnType<typeof useH5AgentChatHistory>,
    lifecycle: ReturnType<typeof useH5AgentChatLifecycle>,
) {
    const {
        pageSuspendedRef,
        unmountedRef,
        token,
        agentId,
        reconnectTimerRef,
        reconnectAttemptRef,
        wsRef,
        recoveryPollingNeededRef,
        resumeCleanupNeededRef,
        recoverActiveSessionRef,
        sessionIdRef,
        setConnectionStatus,
        scheduleReconnectRef,
        turnRuntimeBySessionRef,
        generationActiveRef,
        setIsWaiting,
        setIsStreaming,
        setIsStopping,
        hiddenDroppedEventRef,
        historyLoadedSessionRef,
        hiddenTerminalEventRef,
        clearSocketConnectTimer,
        setIsReadOnly,
        setOnboardingKickoffRequest,
        setSessionId,
        skipNextConnectedHistoryRef,
        flushStreamBatch,
        setMessages,
        enqueueStreamEvent,
        closeCurrentSocket,
        socketConnectTimerRef,
        resumeEventGateRef,
        resumeReconcilePromiseRef,
        resumeAutoFollowRef,
        cancelAutoFollowRef,
        resumeReconnectTimerRef,
        nativeNavigationFallbackTimerRef,
        setPageActive,
        setPageResumeRevision,
        authStatus,
        agent,
        beginResumeEventGate,
        channel,
        initialSessionId,
        cancelHistoryLoad,
        discardStreamBatch,
        finishResumeEventGate,
        releaseResumeReconcileOwner,
        resumeEventConsumerRef,
        sceneKey,
        hostContext,
        messageRuntimeBlockedRef,
    } = state;
    const {
        cancelRecoveryPolling,
        loadHistory,
        loadHistoryRef,
        normalizeHistoryMessage,
        startRecoveryPolling,
    } = history;
    const { refreshSceneManifest } = lifecycle;

    const reconcileHostOutbox = useCallback((socket: WebSocket, activeSessionId: string) => {
        if (!hostContext.enabled || wsRef.current !== socket || sessionIdRef.current !== activeSessionId) return;
        hostContext.reconcile(socket);
        hostContext.retry(socket, activeSessionId,
            !messageRuntimeBlockedRef.current && !pageSuspendedRef.current
            && !unmountedRef.current && !document.hidden && !(socket as any)._readOnly);
    }, [hostContext]);

    const scheduleReconnect = useCallback(() => {
        if (
            pageSuspendedRef.current
            || unmountedRef.current
            || !token
            || !agentId
            || document.visibilityState === 'hidden'
        ) return;
        if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current);
        const attempt = reconnectAttemptRef.current;
        reconnectAttemptRef.current = attempt + 1;
        const delay = Math.min(12000, 900 * 2 ** attempt);
        reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            const socket = wsRef.current;
            if (socket && (
                socket.readyState === WebSocket.OPEN
                || socket.readyState === WebSocket.CONNECTING
            )) return;
            if (recoveryPollingNeededRef.current || resumeCleanupNeededRef.current) {
                recoverActiveSessionRef.current();
            } else {
                openSocketRef.current(sessionIdRef.current);
            }
        }, delay);
    }, [agentId, token]);

    useEffect(() => {
        scheduleReconnectRef.current = scheduleReconnect;
        return () => {
            scheduleReconnectRef.current = () => undefined;
        };
    }, [scheduleReconnect]);

    const handleSocketMessage = useCallback((data: any, socket: WebSocket) => {
        const runtimeSessionId = String(
            data.session_id || (socket as any)._runtimeSessionId || sessionIdRef.current || '',
        );
        const turnReduction = reduceConversationTurnEvent(
            turnRuntimeBySessionRef.current[runtimeSessionId] || IDLE_CONVERSATION_TURN,
            data,
        );
        if (!conversationTurnEventShouldBeHandled(turnReduction)) return;
        turnRuntimeBySessionRef.current[runtimeSessionId] = turnReduction.runtime;
        if (turnReduction.controlsLifecycle && turnReduction.hasSnapshot) {
            const nextWaiting = conversationTurnIsWaiting(turnReduction.runtime);
            const nextStreaming = conversationTurnIsStreaming(turnReduction.runtime);
            generationActiveRef.current = conversationTurnIsRunning(turnReduction.runtime);
            setIsWaiting(nextWaiting);
            setIsStreaming(nextStreaming);
            if (!generationActiveRef.current) setIsStopping(false);
        }
        const isTerminalEvent = conversationTurnEventClosesStream(turnReduction, data);
        if (pageSuspendedRef.current) {
            hiddenDroppedEventRef.current = true;
            historyLoadedSessionRef.current = null;
            if (isTerminalEvent) {
                hiddenTerminalEventRef.current = true;
                generationActiveRef.current = false;
                // The terminal frame can arrive just before its durable history
                // row is committed. Keep doing a short foreground reconciliation
                // window so resume cannot permanently miss that final row.
                recoveryPollingNeededRef.current = true;
            }
            return;
        }
        if (isTerminalEvent) {
            generationActiveRef.current = false;
            const recoveryAction = resolveVisibleTerminalRecoveryAction({
                isActiveRuntime: true,
                recoveryNeeded: recoveryPollingNeededRef.current,
            });
            if (recoveryAction === 'continue') {
                startRecoveryPolling();
            } else {
                recoveryPollingNeededRef.current = false;
                cancelRecoveryPolling();
            }
        }
        if (data.type !== 'thinking' && data.type !== 'chunk') flushStreamBatch();
        if (data.type === 'scene_manifest') {
            state.sceneManifestRequestRef.current += 1;
            state.sceneManifestRef.current = data.manifest;
            state.setSceneManifest(data.manifest);
            return;
        }
        if (data.type === 'connected' && 'scene_manifest' in data) {
            state.sceneManifestRequestRef.current += 1;
            state.sceneManifestRef.current = data.scene_manifest;
            state.setSceneManifest(data.scene_manifest);
        }
        if (data.type === 'connected' && data.session_id) {
            clearSocketConnectTimer();
            const nextSessionId = String(data.session_id);
            sessionIdRef.current = nextSessionId;
            setSessionId(nextSessionId);
            setConnectionStatus('connected');
            setIsReadOnly(data.read_only === true);
            (socket as any)._readOnly = data.read_only === true;
            void refreshSceneManifest();
            if (data.onboarding_required === true && !hostContext.enabled) {
                generationActiveRef.current = true;
                setIsWaiting(true);
                setIsStreaming(false);
            }
            setOnboardingKickoffRequest({
                sessionId: nextSessionId,
                required: data.onboarding_required === true && !hostContext.enabled,
                socket,
            });
            window.history.replaceState({}, '', writeChatSessionIdToHref(window.location.href, nextSessionId));
            if (skipNextConnectedHistoryRef.current === nextSessionId) {
                skipNextConnectedHistoryRef.current = null;
                if (!resumeEventGateRef.current) reconcileHostOutbox(socket, nextSessionId);
            } else {
                void loadHistory(nextSessionId).then((loaded) => {
                    if (loaded) reconcileHostOutbox(socket, nextSessionId);
                });
            }
            return;
        }

        if (data.type === 'onboarded') {
            setOnboardingKickoffRequest(null);
            return;
        }

        if (data.type === 'onboarding_skipped') {
            setOnboardingKickoffRequest(null);
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            return;
        }

        if (data.type === 'turn_receipt') return;

        if (data.type === 'channel_user_message') {
            setMessages((prev) => foldConversationTimelineEvent(prev, data, { makeId }).messages);
            return;
        }

        if (data.type === 'user_message_committed') {
            setMessages((prev) => foldConversationTimelineEvent(prev, data, { makeId }).messages);
            return;
        }

        if (data.type === 'assistant_message_committed') {
            setMessages((prev) => foldConversationTimelineEvent(prev, data, {
                makeId,
                preserveTransient: !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot,
            }).messages);
            return;
        }

        if (data.type === 'thinking') {
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
                generationActiveRef.current = true;
                setIsWaiting(false);
                setIsStreaming(true);
            }
            enqueueStreamEvent({
                type: 'thinking',
                content: data.content || '',
                messageId: data.message_id ? String(data.message_id) : undefined,
                turnAnchorId: data.turn?.turn_anchor_id ? String(data.turn.turn_anchor_id) : undefined,
                turnGeneration: Number.isInteger(data.turn?.generation) ? Number(data.turn.generation) : undefined,
                producerScope: data.producer_scope ? String(data.producer_scope) : undefined,
            });
            return;
        }

        if (data.type === 'chunk') {
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
                generationActiveRef.current = true;
                setIsWaiting(false);
                setIsStreaming(true);
            }
            enqueueStreamEvent({
                type: 'chunk',
                content: data.content || '',
                messageId: data.message_id ? String(data.message_id) : undefined,
                turnAnchorId: data.turn?.turn_anchor_id ? String(data.turn.turn_anchor_id) : undefined,
                turnGeneration: Number.isInteger(data.turn?.generation) ? Number(data.turn.generation) : undefined,
                producerScope: data.producer_scope ? String(data.producer_scope) : undefined,
            });
            return;
        }

        if (data.type === 'done') {
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
                setIsWaiting(false);
                setIsStreaming(false);
                setIsStopping(false);
            }
            setMessages((prev) => foldConversationTimelineEvent(prev, data, {
                makeId,
                preserveTransient: !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot,
            }).messages);
            return;
        }

        if (data.type === 'tool_call') {
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
                generationActiveRef.current = true;
                setIsWaiting(false);
                setIsStreaming(true);
            }
            setMessages((prev) => foldConversationTimelineEvent(prev, data, { makeId }).messages);
            return;
        }

        if (data.type === 'confirmation_required') {
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
                setIsWaiting(false);
                setIsStreaming(false);
                setIsStopping(false);
            }
            setMessages((prev) => foldConversationTimelineEvent(prev, data, { makeId }).messages);
            return;
        }

        if (data.type === 'error' || data.type === 'quota_exceeded') {
            setMessages((prev) => foldConversationTimelineEvent(prev, data, { makeId }).messages);
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
                setIsWaiting(false);
                setIsStreaming(false);
                setIsStopping(false);
            }
            setMessages((prev) => [...prev, {
                id: makeId(),
                role: 'system',
                content: data.content || '消息发送失败',
                created_at: new Date().toISOString(),
            }]);
        }
    }, [cancelRecoveryPolling, clearSocketConnectTimer, enqueueStreamEvent, flushStreamBatch, loadHistory, normalizeHistoryMessage, refreshSceneManifest, hostContext, reconcileHostOutbox]);

    resumeEventConsumerRef.current = ({ data, socket }) => handleSocketMessage(data, socket);

    const openSocket = useCallback((requestedSessionId?: string | null) => {
        if (
            !agentId
            || !token
            || unmountedRef.current
            || pageSuspendedRef.current
            || document.visibilityState === 'hidden'
        ) return;

        if (reconnectTimerRef.current) {
            window.clearTimeout(reconnectTimerRef.current);
            reconnectTimerRef.current = null;
        }
        closeCurrentSocket();
        setConnectionStatus('connecting');
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const params = new URLSearchParams({
            token,
            lang: navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en',
            channel,
            scene: sceneKey,
        });
        const effectiveSessionId = requestedSessionId || sessionIdRef.current;
        if (effectiveSessionId) params.set('session_id', effectiveSessionId);
        if (hostContext.enabled) params.set('host_context', 'true');

        let ws: WebSocket;
        try {
            ws = new WebSocket(`${protocol}//${window.location.host}/ws/chat/${agentId}?${params.toString()}`);
        } catch {
            setConnectionStatus('disconnected');
            scheduleReconnect();
            return;
        }
        let resolveServerConnected: (connected: boolean) => void = () => undefined;
        (ws as any)._serverConnected = false;
        (ws as any)._serverConnectedPromise = new Promise<boolean>((resolve) => {
            resolveServerConnected = resolve;
        });
        (ws as any)._runtimeSessionId = effectiveSessionId ? String(effectiveSessionId) : '';
        wsRef.current = ws;
        socketConnectTimerRef.current = window.setTimeout(() => {
            if (wsRef.current !== ws) return;
            wsRef.current = null;
            socketConnectTimerRef.current = null;
            if (ws.readyState < WebSocket.CLOSING) ws.close();
            setConnectionStatus('disconnected');
            scheduleReconnect();
        }, 10000);

        ws.onopen = () => {
            if (wsRef.current !== ws) return;
            reconnectAttemptRef.current = 0;
        };

        ws.onmessage = (event) => {
            if (wsRef.current !== ws) return;
            try {
                const data = JSON.parse(event.data);
                hostContext.acknowledge(data);
                if (data.type === 'connected') {
                    (ws as any)._serverConnected = true;
                    if (data.session_id) (ws as any)._runtimeSessionId = String(data.session_id);
                    resolveServerConnected(true);
                }
                const runtimeKey = String((ws as any)._runtimeSessionId || effectiveSessionId || '');
                if (
                    data.type !== 'connected'
                    && bufferResumeEvent(resumeEventGateRef.current, runtimeKey, { data, socket: ws })
                ) return;
                handleSocketMessage(data, ws);
            } catch {
                // Ignore malformed server frames.
            }
        };

        ws.onerror = () => {
            if (wsRef.current !== ws) return;
            setConnectionStatus('disconnected');
        };

        ws.onclose = () => {
            resolveServerConnected(false);
            if (wsRef.current !== ws) return;
            const turnWasActive = generationActiveRef.current;
            const runtimeKey = String(
                (ws as any)._runtimeSessionId || effectiveSessionId || sessionIdRef.current || '',
            );
            const runtimeBeforeClose =
                turnRuntimeBySessionRef.current[runtimeKey] || IDLE_CONVERSATION_TURN;
            turnRuntimeBySessionRef.current[runtimeKey] =
                beginConversationTurnRecovery(runtimeBeforeClose);
            if (turnWasActive) {
                recoveryPollingNeededRef.current = true;
            }
            clearSocketConnectTimer();
            wsRef.current = null;
            historyLoadedSessionRef.current = null;
            setConnectionStatus('disconnected');
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            setOnboardingKickoffRequest(null);
            scheduleReconnect();
            if (turnWasActive && !pageSuspendedRef.current) startRecoveryPolling();
        };
        return ws;
    }, [
        agentId,
        channel,
        clearSocketConnectTimer,
        closeCurrentSocket,
        handleSocketMessage,
        hostContext,
        scheduleReconnect,
        sceneKey,
        startRecoveryPolling,
        token,
    ]);

    const waitForSocketServerConnection = useCallback(async (socket: WebSocket | undefined) => {
        if (!socket) return false;
        if ((socket as any)._serverConnected === true) return true;
        const connection = (socket as any)._serverConnectedPromise as Promise<boolean> | undefined;
        if (!connection) return false;
        let timeout: ReturnType<typeof setTimeout> | null = null;
        try {
            return await Promise.race([
                connection,
                new Promise<boolean>((resolve) => {
                    timeout = setTimeout(() => resolve(false), 5000);
                }),
            ]);
        } finally {
            if (timeout) clearTimeout(timeout);
        }
    }, []);

    const openSocketRef = useRef(openSocket);
    useEffect(() => {
        openSocketRef.current = openSocket;
    }, [openSocket]);

    const recoverActiveSession = useCallback(() => {
        if (resumeReconcilePromiseRef.current) return resumeReconcilePromiseRef.current;
        const activeSessionId = sessionIdRef.current;
        if (
            !activeSessionId
            || pageSuspendedRef.current
            || unmountedRef.current
            || document.hidden
        ) return Promise.resolve();

        cancelRecoveryPolling();
        hiddenDroppedEventRef.current = false;
        hiddenTerminalEventRef.current = false;
        historyLoadedSessionRef.current = null;
        const gate = beginResumeEventGate(activeSessionId);
        const promise = (async () => {
            const existing = wsRef.current;
            let socket = existing && (
                existing.readyState === WebSocket.OPEN
                || existing.readyState === WebSocket.CONNECTING
            ) ? existing : undefined;
            if (!socket) {
                skipNextConnectedHistoryRef.current = activeSessionId;
                socket = openSocketRef.current(activeSessionId);
            }
            await waitForSocketServerConnection(socket);
            if (
                pageSuspendedRef.current
                || unmountedRef.current
                || document.hidden
                || sessionIdRef.current !== activeSessionId
            ) {
                finishResumeEventGate(gate, false);
                return;
            }
            const loaded = await loadHistoryRef.current(activeSessionId, {
                prepareActiveTurnResume: resumeCleanupNeededRef.current,
            });
            if (loaded) resumeCleanupNeededRef.current = false;
            const stillActive = !pageSuspendedRef.current
                && !unmountedRef.current
                && !document.hidden
                && sessionIdRef.current === activeSessionId;
            finishResumeEventGate(gate, stillActive);
            if (loaded && stillActive && socket) reconcileHostOutbox(socket, activeSessionId);
            if (stillActive && recoveryPollingNeededRef.current) startRecoveryPolling();
        })().finally(() => {
            // A reconnect timer can fire while either this coordinator or a
            // recovery poll owns the shared promise. Releasing through one
            // path guarantees the deduped socket retry is never lost.
            releaseResumeReconcileOwner(promise, activeSessionId);
        });
        resumeReconcilePromiseRef.current = promise;
        return promise;
    }, [
        beginResumeEventGate,
        cancelRecoveryPolling,
        finishResumeEventGate,
        releaseResumeReconcileOwner,
        reconcileHostOutbox,
        startRecoveryPolling,
        waitForSocketServerConnection,
    ]);

    useEffect(() => {
        recoverActiveSessionRef.current = () => { void recoverActiveSession(); };
        return () => {
            recoverActiveSessionRef.current = () => undefined;
        };
    }, [recoverActiveSession]);

    useEffect(() => {
        if (authStatus !== 'ready' || !agent || !token) return;
        pageSuspendedRef.current = document.visibilityState === 'hidden';
        openSocket(initialSessionId);
        return closeCurrentSocket;
    }, [agent, authStatus, closeCurrentSocket, initialSessionId, openSocket, token]);

    useLayoutEffect(() => installH5PageLifecycle({
        onSuspend: () => {
            hostContext.cancelPending();
            pageSuspendedRef.current = true;
            setPageActive(false);
            const activeGate = resumeEventGateRef.current;
            if (activeGate) finishResumeEventGate(activeGate, false);
            cancelHistoryLoad();
            discardStreamBatch();
            cancelAutoFollowRef.current();
            cancelRecoveryPolling();
            if (reconnectTimerRef.current) {
                window.clearTimeout(reconnectTimerRef.current);
                reconnectTimerRef.current = null;
            }
            if (resumeReconnectTimerRef.current) {
                window.clearTimeout(resumeReconnectTimerRef.current);
                resumeReconnectTimerRef.current = null;
            }
            if (nativeNavigationFallbackTimerRef.current) {
                window.clearTimeout(nativeNavigationFallbackTimerRef.current);
                nativeNavigationFallbackTimerRef.current = null;
            }
            if (generationActiveRef.current) {
                recoveryPollingNeededRef.current = true;
                resumeCleanupNeededRef.current = true;
            }
            generationActiveRef.current = false;
            historyLoadedSessionRef.current = null;
            closeCurrentSocket();
            setConnectionStatus('disconnected');
            setIsWaiting(false);
            setIsStreaming(false);
            setIsStopping(false);
            setOnboardingKickoffRequest(null);
        },
        onResume: () => {
            pageSuspendedRef.current = false;
            setPageActive(true);
            setPageResumeRevision((revision) => revision + 1);
            if (nativeNavigationFallbackTimerRef.current) {
                window.clearTimeout(nativeNavigationFallbackTimerRef.current);
                nativeNavigationFallbackTimerRef.current = null;
            }
            if (authStatus !== 'ready' || !agent || !token || unmountedRef.current) return;
            if (reconnectTimerRef.current) {
                window.clearTimeout(reconnectTimerRef.current);
                reconnectTimerRef.current = null;
            }
            if (resumeReconnectTimerRef.current) {
                window.clearTimeout(resumeReconnectTimerRef.current);
            }
            if (!sessionIdRef.current) {
                openSocketRef.current(null);
                return;
            }
            resumeReconnectTimerRef.current = window.setTimeout(() => {
                resumeReconnectTimerRef.current = null;
                recoverActiveSessionRef.current();
            }, 50);
        },
    }), [
        agent,
        authStatus,
        cancelHistoryLoad,
        cancelRecoveryPolling,
        closeCurrentSocket,
        discardStreamBatch,
        finishResumeEventGate,
        hostContext,
        token,
    ]);

    const recoverFromNativeNavigation = useCallback(() => {
        if (nativeNavigationFallbackTimerRef.current) {
            window.clearTimeout(nativeNavigationFallbackTimerRef.current);
            nativeNavigationFallbackTimerRef.current = null;
        }
        if (
            document.visibilityState === 'hidden'
            || authStatus !== 'ready'
            || !agent
            || !token
            || unmountedRef.current
        ) return;
        pageSuspendedRef.current = false;
        recoverActiveSessionRef.current();
    }, [agent, authStatus, token]);

    const prepareForNativeNavigation = useCallback(() => {
        hostContext.cancelPending();
        if (reconnectTimerRef.current) {
            window.clearTimeout(reconnectTimerRef.current);
            reconnectTimerRef.current = null;
        }
        historyLoadedSessionRef.current = null;
        cancelHistoryLoad();
        discardStreamBatch();
        closeCurrentSocket();
        setConnectionStatus('disconnected');
        setIsWaiting(false);
        setIsStreaming(false);
        setIsStopping(false);
        setOnboardingKickoffRequest(null);
        if (nativeNavigationFallbackTimerRef.current) {
            window.clearTimeout(nativeNavigationFallbackTimerRef.current);
        }
        // Some WebViews do not emit pagehide when native navigation fails. If the
        // H5 page is still visible, restore its connection instead of stranding it.
        nativeNavigationFallbackTimerRef.current = window.setTimeout(() => {
            nativeNavigationFallbackTimerRef.current = null;
            recoverFromNativeNavigation();
        }, 2000);
    }, [cancelHistoryLoad, closeCurrentSocket, discardStreamBatch, recoverFromNativeNavigation, hostContext]);

    return {
        openSocket,
        prepareForNativeNavigation,
        recoverFromNativeNavigation,
    };
}
