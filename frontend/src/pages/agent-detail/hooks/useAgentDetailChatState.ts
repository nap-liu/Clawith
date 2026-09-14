import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { LivePreviewState } from '../../../components/AgentBayLivePanel';
import type { SidePanelTab } from '../../../components/AgentSidePanel';
import type { WorkspaceActivity, WorkspaceLiveDraft } from '../../../components/WorkspaceOperationPanel';
import { chatSessionApi, fileApi } from '../../../services/api';
import { useAppStore, useAuthStore } from '../../../stores';
import {
    buildPreviewImagesFromAttachments,
    downloadChatAttachment,
    type ChatAttachedFile,
    type ChatMessageAttachment,
    type ChatPreviewImage,
    type ChatQuotedMessage,
} from '../../../utils/chatAttachments';
import { createClientId } from '../../../utils/clientId';
import { foldConversationTimelineEvent, upsertToolCallMessage as mergeToolCallMessage } from '../../../features/conversation/core/chatTimeline';
import {
    IDLE_CONVERSATION_TURN,
    type ConversationTurnRuntime,
} from '../../../features/conversation/core/conversationTurnLifecycle';
import {
    createOrReuseResumeEventGate,
    drainResumeEventGate,
    type ResumeEventGate,
} from '../../../features/conversation/core/resumeRecovery';
import { isPendingConfirmationToolCall } from '../shared';

