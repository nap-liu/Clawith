import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { SubagentRunCardData } from '../../../components/SubagentRunCard';
import {
    createOrReuseResumeEventGate,
    drainResumeEventGate,
    shouldScheduleResumeReconnect,
    type ResumeEventGate,
} from '../../../features/conversation/core/resumeRecovery';
import {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    type ConversationTurnRuntime,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import { useToast } from '../../../components/Toast/ToastProvider';
import { useAuthStore } from '../../../stores';
import { fileApi, type SceneManifest } from '../../../services/api';
import type { Agent } from '../../../types';
import {
    downloadChatAttachment,
    type ChatAttachedFile,
    type ChatModelOption,
    type ChatPreviewImage,
} from '../../../utils/chatAttachments';
import {
    foldConversationTimelineEvent,
    hasPendingConfirmation,
    type H5AssistantStreamMessage,
    type H5ChatMessage,
} from '../chatTimeline';
import { parseH5Theme } from '../h5Params';
import { parseChatSessionId } from '../../../utils/chatUrlParams';
import {
    detectH5ContainerRuntime,
    type H5ContainerRuntime,
} from '../../../utils/h5ContainerRuntime';
import { insertSpeechTranscript, useSpeechInput } from '../../../hooks/useSpeechInput';
import type { OnboardingKickoffRequest } from '../../../hooks/useOnboardingKickoff';
import { resolveThemeMode } from '../../../utils/themeMode';
import {
    QUICK_ACTIONS_MENU_CLOSE_MS,
    STREAM_BATCH_DELAY_MS,
    type AuthStatus,
    type BufferedH5SocketEvent,
    type ConnectionStatus,
    type H5SessionSummary,
    type H5UploadDraft,
    makeId,
} from './model';

export function useH5AgentChatState() {
    const { t } = useTranslation();
    const { agentId } = useParams<{ agentId: string }>();
    const [searchParams] = useSearchParams();
    const toast = useToast();
    const searchString = searchParams.toString();
    const channel = useMemo(() => {
        const params = new URLSearchParams(searchString);
        return params.get('channel') || 'miniprogram';
    }, [searchString]);
    const provider = useMemo(() => new URLSearchParams(searchString).get('provider') || '', [searchString]);
    const code = useMemo(() => new URLSearchParams(searchString).get('code') || '', [searchString]);
    const oauthState = useMemo(() => new URLSearchParams(searchString).get('state'), [searchString]);
    const themeMode = useMemo(() => parseH5Theme(new URLSearchParams(searchString).get('theme')), [searchString]);
    const initialSessionId = useMemo(() => parseChatSessionId(new URLSearchParams(searchString).get('session_id')), [searchString]);
    const sceneKey = useMemo(() => new URLSearchParams(searchString).get('scene') || '', [searchString]);
    const [resolvedTheme, setResolvedTheme] = useState(() => resolveThemeMode(themeMode));
    const [containerRuntime, setContainerRuntime] = useState<H5ContainerRuntime | 'detecting'>('detecting');

    const token = useAuthStore((s) => s.token);
    const setAuth = useAuthStore((s) => s.setAuth);

    const [authStatus, setAuthStatus] = useState<AuthStatus>('checking');
    const [authError, setAuthError] = useState('');
    const [agent, setAgent] = useState<Agent | null>(null);
    const [agentError, setAgentError] = useState('');
    const [sceneManifest, setSceneManifest] = useState<SceneManifest | null>(null);
    const [quickActionsOverflow, setQuickActionsOverflow] = useState(false);
    const [quickActionsMenuOpen, setQuickActionsMenuOpen] = useState(false);
    const [quickActionsMenuClosing, setQuickActionsMenuClosing] = useState(false);
    const [quickActionSearch, setQuickActionSearch] = useState('');
    const [messages, setMessages] = useState<H5ChatMessage[]>([]);
    const [historyHasMore, setHistoryHasMore] = useState(false);
    const [historyLoadingOlder, setHistoryLoadingOlder] = useState(false);
    const confirmationPending = useMemo(
        () => hasPendingConfirmation(messages),
        [messages],
    );
    const [input, setInput] = useState('');
    const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
    const [sessionsPanelOpen, setSessionsPanelOpen] = useState(false);
    const [sessions, setSessions] = useState<H5SessionSummary[]>([]);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [sessionsLoadingMore, setSessionsLoadingMore] = useState(false);
    const [sessionsHasMore, setSessionsHasMore] = useState(false);
    const [sessionsError, setSessionsError] = useState('');
    const [llmModels, setLlmModels] = useState<ChatModelOption[]>([]);
    const [tenantDefaultModelId, setTenantDefaultModelId] = useState<string | null>(null);
    const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('idle');
    const [isReadOnly, setIsReadOnly] = useState(false);
    const [pageResumeRevision, setPageResumeRevision] = useState(0);
    const [pageActive, setPageActive] = useState(() => document.visibilityState !== 'hidden');
    const [onboardingKickoffRequest, setOnboardingKickoffRequest] = useState<OnboardingKickoffRequest | null>(null);
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    const [isStopping, setIsStopping] = useState(false);
    const [isStartingNew, setIsStartingNew] = useState(false);
    const [isSwitchingSession, setIsSwitchingSession] = useState(false);
    const [analysisExpanded, setAnalysisExpanded] = useState<Record<string, boolean>>({});
    const [uploadDrafts, setUploadDrafts] = useState<H5UploadDraft[]>([]);
    const [attachedFiles, setAttachedFiles] = useState<ChatAttachedFile[]>([]);
    const [uploadError, setUploadError] = useState('');
    const [imagePreview, setImagePreview] = useState<{ images: ChatPreviewImage[]; index: number } | null>(null);
    const [subagentSessionRun, setSubagentSessionRun] = useState<SubagentRunCardData | null>(null);
    const openSubagentSession = useCallback((run: SubagentRunCardData) => setSubagentSessionRun(run), []);
    const closeSubagentSession = useCallback(() => setSubagentSessionRun(null), []);
    const [unavailableAttachmentKeys, setUnavailableAttachmentKeys] = useState<Set<string>>(() => new Set());

    const wsRef = useRef<WebSocket | null>(null);
    const chatRootRef = useRef<HTMLElement | null>(null);
    const messagesSnapshotRef = useRef(messages);
    messagesSnapshotRef.current = messages;
    const sceneManifestRef = useRef<SceneManifest | null>(null);
    const sceneManifestRequestRef = useRef(0);
    const quickActionActivationRef = useRef(false);
    const sessionIdRef = useRef<string | null>(initialSessionId);
    const reconnectTimerRef = useRef<number | null>(null);
    const resumeReconnectTimerRef = useRef<number | null>(null);
    const recoveryPollTimerRef = useRef<number | null>(null);
    const recoveryPollingGenerationRef = useRef(0);
    const recoveryPollingNeededRef = useRef(false);
    const nativeNavigationFallbackTimerRef = useRef<number | null>(null);
    const socketConnectTimerRef = useRef<number | null>(null);
    const reconnectAttemptRef = useRef(0);
    const pageSuspendedRef = useRef(
        typeof document !== 'undefined' && document.visibilityState === 'hidden',
    );
    const generationActiveRef = useRef(false);
    const turnRuntimeBySessionRef = useRef<Record<string, ConversationTurnRuntime>>({});
    const hiddenTerminalEventRef = useRef(false);
    const hiddenDroppedEventRef = useRef(false);
    const unmountedRef = useRef(false);
    const messagesScrollerRef = useRef<HTMLDivElement | null>(null);
    const sessionsScrollerRef = useRef<HTMLDivElement | null>(null);
    const sessionsRequestGenerationRef = useRef(0);
    const sessionsLoadingMoreRef = useRef(false);
    const quickActionsRef = useRef<HTMLDivElement | null>(null);
    const quickActionsMenuCloseTimerRef = useRef<number | null>(null);
    const messageDispatchLockedRef = useRef(false);
    const messageRuntimeBlockedRef = useRef(false);
    const initialHistoryRequestedRef = useRef<string | null>(null);
    const historyLoadedSessionRef = useRef<string | null>(null);
    const resumeAutoFollowRef = useRef<() => void>(() => undefined);
    const cancelAutoFollowRef = useRef<() => void>(() => undefined);
    const historyLoadGenerationRef = useRef(0);
    const historyLoadRef = useRef<{
        sessionId: string;
        controller: AbortController;
        promise: Promise<boolean>;
    } | null>(null);
    const historyPaginationSessionRef = useRef<string | null>(null);
    const historyOldestCursorRef = useRef<string | null>(null);
    const olderHistoryLoadRef = useRef<AbortController | null>(null);
    const streamBatchRef = useRef<H5AssistantStreamMessage[]>([]);
    const streamBatchTimerRef = useRef<number | null>(null);
    const resumeEventGateRef = useRef<ResumeEventGate<BufferedH5SocketEvent> | null>(null);
    const resumeEventGateGenerationRef = useRef(0);
    const resumeEventConsumerRef = useRef<(event: BufferedH5SocketEvent) => void>(() => undefined);
    const resumeCleanupNeededRef = useRef(false);
    const resumeReconcilePromiseRef = useRef<Promise<void> | null>(null);
    const recoverActiveSessionRef = useRef<() => void>(() => undefined);
    const scheduleReconnectRef = useRef<() => void>(() => undefined);
    generationActiveRef.current = isWaiting || isStreaming || isStopping;

    const beginResumeEventGate = useCallback((runtimeKey: string) => {
        const activeGate = resumeEventGateRef.current;
        const nextGeneration = resumeEventGateGenerationRef.current + 1;
        const { gate, displacedEvents } = createOrReuseResumeEventGate<BufferedH5SocketEvent>(
            activeGate,
            runtimeKey,
            nextGeneration,
        );
        if (gate !== activeGate) resumeEventGateGenerationRef.current = nextGeneration;
        resumeEventGateRef.current = gate;
        displacedEvents.forEach((event) => resumeEventConsumerRef.current(event));
        return gate;
    }, []);

    const finishResumeEventGate = useCallback((
        gate: ResumeEventGate<BufferedH5SocketEvent>,
        replay: boolean,
    ) => {
        if (resumeEventGateRef.current !== gate) return;
        resumeEventGateRef.current = null;
        const events = drainResumeEventGate(gate);
        if (replay) events.forEach((event) => resumeEventConsumerRef.current(event));
    }, []);

    const releaseResumeReconcileOwner = useCallback((
        owner: Promise<void>,
        ownerSessionId: string,
    ) => {
        if (resumeReconcilePromiseRef.current !== owner) return;
        resumeReconcilePromiseRef.current = null;
        if (sessionIdRef.current !== ownerSessionId) return;
        if (shouldScheduleResumeReconnect({
            pageSuspended: pageSuspendedRef.current,
            unmounted: unmountedRef.current,
            hidden: document.hidden,
            socketReadyState: wsRef.current?.readyState,
        })) {
            scheduleReconnectRef.current();
        }
    }, []);

    const cancelHistoryLoad = useCallback(() => {
        historyLoadGenerationRef.current += 1;
        historyLoadRef.current?.controller.abort();
        historyLoadRef.current = null;
        olderHistoryLoadRef.current?.abort();
        olderHistoryLoadRef.current = null;
        setHistoryLoadingOlder(false);
    }, []);

    const discardStreamBatch = useCallback(() => {
        if (streamBatchTimerRef.current !== null) {
            window.clearTimeout(streamBatchTimerRef.current);
            streamBatchTimerRef.current = null;
        }
        streamBatchRef.current = [];
    }, []);

    const flushStreamBatch = useCallback(() => {
        if (streamBatchTimerRef.current !== null) {
            window.clearTimeout(streamBatchTimerRef.current);
            streamBatchTimerRef.current = null;
        }
        const batch = streamBatchRef.current;
        if (batch.length === 0) return;
        streamBatchRef.current = [];
        setMessages((prev) => batch.reduce(
            (next, event) => foldConversationTimelineEvent(next, {
                type: event.type,
                content: event.content,
                message_id: event.messageId,
                producer_scope: event.producerScope,
                transient_message_id: event.transientMessageId,
                turn: event.turnAnchorId ? {
                    turn_anchor_id: event.turnAnchorId,
                    generation: event.turnGeneration,
                } : undefined,
            }, { makeId, now: event.now }).messages,
            prev,
        ));
    }, []);

    const enqueueStreamEvent = useCallback((event: H5AssistantStreamMessage) => {
        const last = streamBatchRef.current[streamBatchRef.current.length - 1];
        if (last && last.type === event.type && last.messageId === event.messageId
            && last.turnAnchorId === event.turnAnchorId
            && last.producerScope === event.producerScope) {
            last.content = `${last.content || ''}${event.content || ''}`;
        } else {
            streamBatchRef.current.push({ ...event });
        }
        if (streamBatchTimerRef.current === null) {
            streamBatchTimerRef.current = window.setTimeout(flushStreamBatch, STREAM_BATCH_DELAY_MS);
        }
    }, [flushStreamBatch]);

    useEffect(() => {
        setUnavailableAttachmentKeys(new Set());
    }, [sessionId]);

    const markAttachmentUnavailable = useCallback((key: string) => {
        setUnavailableAttachmentKeys((current) => {
            if (current.has(key)) return current;
            const next = new Set(current);
            next.add(key);
            return next;
        });
    }, []);

    const handleAttachmentDownload = useCallback(async (path: string, name: string) => {
        if (!agentId) return;
        try {
            await downloadChatAttachment(fileApi.downloadUrl(agentId, path), name);
        } catch {
            markAttachmentUnavailable(path);
        }
    }, [agentId, markAttachmentUnavailable]);
    const textareaRef = useRef<HTMLTextAreaElement | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);
    const uploadAbortRef = useRef<Map<string, () => void>>(new Map());
    const skipNextConnectedHistoryRef = useRef<string | null>(null);
    const speechInputSnapshotRef = useRef({ value: '', selectionStart: 0, selectionEnd: 0 });
    const speechSelectionCapturedRef = useRef(false);
    const inputSelectionRef = useRef({ start: 0, end: 0, hasPosition: false });
    const containerRuntimeDetectionRef = useRef<Promise<H5ContainerRuntime> | null>(null);

    const openQuickActionsMenu = useCallback(() => {
        if (quickActionsMenuCloseTimerRef.current !== null) {
            window.clearTimeout(quickActionsMenuCloseTimerRef.current);
            quickActionsMenuCloseTimerRef.current = null;
        }
        setQuickActionsMenuClosing(false);
        setQuickActionsMenuOpen(true);
    }, []);

    const closeQuickActionsMenu = useCallback(() => {
        if (!quickActionsMenuOpen || quickActionsMenuClosing) return;
        setQuickActionsMenuClosing(true);
        quickActionsMenuCloseTimerRef.current = window.setTimeout(() => {
            setQuickActionsMenuOpen(false);
            setQuickActionsMenuClosing(false);
            quickActionsMenuCloseTimerRef.current = null;
        }, QUICK_ACTIONS_MENU_CLOSE_MS);
    }, [quickActionsMenuClosing, quickActionsMenuOpen]);

    useEffect(() => () => {
        if (quickActionsMenuCloseTimerRef.current !== null) {
            window.clearTimeout(quickActionsMenuCloseTimerRef.current);
        }
    }, []);

    const ensureContainerRuntime = useCallback(() => {
        if (!containerRuntimeDetectionRef.current) {
            containerRuntimeDetectionRef.current = detectH5ContainerRuntime()
                .catch((error) => {
                    console.warn('H5 container runtime detection failed', error);
                    return 'standard' as H5ContainerRuntime;
                });
        }
        return containerRuntimeDetectionRef.current;
    }, []);

    const speechTextAtCursor = useCallback((text: string) => {
        const snapshot = speechInputSnapshotRef.current;
        return insertSpeechTranscript(snapshot.value, text, snapshot.selectionStart, snapshot.selectionEnd);
    }, []);

    const restoreInputCaret = useCallback((position: number) => {
        inputSelectionRef.current = { start: position, end: position, hasPosition: true };
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
                textareaRef.current?.setSelectionRange(position, position);
            });
        });
    }, []);

    const handleSpeechInterim = useCallback((text: string) => {
        setInput(speechTextAtCursor(text).value);
    }, [speechTextAtCursor]);
    const handleSpeechFinal = useCallback((text: string) => {
        const next = speechTextAtCursor(text);
        setInput(next.value);
        restoreInputCaret(next.caret);
    }, [restoreInputCaret, speechTextAtCursor]);
    const handleSpeechCancel = useCallback(() => {
        const snapshot = speechInputSnapshotRef.current;
        setInput(snapshot.value);
        restoreInputCaret(snapshot.selectionStart);
    }, [restoreInputCaret]);
    const speech = useSpeechInput({
        onInterim: handleSpeechInterim,
        onFinal: handleSpeechFinal,
        onCancel: handleSpeechCancel,
    });
    const startSpeech = speech.start;

    const captureSpeechInsertionPoint = useCallback(() => {
        const selection = inputSelectionRef.current;
        const field = textareaRef.current;
        const selectionStart = selection.hasPosition && field ? field.selectionStart : input.length;
        const selectionEnd = selection.hasPosition && field ? field.selectionEnd : input.length;
        speechInputSnapshotRef.current = { value: input, selectionStart, selectionEnd };
        speechSelectionCapturedRef.current = true;
    }, [input]);

    const startSpeechInput = useCallback(() => {
        if (!speechSelectionCapturedRef.current) captureSpeechInsertionPoint();
        speechSelectionCapturedRef.current = false;
        void startSpeech();
    }, [captureSpeechInsertionPoint, startSpeech]);

    const handleInputSelect = (event: React.SyntheticEvent<HTMLTextAreaElement>) => {
        const field = event.currentTarget;
        inputSelectionRef.current = {
            start: field.selectionStart,
            end: field.selectionEnd,
            hasPosition: true,
        };
    };

    const clearSocketConnectTimer = useCallback(() => {
        if (socketConnectTimerRef.current === null) return;
        window.clearTimeout(socketConnectTimerRef.current);
        socketConnectTimerRef.current = null;
    }, []);

    const closeCurrentSocket = useCallback(() => {
        clearSocketConnectTimer();
        const socket = wsRef.current;
        const runtimeKey = String(
            (socket as any)?._runtimeSessionId || sessionIdRef.current || '',
        );
        if (runtimeKey) {
            turnRuntimeBySessionRef.current[runtimeKey] =
                beginConversationTurnRecovery(
                    turnRuntimeBySessionRef.current[runtimeKey] || IDLE_CONVERSATION_TURN,
                );
        }
        wsRef.current = null;
        if (socket && socket.readyState < WebSocket.CLOSING) {
            socket.close();
        }
    }, [clearSocketConnectTimer]);

    return {
        t,
        agentId,
        searchParams,
        toast,
        searchString,
        channel,
        provider,
        code,
        oauthState,
        themeMode,
        initialSessionId,
        sceneKey,
        resolvedTheme,
        setResolvedTheme,
        containerRuntime,
        setContainerRuntime,
        token,
        setAuth,
        authStatus,
        setAuthStatus,
        authError,
        setAuthError,
        agent,
        setAgent,
        agentError,
        setAgentError,
        sceneManifest,
        setSceneManifest,
        quickActionsOverflow,
        setQuickActionsOverflow,
        quickActionsMenuOpen,
        setQuickActionsMenuOpen,
        quickActionsMenuClosing,
        setQuickActionsMenuClosing,
        quickActionSearch,
        setQuickActionSearch,
        messages,
        setMessages,
        historyHasMore,
        setHistoryHasMore,
        historyLoadingOlder,
        setHistoryLoadingOlder,
        confirmationPending,
        input,
        setInput,
        sessionId,
        setSessionId,
        sessionsPanelOpen,
        setSessionsPanelOpen,
        sessions,
        setSessions,
        sessionsLoading,
        setSessionsLoading,
        sessionsLoadingMore,
        setSessionsLoadingMore,
        sessionsHasMore,
        setSessionsHasMore,
        sessionsError,
        setSessionsError,
        llmModels,
        setLlmModels,
        tenantDefaultModelId,
        setTenantDefaultModelId,
        connectionStatus,
        setConnectionStatus,
        isReadOnly,
        setIsReadOnly,
        pageResumeRevision,
        setPageResumeRevision,
        pageActive,
        setPageActive,
        onboardingKickoffRequest,
        setOnboardingKickoffRequest,
        isWaiting,
        setIsWaiting,
        isStreaming,
        setIsStreaming,
        isStopping,
        setIsStopping,
        isStartingNew,
        setIsStartingNew,
        isSwitchingSession,
        setIsSwitchingSession,
        analysisExpanded,
        setAnalysisExpanded,
        uploadDrafts,
        setUploadDrafts,
        attachedFiles,
        setAttachedFiles,
        uploadError,
        setUploadError,
        imagePreview,
        setImagePreview,
        subagentSessionRun,
        setSubagentSessionRun,
        openSubagentSession,
        closeSubagentSession,
        unavailableAttachmentKeys,
        setUnavailableAttachmentKeys,
        wsRef,
        chatRootRef,
        messagesSnapshotRef,
        sceneManifestRef,
        sceneManifestRequestRef,
        quickActionActivationRef,
        sessionIdRef,
        reconnectTimerRef,
        resumeReconnectTimerRef,
        recoveryPollTimerRef,
        recoveryPollingGenerationRef,
        recoveryPollingNeededRef,
        nativeNavigationFallbackTimerRef,
        socketConnectTimerRef,
        reconnectAttemptRef,
        pageSuspendedRef,
        generationActiveRef,
        turnRuntimeBySessionRef,
        hiddenTerminalEventRef,
        hiddenDroppedEventRef,
        unmountedRef,
        messagesScrollerRef,
        sessionsScrollerRef,
        sessionsRequestGenerationRef,
        sessionsLoadingMoreRef,
        quickActionsRef,
        quickActionsMenuCloseTimerRef,
        messageDispatchLockedRef,
        messageRuntimeBlockedRef,
        initialHistoryRequestedRef,
        historyLoadedSessionRef,
        resumeAutoFollowRef,
        cancelAutoFollowRef,
        historyLoadGenerationRef,
        historyLoadRef,
        historyPaginationSessionRef,
        historyOldestCursorRef,
        olderHistoryLoadRef,
        streamBatchRef,
        streamBatchTimerRef,
        resumeEventGateRef,
        resumeEventGateGenerationRef,
        resumeEventConsumerRef,
        resumeCleanupNeededRef,
        resumeReconcilePromiseRef,
        recoverActiveSessionRef,
        scheduleReconnectRef,
        beginResumeEventGate,
        finishResumeEventGate,
        releaseResumeReconcileOwner,
        cancelHistoryLoad,
        discardStreamBatch,
        flushStreamBatch,
        enqueueStreamEvent,
        markAttachmentUnavailable,
        handleAttachmentDownload,
        textareaRef,
        fileInputRef,
        uploadAbortRef,
        skipNextConnectedHistoryRef,
        speechInputSnapshotRef,
        speechSelectionCapturedRef,
        inputSelectionRef,
        containerRuntimeDetectionRef,
        openQuickActionsMenu,
        closeQuickActionsMenu,
        ensureContainerRuntime,
        speechTextAtCursor,
        restoreInputCaret,
        handleSpeechInterim,
        handleSpeechFinal,
        handleSpeechCancel,
        speech,
        startSpeech,
        captureSpeechInsertionPoint,
        startSpeechInput,
        handleInputSelect,
        clearSocketConnectTimer,
        closeCurrentSocket,
    };
}
