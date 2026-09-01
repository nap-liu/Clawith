import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import {
  applyAssistantDoneMessage,
  applyAssistantMessageCommitted,
  applyAssistantStreamMessage,
  applyConfirmationRequiredEvent,
  applyUserMessageCommitted,
  buildConversationEntries,
  foldConversationTimelineEvent,
  getConversationScrollAnchor,
  mapHistoryMessage,
  mergeHistoryMessages,
  toolCallMessageFromEvent,
  type ConversationMessage,
} from "../features/conversation/core/chatTimeline";
import { resolveConversationMessageAnchor } from "../features/conversation/core/conversationAnchoring";
import {
  IDLE_CONVERSATION_TURN,
  beginConversationTurnRecovery,
  conversationTurnIsRunning,
  conversationTurnEventShouldBeHandled,
  reduceConversationTurnEvent,
  type ConversationTurnRuntime,
} from "../features/conversation/core/conversationTurnLifecycle";
import { useConversationAutoFollow } from "../features/conversation/useConversationAutoFollow";
import { type RichMentionComposerHandle } from "./ui/RichMentionComposer";
import { chatSessionApi, fileApi } from "../services/api";

const TIMELINE_EVENT_TYPES = new Set([
  "turn_receipt",
  "thinking",
  "chunk",
  "done",
  "tool_call",
  "confirmation_required",
  "assistant_message_committed",
  "user_message_committed",
  "channel_user_message",
]);
import {
  normalizeChatAttachmentFields,
  type ChatAttachedFile,
  type ChatPreviewImage,
} from "../utils/chatAttachments";
import SessionViewerReady from "./sessionViewer/SessionViewerReady";
import {
  type GroupTurnState,
  type SessionViewerDrawerProps,
} from "./sessionViewer/model";

export { deriveGroupTurnState } from "./sessionViewer/model";
export type {
  SessionViewerGroupConfig,
  SessionViewerTarget,
} from "./sessionViewer/model";