export function useAgentDetailChatState({
    id,
    agent,
    currentUser,
    setOnboardingKickoffRequest,
    onboardingRequestsRef,
    skipNextSessionUrlRestoreRef,
    writeSessionIdToUrl,
    livePanelVisible,
    setLivePanelVisible,
    sidePanelTab,
    setSidePanelTab,
}: any) {
    const [sessions, setSessions] = useState<any[]>([]);
    const [allSessions, setAllSessions] = useState<any[]>([]);
    const [sessionsHasMore, setSessionsHasMore] = useState(false);
    const [allSessionsHasMore, setAllSessionsHasMore] = useState(false);
    const [sessionsNextCursor, setSessionsNextCursor] = useState<string | null>(null);
    const [allSessionsNextCursor, setAllSessionsNextCursor] = useState<string | null>(null);
    const [activeSession, setActiveSession] = useState<any | null>(null);
    const [subagentSessionRun, setSubagentSessionRun] = useState<any | null>(null);
    const openSubagentSession = useCallback((run: any) => setSubagentSessionRun(run), []);
    const closeSubagentSession = useCallback(() => setSubagentSessionRun(null), []);
    const { data: activeSessionExecution = null } = useQuery({
        queryKey: ['session-execution', id, activeSession?.id],
        queryFn: () => chatSessionApi.execution(id, activeSession.id).catch(() => null),
        enabled: !!id && !!activeSession?.id,
        refetchInterval: activeSession?.id ? 10000 : false,
    });
    const [chatScope, setChatScope] = useState<'mine' | 'all'>('mine');
    const [scopeDropdownOpen, setScopeDropdownOpen] = useState(false);
    const scopeDropdownRef = useRef<HTMLDivElement>(null);
    const [historyMsgs, setHistoryMsgs] = useState<any[]>([]);
    const [historyOldestTs, setHistoryOldestTs] = useState<string | null>(null);
    const [historyHasMore, setHistoryHasMore] = useState(true);
    const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [allSessionsLoading, setAllSessionsLoading] = useState(false);
    const [sessionsLoadingMore, setSessionsLoadingMore] = useState(false);
    const [allSessionsLoadingMore, setAllSessionsLoadingMore] = useState(false);
    const [agentExpired, setAgentExpired] = useState(false);
    const token = useAuthStore((s) => s.token);
    const isAgentOwner = currentUser?.id != null && agent?.creator_id != null && String(agent.creator_id) === String(currentUser.id);
    const isPlatformAdmin = currentUser?.role === 'platform_admin' || !!currentUser?.is_platform_admin;
    const canViewAllAgentChatSessions = isPlatformAdmin
        || currentUser?.role === 'org_admin'
        || agent?.access_level === 'manage'
        || isAgentOwner;
    const wsMapRef = useRef<Record<string, WebSocket>>({});
    const reconnectTimerRef = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
    const reconnectDisabledRef = useRef<Record<string, boolean>>({});
    const reconnectAttemptsRef = useRef<Record<string, number>>({});
    const sessionUiStateRef = useRef<Record<string, { isWaiting: boolean; isStreaming: boolean; isStopping: boolean }>>({});
    const sessionTurnRuntimeRef = useRef<Record<string, ConversationTurnRuntime>>({});
    const activeSessionIdRef = useRef<string | null>(null);
    const activeReadOnlyRef = useRef<boolean>(false);
    const currentAgentIdRef = useRef<string | undefined>(id);
    const sessionMsgAbortRef = useRef<AbortController | null>(null);
    const historyMoreAbortRef = useRef<AbortController | null>(null);
    const sessionsListAbortRef = useRef<AbortController | null>(null);
    const allSessionsListAbortRef = useRef<AbortController | null>(null);
    const sessionsListGenerationRef = useRef(0);
    const allSessionsListGenerationRef = useRef(0);
    const sessionLoadSeqRef = useRef(0);
    const pcPageSuspendedRef = useRef(false);
    const pcHiddenDroppedEventRef = useRef(false);
    const pcRecoveryPollingNeededRef = useRef(false);
    const pcRecoveryPollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const pcRecoveryPollingGenerationRef = useRef(0);
    const startPcRecoveryPollingRef = useRef<(session: any, scope: 'mine' | 'all') => void>(() => undefined);
    type BufferedPcSocketEvent = { data: any; consume: (data: any) => void };
    const pcResumeEventGateRef = useRef<ResumeEventGate<BufferedPcSocketEvent> | null>(null);
    const pcResumeGenerationRef = useRef(0);
    const pcResumeReconcilePromiseRef = useRef<Promise<void> | null>(null);
    const ensureSessionSocketRef = useRef<(session: any, agentId: string, authToken: string) => WebSocket | undefined>(() => undefined);
    const pcResumeHadActiveTurnRef = useRef(false);
    const cancelPcAutoFollowRef = useRef<() => void>(() => undefined);
    const [pcPageActive, setPcPageActive] = useState(() => !document.hidden);
    const [pcResumeMeasurementKey, setPcResumeMeasurementKey] = useState<number | null>(null);

    const buildSessionRuntimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;
    const cancelPcRecoveryPolling = () => {
        pcRecoveryPollingGenerationRef.current += 1;
        if (pcRecoveryPollTimerRef.current) clearTimeout(pcRecoveryPollTimerRef.current);
        pcRecoveryPollTimerRef.current = null;
    };
    const clearReconnectTimer = (key: string) => {
        const timer = reconnectTimerRef.current[key];
        if (timer) {
            clearTimeout(timer);
            reconnectTimerRef.current[key] = null;
        }
    };
    const beginPcResumeEventGate = (runtimeKey: string) => {
        const activeGate = pcResumeEventGateRef.current;
        const nextGeneration = pcResumeGenerationRef.current + 1;
        const { gate, displacedEvents } = createOrReuseResumeEventGate(activeGate, runtimeKey, nextGeneration);
        if (gate !== activeGate) pcResumeGenerationRef.current = nextGeneration;
        pcResumeEventGateRef.current = gate;
        displacedEvents.forEach(({ data, consume }) => consume(data));
        return gate;
    };
    const finishPcResumeEventGate = (gate: ResumeEventGate<BufferedPcSocketEvent>, replay: boolean) => {
        if (pcResumeEventGateRef.current !== gate) return;
        pcResumeEventGateRef.current = null;
        const events = drainResumeEventGate(gate);
        if (!replay) return;
        events.forEach(({ data, consume }) => consume(data));
    };
    const releasePcResumeReconcileOwner = (owner: Promise<void>, session: any, ownerAgentId: string, ownerToken: string) => {
        if (pcResumeReconcilePromiseRef.current !== owner) return;
        pcResumeReconcilePromiseRef.current = null;
        const runtimeKey = buildSessionRuntimeKey(ownerAgentId, String(session.id));
        if (String(currentAgentIdRef.current || '') !== ownerAgentId || String(activeSessionIdRef.current || '') !== String(session.id)) return;
        reconnectDisabledRef.current[runtimeKey] = false;
        ensureSessionSocketRef.current(session, ownerAgentId, ownerToken);
    };
    const closeSessionSocket = (key: string, disableReconnect = true) => {
        if (disableReconnect) reconnectDisabledRef.current[key] = true;
        clearReconnectTimer(key);
        reconnectAttemptsRef.current[key] = 0;
        const ws = wsMapRef.current[key];
        if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
        delete wsMapRef.current[key];
        delete sessionUiStateRef.current[key];
        delete sessionTurnRuntimeRef.current[key];
    };
    const setSessionUiState = (key: string, next: Partial<{ isWaiting: boolean; isStreaming: boolean; isStopping: boolean }>) => {
        const prev = sessionUiStateRef.current[key] || { isWaiting: false, isStreaming: false, isStopping: false };
        sessionUiStateRef.current[key] = { ...prev, ...next };
    };
    const sessionUserIdStr = (s: any) => (s?.user_id == null ? '' : String(s.user_id));
    const viewerUserIdStr = () => (currentUser?.id == null ? '' : String(currentUser.id));
    const isAgentChatSession = (s: any) => String(s?.source_channel || '').toLowerCase() === 'agent' || String(s?.participant_type || '').toLowerCase() === 'agent';
    const normalizeChatSession = (sess: any) => {
        if (!sess || typeof sess !== 'object') return sess;
        const vu = viewerUserIdStr();
        const rawUid = sess.user_id != null && String(sess.user_id).trim() !== '' ? String(sess.user_id) : vu;
        return {
            ...sess,
            id: String(sess.id),
            agent_id: sess.agent_id != null ? String(sess.agent_id) : sess.agent_id,
            user_id: rawUid,
            unread_count: Number(sess.unread_count || 0),
            is_primary: Boolean(sess.is_primary),
            source_channel: typeof sess.source_channel === 'string' && sess.source_channel.trim() ? sess.source_channel : 'web',
            participant_type: typeof sess.participant_type === 'string' && sess.participant_type.trim() ? sess.participant_type : 'user',
            is_group: Boolean(sess.is_group),
        };
    };
    const clearUnreadForSession = (sessionId?: string | null) => {
        if (!sessionId) return;
        const sid = String(sessionId);
        setSessions((prev) => prev.map((item: any) => String(item.id) === sid ? { ...item, unread_count: 0 } : item));
        setAllSessions((prev) => prev.map((item: any) => String(item.id) === sid ? { ...item, unread_count: 0 } : item));
        setActiveSession((prev: any) => prev && String(prev.id) === sid ? { ...prev, unread_count: 0 } : prev);
    };
    const isWritableSession = (sess: any, scopeOverride: 'mine' | 'all' = chatScope) => {
        if (!sess) return false;
        const sc = String(sess.source_channel || 'web').toLowerCase();
        const pt = String(sess.participant_type || 'user').toLowerCase();
        if (sc === 'agent' || sc === 'subagent' || pt === 'agent') return false;
        if (sess.is_group) return false;
        if (scopeOverride === 'all') return false;
        const su = sessionUserIdStr(sess);
        const vu = viewerUserIdStr();
        if (su && vu && su !== vu) return false;
        return true;
    };
    const isViewingOtherUsersSessions = canViewAllAgentChatSessions && chatScope === 'all';
    const othersListForPicker = allSessions;
    useEffect(() => {
        if (!canViewAllAgentChatSessions && chatScope === 'all') setChatScope('mine');
    }, [canViewAllAgentChatSessions, chatScope]);
    useEffect(() => {
        if (!scopeDropdownOpen) return;
        const handler = (e: MouseEvent) => {
            if (scopeDropdownRef.current && !scopeDropdownRef.current.contains(e.target as Node)) setScopeDropdownOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [scopeDropdownOpen]);

    interface ChatMsg {
        role: 'user' | 'assistant' | 'tool_call';
        content: string;
        display_content?: string;
        attachments?: ChatMessageAttachment[];
        quoted_message?: ChatQuotedMessage;
        id?: string;
        fileName?: string;
        toolName?: string;
        toolCallId?: string;
        toolArgs?: any;
        toolStatus?: 'running' | 'done';
        toolResult?: string;
        toolThinking?: string;
        thinking?: string;
        streaming?: boolean;
        _streaming?: boolean;
        imageUrl?: string;
        previewImages?: ChatPreviewImage[];
        timestamp?: string;
        sender_name?: string;
        sender_user_id?: string;
        sender_agent_id?: string;
        turnAnchorId?: string;
        turnGeneration?: number;
        producerScope?: string;
        confirmationToolCalls?: ChatMsg[];
    }
    const [chatMessages, setChatMessages] = useState<ChatMsg[]>([]);
    const chatMessagesSnapshotRef = useRef<ChatMsg[]>(chatMessages);
    const historyMsgsSnapshotRef = useRef<any[]>(historyMsgs);
    chatMessagesSnapshotRef.current = chatMessages;
    historyMsgsSnapshotRef.current = historyMsgs;
    const chatStreamBatchRef = useRef<Array<{ type: 'thinking' | 'chunk'; content: string; messageId?: string; turnAnchorId?: string; turnGeneration?: number; producerScope?: string }>>([]);
    const chatStreamBatchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const flushChatStreamBatch = useCallback(() => {
        if (chatStreamBatchTimerRef.current) {
            clearTimeout(chatStreamBatchTimerRef.current);
            chatStreamBatchTimerRef.current = null;
        }
        const batch = chatStreamBatchRef.current;
        if (batch.length === 0) return;
        chatStreamBatchRef.current = [];
        setChatMessages((prev) => batch.reduce((next, event) => foldConversationTimelineEvent(next as any, {
            type: event.type,
            content: event.content,
            message_id: event.messageId,
            producer_scope: event.producerScope,
            turn: event.turnAnchorId ? { turn_anchor_id: event.turnAnchorId, generation: event.turnGeneration } : undefined,
        }, { makeId: createClientId }).messages as ChatMsg[], prev));
    }, []);
    const discardChatStreamBatch = useCallback(() => {
        if (chatStreamBatchTimerRef.current) clearTimeout(chatStreamBatchTimerRef.current);
        chatStreamBatchTimerRef.current = null;
        chatStreamBatchRef.current = [];
    }, []);
    const enqueueChatStreamEvent = useCallback((event: { type: 'thinking' | 'chunk'; content: string; messageId?: string; turnAnchorId?: string; turnGeneration?: number; producerScope?: string }) => {
        const last = chatStreamBatchRef.current[chatStreamBatchRef.current.length - 1];
        if (last && last.type === event.type && last.messageId === event.messageId && last.turnAnchorId === event.turnAnchorId && last.producerScope === event.producerScope) last.content += event.content;
        else chatStreamBatchRef.current.push({ ...event });
        if (!chatStreamBatchTimerRef.current) chatStreamBatchTimerRef.current = setTimeout(flushChatStreamBatch, 40);
    }, [flushChatStreamBatch]);
    const confirmationPending = chatMessages.some(isPendingConfirmationToolCall);
    const upsertToolCallMessage = (toolMsg: ChatMsg) => {
        setChatMessages((prev) => mergeToolCallMessage(prev as any, toolMsg as any) as ChatMsg[]);
    };
    const [chatInfoMsg, setChatInfoMsg] = useState<string | null>(null);
    const chatInfoTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [liveState, setLiveState] = useState<LivePreviewState>({});
    const [workspaceActivePath, setWorkspaceActivePath] = useState<string | null>(null);
    const [workspaceLockedPath, setWorkspaceLockedPath] = useState<string | null>(null);
    const [workspaceActivities, setWorkspaceActivities] = useState<WorkspaceActivity[]>([]);
    const [workspaceLiveDraft, setWorkspaceLiveDraft] = useState<WorkspaceLiveDraft | null>(null);
    const workspaceEditingRef = useRef(false);
    const workspaceLockedPathRef = useRef<string | null>(null);
    const [wsSessionId, setWsSessionId] = useState<string>('');
    const [sessionListCollapsed, setSessionListCollapsed] = useState(false);
    const livePanelAutoCollapsedRef = useRef(false);
    const [chatInput, setChatInput] = useState('');
    const [wsConnected, setWsConnected] = useState(false);
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    const [isStopping, setIsStopping] = useState(false);
    const [chatUploadDrafts, setChatUploadDrafts] = useState<{ id: string; name: string; percent: number; previewUrl?: string; sizeBytes: number }[]>([]);
    const chatUploadAbortRef = useRef<Map<string, () => void>>(new Map());
    type PendingChatMessage = {
        runtimeKey: string;
        contentForLLM: string;
        displayContent: string;
        fileName: string;
        imageUrl?: string;
        previewImages: ChatPreviewImage[];
        attachments: ChatMessageAttachment[];
        modelId?: string | null;
        reasoningEffort?: string | null;
        messageId: string;
    };
    const [attachedFiles, setAttachedFiles] = useState<ChatAttachedFile[]>([]);
    const attachedImagePreviews = useMemo(() => buildPreviewImagesFromAttachments(attachedFiles), [attachedFiles]);
    const [chatImagePreview, setChatImagePreview] = useState<{ images: ChatPreviewImage[]; index: number } | null>(null);
    const [unavailableAttachmentKeys, setUnavailableAttachmentKeys] = useState<Set<string>>(() => new Set());
    const dismissedWorkspaceRefPath = useRef<string | null>(null);
    const pendingChatSendRef = useRef<PendingChatMessage | null>(null);
    const wsRef = useRef<WebSocket | null>(null);
    const chatContainerRef = useRef<HTMLDivElement>(null);
    const chatInputRef = useRef<HTMLTextAreaElement>(null);
    const chatInputAreaRef = useRef<HTMLDivElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    useEffect(() => {
        setUnavailableAttachmentKeys(new Set());
    }, [activeSession?.id]);
    const markAttachmentUnavailable = useCallback((key: string) => {
        setUnavailableAttachmentKeys((current) => {
            if (current.has(key)) return current;
            const next = new Set(current);
            next.add(key);
            return next;
        });
    }, []);
    const handleAttachmentDownload = useCallback(async (path: string, name: string) => {
        if (!id) return;
        try {
            await downloadChatAttachment(fileApi.downloadUrl(id, path), name);
        } catch {
            markAttachmentUnavailable(path);
        }
    }, [id, markAttachmentUnavailable]);
    const workspacePreviewLocked = !!workspaceLockedPath;
    useEffect(() => {
        workspaceLockedPathRef.current = workspaceLockedPath;
    }, [workspaceLockedPath]);
    const allowWorkspaceAutoSwitch = useCallback((path?: string | null) => {
        if (!path) return false;
        if (workspaceEditingRef.current) return false;
        if (!workspaceLockedPathRef.current) return true;
        return workspaceLockedPathRef.current === path;
    }, []);
    const allowLivePanelAutoFocus = useCallback(() => !workspaceEditingRef.current && !workspaceLockedPathRef.current, []);
    const handleWorkspaceSelectPath = useCallback((path: string) => {
        setWorkspaceActivePath(path);
        if (workspaceLockedPath) setWorkspaceLockedPath(path);
    }, [workspaceLockedPath]);
    const handleWorkspaceToggleLock = useCallback(() => {
        setWorkspaceLockedPath((current) => current ? null : workspaceActivePath);
    }, [workspaceActivePath]);
    const handleWorkspaceEditingChange = useCallback((editing: boolean) => {
        workspaceEditingRef.current = editing;
    }, []);
    const collapseSidebarsForLivePanel = useCallback(() => {
        if (livePanelAutoCollapsedRef.current) return;
        livePanelAutoCollapsedRef.current = true;
        setSessionListCollapsed(true);
        useAppStore.setState({ sidebarCollapsed: true });
    }, []);
    useEffect(() => {
        if (!livePanelVisible) livePanelAutoCollapsedRef.current = false;
    }, [livePanelVisible]);
    const togglePreviewPanel = useCallback((tab: SidePanelTab) => {
        setLivePanelVisible((visible: boolean) => {
            if (visible && sidePanelTab === tab) {
                livePanelAutoCollapsedRef.current = false;
                return false;
            }
            setSidePanelTab(tab);
            collapseSidebarsForLivePanel();
            return true;
        });
    }, [collapseSidebarsForLivePanel, sidePanelTab]);
    const openAwarePanel = useCallback(() => {
        if (!allowLivePanelAutoFocus()) return;
        setSidePanelTab('aware');
        setLivePanelVisible(true);
        collapseSidebarsForLivePanel();
    }, [allowLivePanelAutoFocus, collapseSidebarsForLivePanel]);
    const syncActiveSocketState = (sess: any | null = activeSession, agentId: string | undefined = id) => {
        if (!sess || !agentId) {
            wsRef.current = null;
            setWsConnected(false);
            return;
        }
        const key = buildSessionRuntimeKey(agentId, sess.id);
        const ws = wsMapRef.current[key];
        wsRef.current = ws ?? null;
        if (ws && ws.readyState === WebSocket.OPEN && (ws as any)._serverConnected === true) {
            setWsConnected(true);
            setOnboardingKickoffRequest(onboardingRequestsRef.current[key] || null);
        } else if (!ws || ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED) {
            setWsConnected(false);
            setOnboardingKickoffRequest(null);
        }
    };
    const clearChatSelection = () => {
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setHistoryMsgs([]);
        setWsConnected(false);
        setIsStreaming(false);
        setIsWaiting(false);
        setIsStopping(false);
        skipNextSessionUrlRestoreRef.current = true;
        if (!writeSessionIdToUrl(null)) skipNextSessionUrlRestoreRef.current = false;
    };
    const onAdminTabMine = () => {
        setChatScope('mine');
        if (activeSession && sessionUserIdStr(activeSession) !== viewerUserIdStr()) clearChatSelection();
    };
    const onAdminTabOthers = () => {
        setChatScope('all');
        if (activeSession && sessionUserIdStr(activeSession) === viewerUserIdStr()) clearChatSelection();
    };

    return {
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
        subagentSessionRun,
        openSubagentSession,
        closeSubagentSession,
        activeSessionExecution,
        chatScope,
        setChatScope,
        scopeDropdownOpen,
        setScopeDropdownOpen,
        scopeDropdownRef,
        historyMsgs,
        setHistoryMsgs,
        historyOldestTs,
        setHistoryOldestTs,
        historyHasMore,
        setHistoryHasMore,
        historyLoadingMore,
        setHistoryLoadingMore,
        sessionsLoading,
        setSessionsLoading,
        allSessionsLoading,
        setAllSessionsLoading,
        sessionsLoadingMore,
        setSessionsLoadingMore,
        allSessionsLoadingMore,
        setAllSessionsLoadingMore,
        agentExpired,
        setAgentExpired,
        setOnboardingKickoffRequest,
        token,
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
        sessionMsgAbortRef,
        historyMoreAbortRef,
        sessionsListAbortRef,
        allSessionsListAbortRef,
        sessionsListGenerationRef,
        allSessionsListGenerationRef,
        sessionLoadSeqRef,
        pcPageSuspendedRef,
        pcHiddenDroppedEventRef,
        pcRecoveryPollingNeededRef,
        pcRecoveryPollTimerRef,
        pcRecoveryPollingGenerationRef,
        startPcRecoveryPollingRef,
        pcResumeEventGateRef,
        pcResumeGenerationRef,
        pcResumeReconcilePromiseRef,
        ensureSessionSocketRef,
        pcResumeHadActiveTurnRef,
        cancelPcAutoFollowRef,
        pcPageActive,
        setPcPageActive,
        pcResumeMeasurementKey,
        setPcResumeMeasurementKey,
        buildSessionRuntimeKey,
        cancelPcRecoveryPolling,
        clearReconnectTimer,
        beginPcResumeEventGate,
        finishPcResumeEventGate,
        releasePcResumeReconcileOwner,
        closeSessionSocket,
        setSessionUiState,
        sessionUserIdStr,
        viewerUserIdStr,
        isAgentChatSession,
        normalizeChatSession,
        clearUnreadForSession,
        isWritableSession,
        isViewingOtherUsersSessions,
        othersListForPicker,
        chatMessages,
        setChatMessages,
        chatMessagesSnapshotRef,
        historyMsgsSnapshotRef,
        flushChatStreamBatch,
        discardChatStreamBatch,
        enqueueChatStreamEvent,
        confirmationPending,
        upsertToolCallMessage,
        chatInfoMsg,
        setChatInfoMsg,
        chatInfoTimerRef,
        liveState,
        setLiveState,
        workspaceActivePath,
        setWorkspaceActivePath,
        workspaceLockedPath,
        setWorkspaceLockedPath,
        workspaceActivities,
        setWorkspaceActivities,
        workspaceLiveDraft,
        setWorkspaceLiveDraft,
        workspaceEditingRef,
        workspaceLockedPathRef,
        wsSessionId,
        setWsSessionId,
        sessionListCollapsed,
        setSessionListCollapsed,
        livePanelAutoCollapsedRef,
        chatInput,
        setChatInput,
        wsConnected,
        setWsConnected,
        isWaiting,
        setIsWaiting,
        isStreaming,
        setIsStreaming,
        isStopping,
        setIsStopping,
        chatUploadDrafts,
        setChatUploadDrafts,
        chatUploadAbortRef,
        attachedFiles,
        setAttachedFiles,
        attachedImagePreviews,
        chatImagePreview,
        setChatImagePreview,
        unavailableAttachmentKeys,
        setUnavailableAttachmentKeys,
        dismissedWorkspaceRefPath,
        pendingChatSendRef,
        wsRef,
        chatContainerRef,
        chatInputRef,
        chatInputAreaRef,
        fileInputRef,
        markAttachmentUnavailable,
        handleAttachmentDownload,
        workspacePreviewLocked,
        allowWorkspaceAutoSwitch,
        allowLivePanelAutoFocus,
        handleWorkspaceSelectPath,
        handleWorkspaceToggleLock,
        handleWorkspaceEditingChange,
        collapseSidebarsForLivePanel,
        livePanelVisible,
        setLivePanelVisible,
        sidePanelTab,
        setSidePanelTab,
        togglePreviewPanel,
        openAwarePanel,
        syncActiveSocketState,
        clearChatSelection,
        onAdminTabMine,
        onAdminTabOthers,
    };
}
