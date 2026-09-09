import type React from 'react';
import { useCallback, useEffect, useRef } from 'react';
import { chatSessionApi, fileApi } from '../../../services/api';
import {
    normalizeChatAttachmentFields,
    extractChatImageDataMarkers,
} from '../../../utils/chatAttachments';
import {
    CONVERSATION_HISTORY_RECONCILE_TURN_PAGE_SIZE,
    createConversationHistoryPageParams,
    resolveConversationHistoryHasMore,
} from '../../../features/conversation/historyPagination';
import {
    prepareMessagesForActiveTurnResume,
    shouldCompleteRecoveryPolling,
} from '../../../features/conversation/core/resumeRecovery';
import {
    latestHistoryWindowOverlaps,
    mapHistoryMessage,
    mergeHistoryMessages,
    normalizeChatTimelineMessages,
    reconcileLatestHistoryWindow,
    type H5ChatMessage,
} from '../chatTimeline';
import {
    H5_SESSION_PAGE_SIZE,
    makeId,
    normalizeH5SessionSummary,
    type H5SessionSummary,
} from './model';
import type { useH5AgentChatState } from './useH5AgentChatState';

export function useH5AgentChatHistory(state: ReturnType<typeof useH5AgentChatState>) {
    const {
        agentId,
        token,
        historyLoadedSessionRef,
        historyLoadRef,
        olderHistoryLoadRef,
        historyPaginationSessionRef,
        historyOldestCursorRef,
        setHistoryHasMore,
        setHistoryLoadingOlder,
        historyLoadGenerationRef,
        messagesSnapshotRef,
        sessionIdRef,
        setMessages,
        messagesScrollerRef,
        historyHasMore,
        historyLoadingOlder,
        recoveryPollingGenerationRef,
        recoveryPollTimerRef,
        recoveryPollingNeededRef,
        pageSuspendedRef,
        unmountedRef,
        resumeReconcilePromiseRef,
        resumeCleanupNeededRef,
        wsRef,
        beginResumeEventGate,
        finishResumeEventGate,
        releaseResumeReconcileOwner,
        authStatus,
        agent,
        initialSessionId,
        initialHistoryRequestedRef,
        sessionsRequestGenerationRef,
        sessionsLoadingMoreRef,
        setSessionsLoading,
        setSessionsLoadingMore,
        setSessionsError,
        setSessions,
        setSessionsHasMore,
        sessionsLoading,
        sessionsHasMore,
        sessions,
        channel,
    } = state;

    const normalizeHistoryMessage = useCallback((row: any): H5ChatMessage | null => {
        const msg = mapHistoryMessage(row, makeId);
        const hasStructuredAttachments = Object.prototype.hasOwnProperty.call(row || {}, 'attachments');
        if (!msg || !agentId || (msg.role !== 'user' && !(msg.role === 'assistant' && hasStructuredAttachments))) return msg;
        const activeSource = sessions.find((item) => item.id === sessionIdRef.current)?.source_channel || channel;
        const normalized = normalizeChatAttachmentFields({
            raw: msg as Record<string, any>,
            sourceChannel: activeSource,
            buildDownloadUrl: (path, inline) => fileApi.downloadUrl(agentId, path, { inline }),
        });
        const markerImages = normalized.previewImages.length === 0
            ? extractChatImageDataMarkers(msg.content)
            : [];
        const images = normalized.previewImages.length > 0 ? normalized.previewImages : markerImages;
        return {
            ...msg,
            content: normalized.displayContent,
            attachments: normalized.attachments,
            fileName: normalized.fileName || msg.fileName,
            previewImages: images.length > 0 ? images : undefined,
            imageUrl: images.length === 1 ? images[0].src : undefined,
        };
    }, [agentId, channel, sessions]);

    const loadHistory = useCallback((
        nextSessionId: string,
        options: { prepareActiveTurnResume?: boolean } = {},
    ): Promise<boolean> => {
        if (!agentId || !token) return Promise.resolve(false);
        if (historyLoadedSessionRef.current === nextSessionId) return Promise.resolve(true);
        const current = historyLoadRef.current;
        if (current?.sessionId === nextSessionId) return current.promise;
        current?.controller.abort();

        const startsNewPagination = historyPaginationSessionRef.current !== nextSessionId;
        if (startsNewPagination) {
            olderHistoryLoadRef.current?.abort();
            olderHistoryLoadRef.current = null;
            historyPaginationSessionRef.current = nextSessionId;
            historyOldestCursorRef.current = null;
            setHistoryHasMore(false);
            setHistoryLoadingOlder(false);
        }

        const controller = new AbortController();
        const historyTimeout = window.setTimeout(() => controller.abort(), 10000);
        const generation = ++historyLoadGenerationRef.current;
        const promise = (async () => {
            try {
                const currentMessages = messagesSnapshotRef.current;
                let collectedRows: any[] = [];
                let responseCursor: string | null = null;
                let responseHasMore: string | null = null;
                let overlapFound = startsNewPagination || currentMessages.length === 0;
                let before: string | null = null;

                do {
                    const params = createConversationHistoryPageParams(
                        before,
                        !startsNewPagination && !before
                            ? CONVERSATION_HISTORY_RECONCILE_TURN_PAGE_SIZE
                            : undefined,
                    );
                    const response = await fetch(`/api/agents/${agentId}/sessions/${nextSessionId}/message-turns?${params}`, {
                        headers: { Authorization: `Bearer ${token}` },
                        signal: controller.signal,
                    });
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    const pageCursor = response.headers.get('X-Message-Next-Cursor');
                    const pageHasMore = response.headers.get('X-Message-Has-More');
                    const pageRows = await response.json();
                    if (
                        controller.signal.aborted
                        || generation !== historyLoadGenerationRef.current
                        || sessionIdRef.current !== nextSessionId
                    ) return false;
                    const safePageRows = Array.isArray(pageRows) ? pageRows : [];
                    safePageRows.forEach((row) => state.hostContext.acknowledge({ ...row, type: 'history' }));
                    collectedRows = [...safePageRows, ...collectedRows];
                    responseCursor = pageCursor;
                    responseHasMore = pageHasMore;

                    if (!startsNewPagination && safePageRows.length > 0) {
                        const normalizedPage = safePageRows
                            .map(normalizeHistoryMessage)
                            .filter(Boolean) as H5ChatMessage[];
                        overlapFound = latestHistoryWindowOverlaps(currentMessages, normalizedPage);
                    }
                    const hasMore = resolveConversationHistoryHasMore(pageHasMore);
                    if (overlapFound || !hasMore || !pageCursor || pageCursor === before) break;
                    before = pageCursor;
                } while (true);

                const normalized = collectedRows
                    .map(normalizeHistoryMessage)
                    .filter(Boolean) as H5ChatMessage[];
                const history = normalizeChatTimelineMessages(normalized);
                setMessages((prev) => {
                    const prepared = options.prepareActiveTurnResume
                        ? prepareMessagesForActiveTurnResume(prev)
                        : prev;
                    return startsNewPagination
                        ? mergeHistoryMessages(prepared, history)
                        : reconcileLatestHistoryWindow(prepared, history);
                });
                if (startsNewPagination || !overlapFound || !historyOldestCursorRef.current) {
                    const oldestRow = collectedRows[0];
                    const oldestCursor = responseCursor || (oldestRow?.created_at
                        ? `${oldestRow.created_at}${oldestRow.id ? `|${oldestRow.id}` : ''}`
                        : null);
                    historyOldestCursorRef.current = oldestCursor;
                    setHistoryHasMore(Boolean(
                        oldestCursor
                        && resolveConversationHistoryHasMore(responseHasMore)
                    ));
                }
                historyLoadedSessionRef.current = nextSessionId;
                return true;
            } catch (error: any) {
                if (error?.name !== 'AbortError') console.warn('Failed to load H5 chat history', error);
                return false;
            } finally {
                window.clearTimeout(historyTimeout);
                if (historyLoadRef.current?.controller === controller) historyLoadRef.current = null;
            }
        })();
        historyLoadRef.current = { sessionId: nextSessionId, controller, promise };
        return promise;
    }, [agentId, normalizeHistoryMessage, token, state.hostContext]);

    const loadOlderHistory = useCallback(async () => {
        const activeSessionId = sessionIdRef.current;
        const before = historyOldestCursorRef.current;
        if (
            !agentId
            || !token
            || !activeSessionId
            || !before
            || !historyHasMore
            || historyLoadingOlder
            || olderHistoryLoadRef.current
        ) return;

        const controller = new AbortController();
        olderHistoryLoadRef.current = controller;
        setHistoryLoadingOlder(true);
        const scroller = messagesScrollerRef.current;
        const previousScrollHeight = scroller?.scrollHeight ?? 0;
        const previousScrollTop = scroller?.scrollTop ?? 0;
        let anchorRestoreScheduled = false;

        try {
            const params = createConversationHistoryPageParams(before);
            const response = await fetch(`/api/agents/${agentId}/sessions/${activeSessionId}/message-turns?${params}`, {
                headers: { Authorization: `Bearer ${token}` },
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const responseCursor = response.headers.get('X-Message-Next-Cursor');
            const responseHasMore = response.headers.get('X-Message-Has-More');
            const rows = await response.json();
            if (
                controller.signal.aborted
                || sessionIdRef.current !== activeSessionId
                || historyPaginationSessionRef.current !== activeSessionId
            ) return;
            if (!Array.isArray(rows) || rows.length === 0) {
                setHistoryHasMore(false);
                return;
            }

            const normalized = rows
                .map(normalizeHistoryMessage)
                .filter(Boolean) as H5ChatMessage[];
            const olderPage = normalizeChatTimelineMessages(normalized);
            // The stable cursor guarantees this page is strictly older than the
            // current window. Prepend it directly so distinct messages with the
            // same role/content remain distinct.
            setMessages((prev) => {
                const newerMessageIds = new Set(prev.map((message) => message.id).filter(Boolean));
                const distinctOlderPage = olderPage.filter(
                    (message) => !message.id || !newerMessageIds.has(message.id),
                );
                return normalizeChatTimelineMessages([...distinctOlderPage, ...prev]);
            });

            const oldestRow = rows[0];
            const nextCursor = responseCursor || (oldestRow?.created_at
                ? `${oldestRow.created_at}${oldestRow.id ? `|${oldestRow.id}` : ''}`
                : null);
            historyOldestCursorRef.current = nextCursor;
            setHistoryHasMore(Boolean(
                nextCursor
                && nextCursor !== before
                && resolveConversationHistoryHasMore(responseHasMore)
            ));

            anchorRestoreScheduled = true;
            window.requestAnimationFrame(() => {
                window.requestAnimationFrame(() => {
                    if (scroller && sessionIdRef.current === activeSessionId) {
                        scroller.scrollTop = previousScrollTop + scroller.scrollHeight - previousScrollHeight;
                    }
                    if (olderHistoryLoadRef.current === controller) olderHistoryLoadRef.current = null;
                    if (!controller.signal.aborted && sessionIdRef.current === activeSessionId) {
                        setHistoryLoadingOlder(false);
                    }
                });
            });
        } catch (error: any) {
            if (error?.name !== 'AbortError') console.warn('Failed to load older H5 chat history', error);
        } finally {
            if (!anchorRestoreScheduled) {
                if (olderHistoryLoadRef.current === controller) olderHistoryLoadRef.current = null;
                if (!controller.signal.aborted && sessionIdRef.current === activeSessionId) {
                    setHistoryLoadingOlder(false);
                }
            }
        }
    }, [agentId, historyHasMore, historyLoadingOlder, normalizeHistoryMessage, token]);

    const loadHistoryRef = useRef(loadHistory);
    useEffect(() => {
        loadHistoryRef.current = loadHistory;
    }, [loadHistory]);

    const cancelRecoveryPolling = useCallback(() => {
        recoveryPollingGenerationRef.current += 1;
        if (recoveryPollTimerRef.current !== null) {
            window.clearTimeout(recoveryPollTimerRef.current);
            recoveryPollTimerRef.current = null;
        }
    }, []);

    const startRecoveryPolling = useCallback(() => {
        cancelRecoveryPolling();
        const pollingGeneration = recoveryPollingGenerationRef.current;
        const delays = [1000, 2000, 4000, 8000, 16000, 30000];
        let attempt = 0;
        let successfulLoads = 0;
        const poll = () => {
            const activeSessionId = sessionIdRef.current;
            if (
                pollingGeneration !== recoveryPollingGenerationRef.current
                || !recoveryPollingNeededRef.current
                || pageSuspendedRef.current
                || unmountedRef.current
                || document.visibilityState === 'hidden'
                || !activeSessionId
            ) return;
            if (resumeReconcilePromiseRef.current) {
                if (pollingGeneration !== recoveryPollingGenerationRef.current) return;
                recoveryPollTimerRef.current = window.setTimeout(poll, 500);
                return;
            }
            historyLoadedSessionRef.current = null;
            const gate = beginResumeEventGate(activeSessionId);
            let loadedSuccessfully = false;
            const promise = loadHistoryRef.current(activeSessionId, {
                prepareActiveTurnResume: resumeCleanupNeededRef.current,
            }).then((loaded) => {
                loadedSuccessfully = loaded;
                if (loaded) {
                    successfulLoads += 1;
                    resumeCleanupNeededRef.current = false;
                }
            }).finally(() => {
                const stillActive = !pageSuspendedRef.current
                    && !unmountedRef.current
                    && !document.hidden
                    && sessionIdRef.current === activeSessionId;
                finishResumeEventGate(gate, stillActive);
                releaseResumeReconcileOwner(promise, activeSessionId);
                if (pollingGeneration !== recoveryPollingGenerationRef.current) return;
                if (shouldCompleteRecoveryPolling({
                    pollingGeneration,
                    currentGeneration: recoveryPollingGenerationRef.current,
                    loadedSuccessfully,
                    successfulLoads,
                    stillActive,
                    socketReadyState: wsRef.current?.readyState,
                })) {
                    recoveryPollingNeededRef.current = false;
                    recoveryPollTimerRef.current = null;
                    return;
                }
                if (!recoveryPollingNeededRef.current || attempt >= delays.length) return;
                recoveryPollTimerRef.current = window.setTimeout(poll, delays[attempt++]);
            });
            resumeReconcilePromiseRef.current = promise;
        };
        recoveryPollTimerRef.current = window.setTimeout(poll, delays[attempt++]);
    }, [beginResumeEventGate, cancelRecoveryPolling, finishResumeEventGate, releaseResumeReconcileOwner]);

    useEffect(() => {
        if (
            authStatus !== 'ready'
            || !agent
            || !token
            || !initialSessionId
            || initialHistoryRequestedRef.current === initialSessionId
        ) return;
        initialHistoryRequestedRef.current = initialSessionId;
        sessionIdRef.current = initialSessionId;
        void loadHistoryRef.current(initialSessionId);
    }, [agent, authStatus, initialSessionId, token]);

    const loadSessions = useCallback(async () => {
        if (!agentId) return;
        const generation = ++sessionsRequestGenerationRef.current;
        sessionsLoadingMoreRef.current = false;
        setSessionsLoading(true);
        setSessionsLoadingMore(false);
        setSessionsError('');
        try {
            const rows = await chatSessionApi.list(agentId, {
                scope: 'mine',
                limit: H5_SESSION_PAGE_SIZE + 1,
                offset: 0,
            });
            if (generation !== sessionsRequestGenerationRef.current) return;
            const pageRows = rows.slice(0, H5_SESSION_PAGE_SIZE);
            const next = pageRows.map(normalizeH5SessionSummary).filter(Boolean) as H5SessionSummary[];
            setSessions(next);
            setSessionsHasMore(rows.length > H5_SESSION_PAGE_SIZE);
        } catch (error: any) {
            if (generation !== sessionsRequestGenerationRef.current) return;
            setSessionsError(error?.message || '无法加载历史会话');
        } finally {
            if (generation === sessionsRequestGenerationRef.current) setSessionsLoading(false);
        }
    }, [agentId]);

    const loadMoreSessions = useCallback(async () => {
        if (
            !agentId
            || sessionsLoading
            || sessionsLoadingMoreRef.current
            || !sessionsHasMore
        ) return;

        const generation = sessionsRequestGenerationRef.current;
        const offset = sessions.length;
        sessionsLoadingMoreRef.current = true;
        setSessionsLoadingMore(true);
        setSessionsError('');
        try {
            const rows = await chatSessionApi.list(agentId, {
                scope: 'mine',
                limit: H5_SESSION_PAGE_SIZE + 1,
                offset,
            });
            if (generation !== sessionsRequestGenerationRef.current) return;
            const pageRows = rows.slice(0, H5_SESSION_PAGE_SIZE);
            const next = pageRows.map(normalizeH5SessionSummary).filter(Boolean) as H5SessionSummary[];
            setSessions((current) => {
                const knownIds = new Set(current.map((item) => item.id));
                return [...current, ...next.filter((item) => !knownIds.has(item.id))];
            });
            setSessionsHasMore(rows.length > H5_SESSION_PAGE_SIZE);
        } catch (error: any) {
            if (generation === sessionsRequestGenerationRef.current) {
                setSessionsError(error?.message || '无法加载更多历史会话');
            }
        } finally {
            if (generation === sessionsRequestGenerationRef.current) {
                sessionsLoadingMoreRef.current = false;
                setSessionsLoadingMore(false);
            }
        }
    }, [agentId, sessions.length, sessionsHasMore, sessionsLoading]);

    const handleSessionsScroll = useCallback((event: React.UIEvent<HTMLDivElement>) => {
        const scroller = event.currentTarget;
        const distanceToBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
        if (distanceToBottom <= 120) void loadMoreSessions();
    }, [loadMoreSessions]);

    return {
        cancelRecoveryPolling,
        handleSessionsScroll,
        loadHistory,
        loadHistoryRef,
        loadOlderHistory,
        loadSessions,
        normalizeHistoryMessage,
        startRecoveryPolling,
    };
}