const ACTIVE_STATUSES = new Set(["queued", "pending", "running", "processing"]);
const SESSION_HISTORY_PAGE_SIZE = 500;
const SESSION_HISTORY_MAX_PAGES = 8;
const SESSION_HISTORY_MAX_MESSAGES = 4_000;
export default function SessionViewerDrawer({
  agentId,
  agentName,
  target,
  routeMode = "pc",
  portalContainer,
  interactive = false,
  embedded = false,
  groupConfig,
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
  const [error, setError] = useState("");
  const [composerError, setComposerError] = useState("");
  const [draft, setDraft] = useState("");
  const [attachedFiles, setAttachedFiles] = useState<ChatAttachedFile[]>([]);
  const [uploads, setUploads] = useState<
    Array<{ id: string; name: string; percent: number }>
  >([]);
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [groupTurn, setGroupTurn] = useState<GroupTurnState | null>(null);
  const [resolvedAnchorMessageId, setResolvedAnchorMessageId] = useState("");
  const [anchorNotice, setAnchorNotice] = useState("");
  const [serverReadOnly, setServerReadOnly] = useState(false);
  const [mentions, setMentions] = useState<string[]>([]);
  const [internalUnavailableAttachments, setInternalUnavailableAttachments] =
    useState<Set<string>>(() => new Set());
  const [internalPreview, setInternalPreview] = useState<{
    images: ChatPreviewImage[];
    index: number;
  } | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const richMentionComposerRef = useRef<RichMentionComposerHandle>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const groupConfigRef = useRef(groupConfig);
  groupConfigRef.current = groupConfig;
  const turnRuntimeBySessionRef = useRef<Record<string, ConversationTurnRuntime>>({});
  const uploadAbortRef = useRef(new Map<string, () => void>());
  const requestSequenceRef = useRef(0);
  const groupSendInFlightRef = useRef(false);
  const sessionId = target?.sessionId;
  const accessAgentId = target?.agentId || agentId;
  const targetReadOnly = target?.readOnly === true;
  const groupMode = Boolean(groupConfig);
  // Project group writes use the authorized REST adapter; its WebSocket is a
  // read-only event subscription and must not disable that separate composer.
  const canCompose =
    interactive && !targetReadOnly && (!serverReadOnly || groupMode);
  const mentionLimit = Math.max(0, groupConfig?.maxMentions ?? 8);
  const mentionOptions = useMemo(
    () =>
      groupConfig?.members
        .filter(
          (member) =>
            member.isEnabled !== false &&
            !member.isLeader &&
            member.agentId !== groupConfig.currentAgentId,
        )
        .map((member) => ({ value: member.agentId, label: member.name })) || [],
    [groupConfig],
  );

  const loadSession = useCallback(
    async (background = false) => {
      if (!sessionId || !accessAgentId) return;
      const sequence = ++requestSequenceRef.current;
      if (!background) setLoading(true);
      try {
        const loadHistoryPage = async (before?: string) => {
          const currentGroupConfig = groupConfigRef.current;
          if (currentGroupConfig) {
            return currentGroupConfig.loadMessages(sessionId, {
              before,
              limit: SESSION_HISTORY_PAGE_SIZE,
            });
          }
          const page = await chatSessionApi.messagesPage(
            accessAgentId,
            sessionId,
            {
              before,
              limit: SESSION_HISTORY_PAGE_SIZE,
            },
          );
          return {
            items: Array.isArray(page.items) ? page.items : [],
            hasMore: Boolean(page.has_more),
            nextCursor: page.next_cursor || null,
            turn: undefined,
          };
        };
        const [detail, firstPage] = await Promise.all([
          chatSessionApi
            .get(accessAgentId, sessionId)
            .catch(() => ({ id: sessionId })),
          loadHistoryPage(),
        ]);
        if (sequence !== requestSequenceRef.current) return;
        let rows = Array.isArray(firstPage.items) ? firstPage.items : [];
        let hasMore = firstPage.hasMore;
        let nextCursor = firstPage.nextCursor;
        let pageCount = 1;
        const anchorRequested = Boolean(
          target?.anchorMessageId || target?.projectRunId,
        );
        let resolvedAnchor = resolveConversationMessageAnchor(
          rows as Array<Record<string, unknown>>,
          target?.anchorMessageId,
          target?.projectRunId,
        );
        while (
          anchorRequested &&
          !resolvedAnchor &&
          hasMore &&
          nextCursor &&
          pageCount < SESSION_HISTORY_MAX_PAGES &&
          rows.length < SESSION_HISTORY_MAX_MESSAGES
        ) {
          const cursor = nextCursor;
          const olderPage = await loadHistoryPage(cursor);
          if (sequence !== requestSequenceRef.current) return;
          const olderRows = Array.isArray(olderPage.items)
            ? olderPage.items
            : [];
          const knownIds = new Set(
            rows.map((row) =>
              String((row as Record<string, unknown>).id || ""),
            ),
          );
          rows = [
            ...olderRows.filter(
              (row) =>
                !knownIds.has(
                  String((row as Record<string, unknown>).id || ""),
                ),
            ),
            ...rows,
          ].slice(-SESSION_HISTORY_MAX_MESSAGES);
          pageCount += 1;
          hasMore = olderPage.hasMore;
          nextCursor = olderPage.nextCursor;
          resolvedAnchor = resolveConversationMessageAnchor(
            rows as Array<Record<string, unknown>>,
            target?.anchorMessageId,
            target?.projectRunId,
          );
          if (!olderRows.length || nextCursor === cursor) break;
        }
        const normalized = (Array.isArray(rows) ? rows : [])
          .map((row) => {
            const raw =
              row && typeof row === "object"
                ? (row as Record<string, any>)
                : {};
            if (raw.role === "tool_call") return mapHistoryMessage(raw);
            const attachmentFields = normalizeChatAttachmentFields({
              raw,
              sourceChannel: String(
                (detail as Record<string, any> | null)?.source_channel || "",
              ),
              buildDownloadUrl: (path, inline) =>
                fileApi.downloadUrl(accessAgentId, path, { inline }),
            });
            const mapped = mapHistoryMessage({
              ...raw,
              display_content: attachmentFields.displayContent,
              attachments: attachmentFields.attachments,
            });
            return mapped
              ? {
                  ...mapped,
                  previewImages: attachmentFields.previewImages,
                  fileName: attachmentFields.fileName,
                  imageUrl: attachmentFields.imageUrl,
                }
              : null;
          })
          .filter((message): message is ConversationMessage =>
            Boolean(message),
          );
        setResolvedAnchorMessageId(resolvedAnchor);
        setAnchorNotice(
          anchorRequested && !resolvedAnchor
            ? t("agent.sessionViewer.anchorNotFound")
            : "",
        );
        let nextGroupTurn: GroupTurnState | null = null;
        if (groupMode && firstPage.turn) {
          const turnReduction = reduceConversationTurnEvent(
            turnRuntimeBySessionRef.current[sessionId] || IDLE_CONVERSATION_TURN,
            { type: "turn_state", turn: firstPage.turn },
          );
          if (turnReduction.accepted) {
            turnRuntimeBySessionRef.current[sessionId] = turnReduction.runtime;
            const activeAgentIds = Array.isArray(firstPage.turn.active_agent_ids)
              ? firstPage.turn.active_agent_ids.map(String)
              : [];
            if (conversationTurnIsRunning(turnReduction.runtime)) {
              nextGroupTurn = {
                phase: "active",
                anchorMessageId: String(firstPage.turn.anchor_message_id || ""),
                agentIds: activeAgentIds,
                runCount: Number(firstPage.turn.run_count || activeAgentIds.length),
              };
            }
            if (!groupSendInFlightRef.current)
              setSending(conversationTurnIsRunning(turnReduction.runtime));
          }
        }
        setSession(detail);
        setMessages((previous) =>
          background ? mergeHistoryMessages(previous, normalized) : normalized,
        );
        if (groupMode) {
          setGroupTurn(nextGroupTurn);
        }
        setError("");
        return nextGroupTurn;
      } catch {
        if (sequence !== requestSequenceRef.current) return;
        setError(t("agent.sessionViewer.loadError"));
        return undefined;
      } finally {
        if (sequence === requestSequenceRef.current && !background)
          setLoading(false);
      }
    },
    [
      accessAgentId,
      groupMode,
      sessionId,
      t,
      target?.anchorMessageId,
      target?.projectRunId,
    ],
  );

  useEffect(() => {
    if (!sessionId) return;
    setSession(null);
    setMessages([]);
    setError("");
    setComposerError("");
    setDraft("");
    setAttachedFiles([]);
    setUploads([]);
    setConnected(false);
    setSending(false);
    setGroupTurn(null);
    setResolvedAnchorMessageId("");
    setAnchorNotice("");
    groupSendInFlightRef.current = false;
    setServerReadOnly(false);
    setMentions([]);
    setInternalUnavailableAttachments(new Set());
    return () => {
      requestSequenceRef.current += 1;
    };
  }, [accessAgentId, sessionId, target?.anchorMessageId, target?.projectRunId]);

  // Refreshing the group configuration (for example after a workspace data
  // poll) must not clear an in-progress draft or its structured mentions.
  // Only the target-change effect above resets composer state.
  useEffect(() => {
    if (!sessionId) return;
    void loadSession(false);
  }, [loadSession, sessionId]);

  useEffect(() => {
    if (!sessionId || !accessAgentId)
      return;
    const token = localStorage.getItem("token");
    if (!token) {
      setComposerError(t("agent.sessionViewer.authenticationRequired"));
      return;
    }

    let disposed = false;
    let reconnectTimer: number | null = null;
    let reconnectAttempt = 0;
    const connect = () => {
      if (disposed || document.hidden) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const language = document.documentElement.lang
        .toLowerCase()
        .startsWith("zh")
        ? "zh"
        : "en";
      const socket = new WebSocket(
        `${protocol}//${window.location.host}/ws/chat/${accessAgentId}?token=${encodeURIComponent(token)}&session_id=${encodeURIComponent(sessionId)}&lang=${language}`,
      );
      socketRef.current = socket;
      socket.onopen = () => {
        if (disposed || socketRef.current !== socket) return;
        reconnectAttempt = 0;
        setComposerError("");
      };
      socket.onmessage = (event) => {
        if (disposed || socketRef.current !== socket) return;
        let payload: Record<string, any>;
        try {
          payload = JSON.parse(String(event.data));
        } catch {
          return;
        }
        const turnReduction = reduceConversationTurnEvent(
          turnRuntimeBySessionRef.current[sessionId] || IDLE_CONVERSATION_TURN,
          payload,
        );
        if (!conversationTurnEventShouldBeHandled(turnReduction)) return;
        turnRuntimeBySessionRef.current[sessionId] = turnReduction.runtime;
        if (turnReduction.controlsLifecycle && turnReduction.hasSnapshot) {
          setSending(conversationTurnIsRunning(turnReduction.runtime));
          if (groupMode && payload.turn) {
            const activeAgentIds = Array.isArray(payload.turn.active_agent_ids)
              ? payload.turn.active_agent_ids.map(String)
              : [];
            setGroupTurn(
              conversationTurnIsRunning(turnReduction.runtime)
                ? {
                    phase: "active",
                    anchorMessageId: String(
                      payload.turn.anchor_message_id ||
                        payload.turn.turn_anchor_id ||
                        "",
                    ),
                    agentIds: activeAgentIds,
                    runCount: Number(
                      payload.turn.run_count || activeAgentIds.length,
                    ),
                  }
                : null,
            );
          }
        }
        if (payload.type === "connected") {
          setConnected(true);
          setServerReadOnly(payload.read_only === true);
          if (payload.read_only === true && !groupMode) {
            setComposerError(t("agent.sessionViewer.readOnlySession"));
          }
          return;
        }
        if (payload.type === "workspace_draft") return;
        if (TIMELINE_EVENT_TYPES.has(String(payload.type || ""))) {
          if (
            turnReduction.controlsLifecycle &&
            !turnReduction.hasSnapshot &&
            ["thinking", "chunk", "tool_call", "confirmation_required"].includes(payload.type)
          ) {
            setSending(true);
          }
          setMessages((previous) =>
            foldConversationTimelineEvent(previous, payload, {
              preserveTransient:
                !turnReduction.controlsLifecycle && !turnReduction.hasSnapshot,
            }).messages,
          );
          if (payload.type === "done") {
            if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
              setSending(false);
            }
            window.setTimeout(() => void loadSession(true), 250);
          }
          return;
        }
        if (payload.type === "error" || payload.type === "quota_exceeded") {
          setMessages((previous) =>
            foldConversationTimelineEvent(previous, payload).messages,
          );
          setComposerError(t("agent.sessionViewer.sendError"));
          if (turnReduction.controlsLifecycle && !turnReduction.hasSnapshot) {
            setSending(false);
          }
        }
      };
      socket.onerror = () => {
        if (socketRef.current === socket) setConnected(false);
      };
      socket.onclose = (event) => {
        if (socketRef.current !== socket) return;
        socketRef.current = null;
        setConnected(false);
        const durableRuntime = beginConversationTurnRecovery(
          turnRuntimeBySessionRef.current[sessionId] || IDLE_CONVERSATION_TURN,
        );
        turnRuntimeBySessionRef.current[sessionId] = durableRuntime;
        setSending(conversationTurnIsRunning(durableRuntime));
        if (
          disposed ||
          event.code === 4001 ||
          event.code === 4002 ||
          event.code === 4003
        )
          return;
        const delay = Math.min(12_000, 800 * 2 ** reconnectAttempt);
        reconnectAttempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };
    const handleVisibility = () => {
      if (!document.hidden && !socketRef.current) connect();
    };
    connect();
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      document.removeEventListener("visibilitychange", handleVisibility);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && socket.readyState < WebSocket.CLOSING)
        socket.close(1000, "session drawer closed");
    };
  }, [
    accessAgentId,
    groupMode,
    interactive,
    loadSession,
    sessionId,
    t,
    targetReadOnly,
  ]);

  useEffect(
    () => () => {
      uploadAbortRef.current.forEach((abort) => abort());
      uploadAbortRef.current.clear();
    },
    [],
  );

  const runtime = session?.runtime;
  const currentStatus = String(
    runtime?.status || target?.status || "",
  ).toLowerCase();
  const active = ACTIVE_STATUSES.has(currentStatus) || sending;
  const timelineScrollAnchor = useMemo(
    () =>
      getConversationScrollAnchor(buildConversationEntries(messages), active),
    [active, messages],
  );
  const {
    showScrollToBottom,
    resumeAutoFollow,
    interactionProps: autoFollowInteractionProps,
  } = useConversationAutoFollow({
    scrollerRef,
    contentKey: timelineScrollAnchor,
    resetKey: `${sessionId || ""}:${target?.anchorMessageId || ""}:${target?.projectRunId || ""}`,
    enabled: Boolean(
      sessionId && !loading && !error && !resolvedAnchorMessageId,
    ),
  });

  useEffect(() => {
    if (!target || embedded) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("disabled"));
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
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      previousFocus?.focus();
    };
  }, [embedded, onClose, target?.sessionId]);

  if (!target || !sessionId) return null;

  return (
    <SessionViewerReady
      {...{
        accessAgentId,
        active,
        agentId,
        agentName,
        anchorNotice,
        attachedFiles,
        autoFollowInteractionProps,
        canCompose,
        closeButtonRef,
        composerError,
        connected,
        currentStatus,
        draft,
        drawerRef,
        embedded,
        error,
        fileInputRef,
        groupConfig,
        groupMode,
        groupSendInFlightRef,
        groupTurn,
        interactive,
        internalPreview,
        internalUnavailableAttachments,
        loadSession,
        loading,
        mentionLimit,
        mentionOptions,
        mentions,
        messages,
        onAttachmentDownload,
        onAttachmentUnavailable,
        onClose,
        onPreviewImages,
        portalContainer,
        resolvedAnchorMessageId,
        resumeAutoFollow,
        richMentionComposerRef,
        routeMode,
        runtime,
        scrollerRef,
        sending,
        serverReadOnly,
        session,
        sessionId,
        setAttachedFiles,
        setComposerError,
        setDraft,
        setGroupTurn,
        setInternalPreview,
        setInternalUnavailableAttachments,
        setMentions,
        setMessages,
        setSending,
        setUploads,
        showScrollToBottom,
        socketRef,
        t,
        target,
        targetReadOnly,
        textareaRef,
        turnRuntimeBySessionRef,
        unavailableAttachmentKeys,
        uploadAbortRef,
        uploads,
      }}
    />
  );
}
