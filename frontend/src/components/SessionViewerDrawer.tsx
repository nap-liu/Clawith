import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import {
  IconArrowUpRight,
  IconPaperclip,
  IconPlayerStopFilled,
  IconMessages,
  IconRefresh,
  IconSend,
  IconTrash,
  IconX,
} from "@tabler/icons-react";

import ConversationTimeline, {
  type ConversationTimelineProps,
} from "../features/conversation/web/ConversationTimeline";
import ConversationScrollToBottomButton from "../features/conversation/ConversationScrollToBottomButton";
import {
  applyAssistantDoneMessage,
  applyAssistantStreamMessage,
  buildConversationEntries,
  getConversationScrollAnchor,
  mapHistoryMessage,
  toolCallMessageFromEvent,
  upsertToolCallMessage,
  type ConversationMessage,
} from "../features/conversation/core/chatTimeline";
import { resolveConversationMessageAnchor } from "../features/conversation/core/conversationAnchoring";
import { useConversationAutoFollow } from "../features/conversation/useConversationAutoFollow";
import ChatImageLightbox from "./ChatImageLightbox";
import RichMentionComposer, {
  type RichMentionComposerHandle,
} from "./ui/RichMentionComposer";
import {
  chatSessionApi,
  fileApi,
  uploadFileWithProgress,
} from "../services/api";
import {
  buildChatAttachmentPayload,
  downloadChatAttachment,
  normalizeChatAttachmentFields,
  type ChatAttachedFile,
  type ChatPreviewImage,
} from "../utils/chatAttachments";
import { createClientId } from "../utils/clientId";

export type SessionViewerTarget = {
  sessionId: string;
  /** Durable ChatMessage id for the exact project Run/event turn. */
  anchorMessageId?: string;
  /** Lets the viewer resolve a child-session anchor from durable message metadata. */
  projectRunId?: string;
  agentId?: string;
  title?: string;
  status?: string;
  mode?: string;
  model?: string;
  /** Force a historical or frozen session into view-only mode. */
  readOnly?: boolean;
};

export type SessionViewerGroupConfig = {
  members: Array<{
    agentId: string;
    name: string;
    isLeader?: boolean;
    isEnabled?: boolean;
  }>;
  currentAgentId?: string;
  maxMentions?: number;
  loadMessages: (
    sessionId: string,
    options?: { before?: string; limit?: number },
  ) => Promise<{
    items: unknown[];
    hasMore: boolean;
    nextCursor: string | null;
  }>;
  sendMessage: (
    sessionId: string,
    payload: {
      content: string;
      llm_content?: string;
      mentions: string[];
      attachments: ReturnType<typeof buildChatAttachmentPayload>["attachments"];
    },
  ) => Promise<{
    message?: Record<string, any>;
    awakened_agent_ids?: string[];
    subagent_runs?: Array<{
      project_run_id?: string;
      run_id: string | null;
      session_id: string | null;
      agent_id: string;
      status: string;
      error?: string;
    }>;
  }>;
};

type GroupPendingRun = {
  projectRunId: string;
  agentId: string;
  status: string;
  createdAt: number;
  anchorMessageId: string;
};

type GroupTurnState = {
  phase: "active" | "expired";
  anchorMessageId: string;
  agentIds: string[];
  runCount: number;
};

const GROUP_RUN_ACTIVE_STATUSES = new Set([
  "queued",
  "pending",
  "waiting",
  "running",
  "processing",
]);
const GROUP_PENDING_TIMEOUT_MS = 45 * 60 * 1000;

function groupMessageMetadata(row: unknown): Record<string, any> {
  if (!row || typeof row !== "object") return {};
  const record = row as Record<string, any>;
  const metadata = record.metadata || record.message_meta;
  return metadata && typeof metadata === "object" && !Array.isArray(metadata)
    ? metadata
    : {};
}

/**
 * Rebuild the pending group turn from the durable group timeline. ProjectRun
 * ids are recorded on the Human anchor and echoed by the Agent reply, so this
 * survives a reload without treating ordinary group messages as a broadcast.
 */
export function deriveGroupTurnState(
  rows: unknown[],
  now = Date.now(),
  timeoutMs = GROUP_PENDING_TIMEOUT_MS,
): GroupTurnState | null {
  const completedProjectRunIds = new Set<string>();
  for (const row of rows) {
    const metadata = groupMessageMetadata(row);
    const sourceIds = Array.isArray(metadata.source_project_run_ids)
      ? metadata.source_project_run_ids
      : Array.isArray(metadata.project_run_ids)
        ? metadata.project_run_ids
        : [];
    sourceIds.forEach((id: unknown) => {
      const value = String(id || "").trim();
      if (value) completedProjectRunIds.add(value);
    });
  }

  const pending: GroupPendingRun[] = [];
  const expired: GroupPendingRun[] = [];
  let latestRunAnchorAt = 0;
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const record = row as Record<string, any>;
    const metadata = groupMessageMetadata(record);
    const subagentRuns = Array.isArray(metadata.subagent_runs)
      ? metadata.subagent_runs
      : [];
    if (!subagentRuns.length) continue;
    const createdAt = Date.parse(String(record.created_at || "")) || now;
    const anchorId = String(record.id || "");
    latestRunAnchorAt = Math.max(latestRunAnchorAt, createdAt);
    for (const rawRun of subagentRuns) {
      if (!rawRun || typeof rawRun !== "object") continue;
      const run = rawRun as Record<string, any>;
      const status = String(run.status || "").toLowerCase();
      const projectRunId = String(run.project_run_id || "").trim();
      const agentId = String(run.agent_id || "").trim();
      if (
        !projectRunId ||
        !agentId ||
        !GROUP_RUN_ACTIVE_STATUSES.has(status) ||
        completedProjectRunIds.has(projectRunId)
      )
        continue;
      const candidate = {
        projectRunId,
        agentId,
        status,
        createdAt,
        anchorMessageId: anchorId,
      };
      if (now - createdAt > timeoutMs) expired.push(candidate);
      else pending.push(candidate);
    }
  }

  // An old timed-out anchor should not keep warning forever once a newer
  // group turn has reached a reply. Active runs can span several anchors,
  // but an expiry notice only belongs to the latest durable turn.
  const source = pending.length
    ? pending
    : expired.filter((run) => run.createdAt === latestRunAnchorAt);
  if (!source.length) return null;
  const latestSource = source.reduce((latest, run) =>
    run.createdAt >= latest.createdAt ? run : latest,
  );
  return {
    phase: pending.length ? "active" : "expired",
    anchorMessageId: latestSource.anchorMessageId,
    agentIds: Array.from(new Set(source.map((run) => run.agentId))),
    runCount: source.length,
  };
}

type SessionViewerDrawerProps = {
  agentId: string;
  agentName: string;
  target: SessionViewerTarget | null;
  routeMode?: "pc" | "h5";
  portalContainer?: HTMLElement | null;
  interactive?: boolean;
  /** Render the same timeline/composer inside the current page instead of an overlay drawer. */
  embedded?: boolean;
  groupConfig?: SessionViewerGroupConfig;
  onClose: () => void;
  onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
  unavailableAttachmentKeys?: ReadonlySet<string>;
  onAttachmentDownload?: ConversationTimelineProps["onAttachmentDownload"];
  onAttachmentUnavailable?: ConversationTimelineProps["onAttachmentUnavailable"];
};

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
  const uploadAbortRef = useRef(new Map<string, () => void>());
  const requestSequenceRef = useRef(0);
  const groupSendInFlightRef = useRef(false);
  const sessionId = target?.sessionId;
  const accessAgentId = target?.agentId || agentId;
  const targetReadOnly = target?.readOnly === true;
  const canCompose = interactive && !targetReadOnly && !serverReadOnly;
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
          if (groupConfig) {
            return groupConfig.loadMessages(sessionId, {
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
        const nextGroupTurn = groupConfig
          ? deriveGroupTurnState(Array.isArray(rows) ? rows : [])
          : null;
        setSession(detail);
        setMessages(normalized);
        if (groupConfig) {
          setGroupTurn(nextGroupTurn);
          if (!groupSendInFlightRef.current)
            setSending(nextGroupTurn?.phase === "active");
        }
        setError("");
        return nextGroupTurn;
      } catch (loadError: any) {
        if (sequence !== requestSequenceRef.current) return;
        setError(loadError?.message || t("agent.sessionViewer.loadError"));
        return undefined;
      } finally {
        if (sequence === requestSequenceRef.current && !background)
          setLoading(false);
      }
    },
    [
      accessAgentId,
      groupConfig,
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
    if (
      !interactive ||
      targetReadOnly ||
      groupConfig ||
      !sessionId ||
      !accessAgentId
    )
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
        const messageId = payload.message_id
          ? String(payload.message_id)
          : undefined;
        if (payload.type === "connected") {
          setConnected(true);
          setServerReadOnly(payload.read_only === true);
          if (payload.read_only === true) {
            setComposerError(t("agent.sessionViewer.readOnlySession"));
          }
          return;
        }
        if (payload.type === "thinking" || payload.type === "chunk") {
          setSending(true);
          setMessages((previous) =>
            applyAssistantStreamMessage(previous, {
              type: payload.type,
              content: String(payload.content || ""),
              messageId,
            }),
          );
          return;
        }
        if (
          payload.type === "workspace_draft" ||
          payload.type === "tool_call" ||
          payload.type === "confirmation_required"
        ) {
          setSending(true);
          setMessages((previous) =>
            upsertToolCallMessage(
              previous,
              toolCallMessageFromEvent({
                ...payload,
                status:
                  payload.type === "confirmation_required"
                    ? "running"
                    : payload.status,
              }),
            ),
          );
          return;
        }
        if (payload.type === "assistant_message_committed") {
          const committed = mapHistoryMessage({
            id: payload.id,
            role: "assistant",
            content: payload.content || "",
            display_content: payload.display_content,
            attachments: payload.attachments,
            created_at: payload.created_at,
          });
          if (committed) {
            setMessages((previous) => {
              const index = previous.findIndex(
                (message) => message.id === committed.id,
              );
              return index < 0
                ? [...previous, committed]
                : [
                    ...previous.slice(0, index),
                    { ...previous[index], ...committed },
                    ...previous.slice(index + 1),
                  ];
            });
          }
          return;
        }
        if (payload.type === "user_message_committed") {
          const clientId = String(payload.client_message_id || "");
          const durableId = String(payload.message_id || "");
          if (clientId && durableId) {
            setMessages((previous) =>
              previous.map((message) =>
                message.id === clientId
                  ? { ...message, id: durableId }
                  : message,
              ),
            );
          }
          return;
        }
        if (payload.type === "channel_user_message") {
          const committed = mapHistoryMessage({
            ...payload,
            role: "user",
            id: payload.id || createClientId(),
          });
          if (committed)
            setMessages((previous) =>
              previous.some((message) => message.id === committed.id)
                ? previous
                : [...previous, committed],
            );
          return;
        }
        if (payload.type === "done") {
          setMessages((previous) =>
            applyAssistantDoneMessage(previous, {
              content: String(payload.content || ""),
              messageId,
            }),
          );
          setSending(false);
          window.setTimeout(() => void loadSession(true), 250);
          return;
        }
        if (payload.type === "error" || payload.type === "quota_exceeded") {
          setComposerError(
            String(
              payload.content ||
                payload.detail ||
                payload.message ||
                t("agent.sessionViewer.sendError"),
            ),
          );
          setSending(false);
        }
      };
      socket.onerror = () => setConnected(false);
      socket.onclose = (event) => {
        if (socketRef.current === socket) socketRef.current = null;
        setConnected(false);
        setSending(false);
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
    groupConfig,
    interactive,
    loadSession,
    sessionId,
    t,
    targetReadOnly,
  ]);

  useEffect(() => {
    if (!interactive || targetReadOnly || !groupConfig || !sessionId) return;
    setConnected(true);
    const timer = window.setInterval(() => void loadSession(true), 3000);
    return () => {
      setConnected(false);
      window.clearInterval(timer);
    };
  }, [groupConfig, interactive, loadSession, sessionId, targetReadOnly]);

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
    if (!sessionId || !active) return;
    const timer = window.setInterval(() => void loadSession(true), 3000);
    return () => window.clearInterval(timer);
  }, [active, loadSession, sessionId]);

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

  const uploadFiles = async (files: File[]) => {
    if (!accessAgentId || files.length === 0) return;
    const allowed = files.slice(
      0,
      Math.max(0, 10 - attachedFiles.length - uploads.length),
    );
    if (allowed.length === 0) {
      setComposerError(t("agent.sessionViewer.attachmentLimit"));
      return;
    }
    setComposerError("");
    await Promise.all(
      allowed.map(async (file) => {
        const uploadId = `session-upload-${createClientId()}`;
        setUploads((previous) => [
          ...previous,
          { id: uploadId, name: file.name, percent: 0 },
        ]);
        const { promise, abort } = uploadFileWithProgress(
          "/chat/upload",
          file,
          (progress) =>
            setUploads((previous) =>
              previous.map((upload) =>
                upload.id === uploadId
                  ? { ...upload, percent: Math.min(progress, 100) }
                  : upload,
              ),
            ),
          { agent_id: accessAgentId },
          600_000,
        );
        uploadAbortRef.current.set(uploadId, abort);
        try {
          const result = await promise;
          setAttachedFiles((previous) =>
            [
              ...previous,
              {
                name: result.saved_filename || result.filename || file.name,
                text: result.extracted_text || "",
                path: result.workspace_path,
                imageUrl: result.image_data_url || undefined,
                mimeType: file.type || undefined,
                sizeBytes: result.size ?? file.size,
                source: "upload" as const,
              },
            ].slice(0, 10),
          );
        } catch (uploadError: any) {
          if (uploadError?.message !== "Upload cancelled") {
            setComposerError(
              uploadError?.message || t("agent.sessionViewer.uploadError"),
            );
          }
        } finally {
          uploadAbortRef.current.delete(uploadId);
          setUploads((previous) =>
            previous.filter((upload) => upload.id !== uploadId),
          );
        }
      }),
    );
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    void uploadFiles(files);
  };

  const handlePaste = (event: ClipboardEvent<HTMLElement>) => {
    const images = Array.from(event.clipboardData?.items || [])
      .filter((item) => item.type.startsWith("image/"))
      .map((item, index) => {
        const file = item.getAsFile();
        if (!file) return null;
        const extension = file.type.split("/")[1] || "png";
        return new File([file], `paste-${Date.now()}-${index}.${extension}`, {
          type: file.type,
        });
      })
      .filter((file): file is File => Boolean(file));
    if (!images.length) return;
    event.preventDefault();
    void uploadFiles(images);
  };

  const sendMessage = async (
    draftOverride?: string,
    mentionsOverride?: string[],
  ) => {
    const socket = socketRef.current;
    if (!canCompose || !connected || sending) return;
    const effectiveDraft = draftOverride ?? draft;
    const effectiveMentions = mentionsOverride ?? mentions;
    if (!effectiveDraft.trim() && attachedFiles.length === 0) return;
    const attachmentPayload = buildChatAttachmentPayload({
      input: effectiveDraft.trim(),
      attachments: attachedFiles,
    });
    const clientMessageId = createClientId();
    setMessages((previous) => [
      ...previous,
      {
        id: clientMessageId,
        role: "user",
        content: attachmentPayload.displayContent,
        display_content: attachmentPayload.displayContent,
        attachments: attachmentPayload.attachments,
        previewImages: attachmentPayload.previewImages,
        fileName: attachmentPayload.fileName,
        imageUrl: attachmentPayload.imageUrl,
        created_at: new Date().toISOString(),
      },
    ]);
    if (groupConfig) {
      groupSendInFlightRef.current = true;
      setSending(true);
      try {
        const result = await groupConfig.sendMessage(sessionId, {
          content:
            attachmentPayload.displayContent ||
            t("agent.sessionViewer.attachmentOnlyMessage"),
          llm_content: attachmentPayload.contentForLLM,
          mentions: effectiveMentions,
          attachments: attachmentPayload.attachments,
        });
        const committed = result.message
          ? mapHistoryMessage(result.message)
          : null;
        if (committed) {
          setMessages((previous) =>
            previous.map((message) =>
              message.id === clientMessageId ? committed : message,
            ),
          );
        }
        const responseRuns = Array.isArray(result.subagent_runs)
          ? result.subagent_runs
          : [];
        const activeResponseRuns = responseRuns.filter((run) =>
          GROUP_RUN_ACTIVE_STATUSES.has(String(run.status || "").toLowerCase()),
        );
        const responseTurn: GroupTurnState | null = activeResponseRuns.length
          ? {
              phase: "active",
              anchorMessageId: String(result.message?.id || clientMessageId),
              agentIds: Array.from(
                new Set(
                  activeResponseRuns
                    .map((run) => String(run.agent_id || ""))
                    .filter(Boolean),
                ),
              ),
              runCount: activeResponseRuns.length,
            }
          : null;
        setGroupTurn(responseTurn);
        setDraft("");
        setAttachedFiles([]);
        setMentions([]);
        setComposerError("");
        if (textareaRef.current) textareaRef.current.style.height = "auto";
        const durableTurn = await loadSession(true);
        const effectiveTurn =
          durableTurn === undefined ? responseTurn : durableTurn;
        setGroupTurn(effectiveTurn);
        setSending(effectiveTurn?.phase === "active");
        if (
          !effectiveTurn &&
          responseRuns.length &&
          responseRuns.every(
            (run) =>
              !GROUP_RUN_ACTIVE_STATUSES.has(
                String(run.status || "").toLowerCase(),
              ),
          )
        ) {
          setComposerError(t("agent.sessionViewer.groupTurnNoActiveRun"));
        }
      } catch (sendError: any) {
        setMessages((previous) =>
          previous.filter((message) => message.id !== clientMessageId),
        );
        setComposerError(
          sendError?.message || t("agent.sessionViewer.sendError"),
        );
        setSending(false);
        setGroupTurn(null);
      } finally {
        groupSendInFlightRef.current = false;
      }
      return;
    }
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setMessages((previous) =>
        previous.filter((message) => message.id !== clientMessageId),
      );
      setComposerError(t("agent.sessionViewer.reconnecting"));
      return;
    }
    try {
      socket.send(
        JSON.stringify({
          message_id: clientMessageId,
          content: attachmentPayload.contentForLLM,
          display_content: attachmentPayload.displayContent,
          file_name: attachmentPayload.fileName,
          attachments: attachmentPayload.attachments,
        }),
      );
      setDraft("");
      setAttachedFiles([]);
      setComposerError("");
      setSending(true);
      if (textareaRef.current) textareaRef.current.style.height = "auto";
    } catch {
      setMessages((previous) =>
        previous.filter((message) => message.id !== clientMessageId),
      );
      setComposerError(t("agent.sessionViewer.reconnecting"));
    }
  };

  const handleComposerKeyDown = (
    event: ReactKeyboardEvent<HTMLTextAreaElement>,
  ) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void sendMessage();
    }
  };

  const abortTurn = () => {
    if (groupConfig) return;
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify({ type: "abort" }));
  };

  const effectiveUnavailableAttachments =
    unavailableAttachmentKeys || internalUnavailableAttachments;
  const handleAttachmentUnavailable =
    onAttachmentUnavailable ||
    ((key: string) => {
      setInternalUnavailableAttachments((previous) =>
        new Set(previous).add(key),
      );
    });
  const handleAttachmentDownload =
    onAttachmentDownload ||
    (async (path: string, displayName: string) => {
      try {
        await downloadChatAttachment(
          fileApi.downloadUrl(accessAgentId, path),
          displayName,
        );
      } catch {
        handleAttachmentUnavailable(path);
      }
    });
  const handlePreviewImages =
    onPreviewImages ||
    ((images: ChatPreviewImage[], index: number) =>
      setInternalPreview({ images, index }));

  const executionAgentId = String(
    runtime?.execution_agent_id ||
      session?.agent_id ||
      target.agentId ||
      agentId,
  );
  const executionAgentName = String(
    runtime?.execution_agent_name ||
      agentName ||
      t("agent.sessionViewer.digitalEmployee"),
  );
  const groupLeader = groupConfig?.members.find(
    (member) => member.isLeader && member.isEnabled !== false,
  );
  const activeGroupAgentIds =
    groupTurn?.phase === "active" ? groupTurn.agentIds : [];
  const activeGroupAgents = activeGroupAgentIds
    .map((pendingAgentId) =>
      groupConfig?.members.find((member) => member.agentId === pendingAgentId),
    )
    .filter((member): member is NonNullable<typeof member> => Boolean(member));
  const primaryGroupAgent =
    activeGroupAgents.find((member) => member.isLeader) ||
    activeGroupAgents[0] ||
    groupLeader;
  const queuedAgentCount = activeGroupAgents.filter(
    (member) => member.agentId !== primaryGroupAgent?.agentId,
  ).length;
  const groupProcessingAgentName =
    primaryGroupAgent?.name || t("projectTerminology.groupProcessingFallback");
  const groupProcessingLabel =
    groupConfig && sending
      ? activeGroupAgents.length > 1
        ? t("agent.sessionViewer.groupProcessingQueued", {
            name: groupProcessingAgentName,
            count: queuedAgentCount,
          })
        : t("agent.sessionViewer.groupProcessing", {
            name: groupProcessingAgentName,
          })
      : "";
  const timelineMessages =
    groupConfig && sending
      ? [
          ...messages,
          {
            id: `group-pending-${groupTurn?.anchorMessageId || "sending"}`,
            role: "assistant" as const,
            content: "",
            created_at: null,
            sender_agent_id:
              activeGroupAgents[0]?.agentId || groupLeader?.agentId,
            sender_name:
              activeGroupAgents[0]?.name ||
              groupLeader?.name ||
              t("projectTerminology.groupProcessingFallback"),
            _streaming: true,
          },
        ]
      : messages;
  const routePrefix = routeMode === "h5" ? "/h5/agents" : "/agents";
  const fullSessionHref = `${routePrefix}/${executionAgentId}/chat?session_id=${encodeURIComponent(sessionId)}`;
  const isSubagent =
    runtime?.kind === "subagent" || session?.source_channel === "subagent";
  const participantName = String(
    session?.username || t("agent.sessionViewer.participant"),
  );

  const viewer = (
    <div
      className={`session-viewer-drawer-layer session-viewer-drawer-layer--${routeMode}${embedded ? " session-viewer-drawer-layer--embedded" : ""}`}
      role="presentation"
    >
      {!embedded && (
        <button
          type="button"
          className="session-viewer-drawer-backdrop"
          aria-label={t("agent.sessionViewer.close")}
          onClick={onClose}
        />
      )}
      <aside
        ref={drawerRef}
        className={`session-viewer-drawer session-viewer-drawer--${routeMode}${embedded ? " session-viewer-drawer--embedded" : ""}`}
        role={embedded ? "region" : "dialog"}
        aria-modal={embedded ? undefined : true}
        aria-labelledby="session-viewer-drawer-title"
      >
        <header className="session-viewer-drawer__header">
          <span className="session-viewer-drawer__mark">
            <IconMessages size={19} stroke={1.8} />
          </span>
          <span className="session-viewer-drawer__heading">
            <strong id="session-viewer-drawer-title">
              {t("agent.sessionViewer.title")}
            </strong>
            <span>
              {session?.title || target.title || `#${sessionId.slice(0, 8)}`}
            </span>
          </span>
          <span className="session-viewer-drawer__header-actions">
            {currentStatus && (
              <span
                className={`session-viewer-drawer__live${active ? "" : " session-viewer-drawer__live--terminal"}`}
              >
                {active && <i />}
                {t(`agent.sessionViewer.status.${currentStatus}`, {
                  defaultValue: currentStatus,
                })}
              </span>
            )}
            {routeMode === "pc" && !groupConfig && !targetReadOnly && (
              <a
                href={fullSessionHref}
                title={t("agent.sessionViewer.openFullSession")}
                onClick={onClose}
              >
                <IconArrowUpRight size={17} stroke={1.8} />
              </a>
            )}
            {!embedded && (
              <button
                ref={closeButtonRef}
                type="button"
                onClick={onClose}
                title={t("agent.sessionViewer.close")}
              >
                <IconX size={18} stroke={1.8} />
              </button>
            )}
          </span>
        </header>
        <div className="session-viewer-drawer__context">
          <span>
            {canCompose
              ? t("agent.sessionViewer.interactive")
              : t("agent.sessionViewer.readOnly")}
          </span>
          <code>{sessionId}</code>
        </div>
        <div
          ref={scrollerRef}
          className="session-viewer-drawer__messages"
          tabIndex={0}
          {...autoFollowInteractionProps}
        >
          {loading ? (
            <div className="session-viewer-drawer__state">
              <IconRefresh
                className="subagent-run-card__spin"
                size={20}
                stroke={1.8}
              />
              <span>{t("agent.sessionViewer.loading")}</span>
            </div>
          ) : error ? (
            <div className="session-viewer-drawer__state session-viewer-drawer__state--error">
              <strong>{t("agent.sessionViewer.loadError")}</strong>
              <span>{error}</span>
              <button type="button" onClick={() => void loadSession(false)}>
                {t("agent.sessionViewer.retry")}
              </button>
            </div>
          ) : timelineMessages.length === 0 ? (
            <div className="session-viewer-drawer__state">
              {t("agent.sessionViewer.empty")}
            </div>
          ) : (
            <>
              {anchorNotice && (
                <div
                  className="session-viewer-drawer__anchor-notice"
                  role="status"
                >
                  {anchorNotice}
                </div>
              )}
              <ConversationTimeline
                agentId={executionAgentId}
                agentName={executionAgentName}
                messages={timelineMessages}
                scrollerRef={scrollerRef}
                focusMessageId={resolvedAnchorMessageId || undefined}
                isRunning={active}
                runningLabel={groupProcessingLabel || undefined}
                mode={routeMode}
                onPreviewImages={handlePreviewImages}
                unavailableAttachmentKeys={effectiveUnavailableAttachments}
                onAttachmentDownload={handleAttachmentDownload}
                onAttachmentUnavailable={handleAttachmentUnavailable}
                onToolResolved={(message, result) =>
                  setMessages((previous) =>
                    upsertToolCallMessage(previous, {
                      ...message,
                      toolStatus: "done",
                      toolResult: result,
                    }),
                  )
                }
                viewOf={(message) => {
                  const groupAgent = groupConfig?.members.find(
                    (member) => member.agentId === message.sender_agent_id,
                  );
                  const groupSenderName =
                    message.sender_name || groupAgent?.name;
                  if (groupConfig) {
                    const senderName =
                      groupSenderName ||
                      (message.sender_agent_id
                        ? t("agent.sessionViewer.projectAgent")
                        : t("agent.sessionViewer.projectUser"));
                    return {
                      isLeft: Boolean(message.sender_agent_id),
                      senderLabel: senderName,
                      avatarText:
                        senderName[0] || (message.sender_agent_id ? "A" : "U"),
                      forceSenderLabel: true,
                    };
                  }
                  return {
                    isLeft: message.role !== "user",
                    senderLabel:
                      message.role === "user"
                        ? message.sender_name ||
                          (isSubagent
                            ? t("agent.sessionViewer.caller")
                            : participantName)
                        : executionAgentName,
                    avatarText:
                      message.role === "user"
                        ? (message.sender_name ||
                            (isSubagent
                              ? t("agent.sessionViewer.callerAvatar")
                              : participantName))[0] || "U"
                        : executionAgentName[0] || "A",
                    forceSenderLabel: true,
                  };
                }}
              />
            </>
          )}
        </div>
        {showScrollToBottom && !resolvedAnchorMessageId && (
          <ConversationScrollToBottomButton
            variant={routeMode === "h5" ? "h5" : "web"}
            bottom={interactive ? 92 : 20}
            onClick={resumeAutoFollow}
            label={t("agent.chat.scrollToBottom")}
          />
        )}
        {interactive && (
          <footer className="session-viewer-drawer__composer">
            {(uploads.length > 0 || attachedFiles.length > 0) && (
              <div className="session-viewer-drawer__attachments">
                {uploads.map((upload) => (
                  <span
                    key={upload.id}
                    className="session-viewer-drawer__attachment session-viewer-drawer__attachment--uploading"
                  >
                    <span>{upload.name}</span>
                    <small>{upload.percent}%</small>
                    <button
                      type="button"
                      onClick={() => uploadAbortRef.current.get(upload.id)?.()}
                      aria-label={`${t("common.cancel")} ${upload.name}`}
                    >
                      <IconX size={13} />
                    </button>
                  </span>
                ))}
                {attachedFiles.map((file, index) => (
                  <span
                    key={`${file.path || file.name}-${index}`}
                    className="session-viewer-drawer__attachment"
                  >
                    <span>{file.name}</span>
                    <button
                      type="button"
                      onClick={() =>
                        setAttachedFiles((previous) =>
                          previous.filter(
                            (_, itemIndex) => itemIndex !== index,
                          ),
                        )
                      }
                      aria-label={`${t("common.delete")} ${file.name}`}
                    >
                      <IconTrash size={13} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {composerError && (
              <div
                className="session-viewer-drawer__composer-error"
                role="status"
              >
                {composerError}
              </div>
            )}
            <div className="session-viewer-drawer__composer-row">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                hidden
                onChange={handleFileChange}
              />
              <button
                type="button"
                className="session-viewer-drawer__composer-icon"
                onClick={() => fileInputRef.current?.click()}
                disabled={
                  !canCompose ||
                  !connected ||
                  sending ||
                  uploads.length > 0 ||
                  attachedFiles.length >= 10
                }
                title={t("agent.workspace.uploadFile")}
              >
                <IconPaperclip size={17} />
              </button>
              {groupConfig ? (
                <RichMentionComposer
                  ref={richMentionComposerRef}
                  value={draft}
                  options={mentionOptions}
                  maxMentions={mentionLimit}
                  disabled={!canCompose || sending}
                  placeholder={
                    targetReadOnly || serverReadOnly
                      ? t("agent.sessionViewer.readOnlySession")
                      : connected
                        ? t("chat.placeholder")
                        : t("agent.sessionViewer.connecting")
                  }
                  onChange={(displayContent, mentionIds) => {
                    setDraft(displayContent);
                    setMentions(mentionIds);
                    setComposerError("");
                  }}
                  onSubmit={(displayContent, mentionIds) =>
                    void sendMessage(displayContent, mentionIds)
                  }
                  onPaste={handlePaste}
                  onLimitExceeded={() =>
                    setComposerError(
                      t("agent.sessionViewer.mentionLimit", {
                        count: mentionLimit,
                      }),
                    )
                  }
                  onUnresolvedSubmit={() =>
                    setComposerError(t("agent.sessionViewer.unresolvedMention"))
                  }
                />
              ) : (
                <textarea
                  ref={textareaRef}
                  value={draft}
                  onChange={(event) => {
                    setDraft(event.target.value);
                    event.target.style.height = "auto";
                    event.target.style.height = `${Math.min(event.target.scrollHeight, 132)}px`;
                  }}
                  onKeyDown={handleComposerKeyDown}
                  onPaste={handlePaste}
                  rows={1}
                  disabled={!canCompose || sending}
                  placeholder={
                    targetReadOnly || serverReadOnly
                      ? t("agent.sessionViewer.readOnlySession")
                      : connected
                        ? t("chat.placeholder")
                        : t("agent.sessionViewer.connecting")
                  }
                />
              )}
              {sending && !groupConfig ? (
                <button
                  type="button"
                  className="session-viewer-drawer__composer-send session-viewer-drawer__composer-send--stop"
                  onClick={abortTurn}
                  title={t("chat.stop")}
                >
                  <IconPlayerStopFilled size={16} />
                </button>
              ) : sending ? (
                <button
                  type="button"
                  className="session-viewer-drawer__composer-send"
                  disabled
                  title={t("chat.sending")}
                >
                  <IconRefresh className="subagent-run-card__spin" size={16} />
                </button>
              ) : (
                <button
                  type="button"
                  className="session-viewer-drawer__composer-send"
                  onClick={() =>
                    groupConfig
                      ? richMentionComposerRef.current?.submit()
                      : void sendMessage()
                  }
                  disabled={
                    !canCompose ||
                    !connected ||
                    uploads.length > 0 ||
                    (!draft.trim() && attachedFiles.length === 0)
                  }
                  title={t("chat.send")}
                >
                  <IconSend size={16} />
                </button>
              )}
            </div>
            {targetReadOnly || serverReadOnly || !connected || groupConfig ? (
              <small className="session-viewer-drawer__composer-status">
                {targetReadOnly || serverReadOnly
                  ? t("agent.sessionViewer.readOnly")
                  : connected
                    ? groupTurn?.phase === "expired"
                      ? t("agent.sessionViewer.groupTurnExpired")
                      : t("agent.sessionViewer.groupConnected")
                    : t("agent.sessionViewer.connecting")}
              </small>
            ) : null}
          </footer>
        )}
      </aside>
      {!onPreviewImages && (
        <ChatImageLightbox
          open={Boolean(internalPreview)}
          images={internalPreview?.images || []}
          index={internalPreview?.index || 0}
          mode={routeMode === "h5" ? "mobile" : "desktop"}
          onClose={() => setInternalPreview(null)}
          onIndexChange={(index) =>
            setInternalPreview((previous) =>
              previous ? { ...previous, index } : previous,
            )
          }
        />
      )}
    </div>
  );
  return embedded
    ? viewer
    : createPortal(viewer, portalContainer || document.body);
}
