import {
  type ChangeEvent,
  type ClipboardEvent,
  type Dispatch,
  type HTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  type SetStateAction,
} from "react";
import { createPortal } from "react-dom";
import { type TFunction } from "i18next";
import {
  IconArrowUpRight,
  IconMessages,
  IconRefresh,
  IconX,
} from "@tabler/icons-react";

import ConversationTimeline, {
  type ConversationTimelineProps,
} from "../../features/conversation/web/ConversationTimeline";
import ConversationScrollToBottomButton from "../../features/conversation/ConversationScrollToBottomButton";
import {
  hasPendingConfirmation,
  mapHistoryMessage,
  upsertToolCallMessage,
  type ConversationMessage,
} from "../../features/conversation/core/chatTimeline";
import {
  IDLE_CONVERSATION_TURN,
  conversationTurnIsRunning,
  reduceConversationTurnEvent,
  type ConversationTurnRuntime,
} from "../../features/conversation/core/conversationTurnLifecycle";
import ChatImageLightbox from "../ChatImageLightbox";
import { type RichMentionComposerHandle } from "../ui/RichMentionComposer";
import { fileApi, uploadFileWithProgress } from "../../services/api";
import {
  buildChatAttachmentPayload,
  downloadChatAttachment,
  type ChatAttachedFile,
  type ChatPreviewImage,
} from "../../utils/chatAttachments";
import { createClientId } from "../../utils/clientId";
import {
  GROUP_RUN_ACTIVE_STATUSES,
  type GroupTurnState,
  type SessionViewerGroupConfig,
  type SessionViewerTarget,
} from "./model";
import SessionViewerComposer from "./SessionViewerComposer";

type UploadState = { id: string; name: string; percent: number };
type InternalPreview = { images: ChatPreviewImage[]; index: number };

type SessionViewerReadyProps = {
  accessAgentId: string;
  active: boolean;
  agentId: string;
  agentName: string;
  anchorNotice: string;
  attachedFiles: ChatAttachedFile[];
  autoFollowInteractionProps: HTMLAttributes<HTMLDivElement>;
  canCompose: boolean;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  composerError: string;
  connected: boolean;
  currentStatus: string;
  draft: string;
  drawerRef: RefObject<HTMLElement | null>;
  embedded: boolean;
  error: string;
  fileInputRef: RefObject<HTMLInputElement | null>;
  groupConfig?: SessionViewerGroupConfig;
  groupMode: boolean;
  groupSendInFlightRef: RefObject<boolean>;
  groupTurn: GroupTurnState | null;
  interactive: boolean;
  internalPreview: InternalPreview | null;
  internalUnavailableAttachments: Set<string>;
  loadSession: (background?: boolean) => Promise<GroupTurnState | null | undefined>;
  loading: boolean;
  mentionLimit: number;
  mentionOptions: Array<{ value: string; label: string }>;
  mentions: string[];
  messages: ConversationMessage[];
  onAttachmentDownload?: ConversationTimelineProps["onAttachmentDownload"];
  onAttachmentUnavailable?: ConversationTimelineProps["onAttachmentUnavailable"];
  onClose: () => void;
  onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
  portalContainer?: HTMLElement | null;
  resolvedAnchorMessageId: string;
  resumeAutoFollow: () => void;
  richMentionComposerRef: RefObject<RichMentionComposerHandle | null>;
  routeMode: "pc" | "h5";
  runtime: Record<string, any> | undefined;
  scrollerRef: RefObject<HTMLDivElement | null>;
  sending: boolean;
  serverReadOnly: boolean;
  session: Record<string, any> | null;
  sessionId: string;
  setAttachedFiles: Dispatch<SetStateAction<ChatAttachedFile[]>>;
  setComposerError: Dispatch<SetStateAction<string>>;
  setDraft: Dispatch<SetStateAction<string>>;
  setGroupTurn: Dispatch<SetStateAction<GroupTurnState | null>>;
  setInternalPreview: Dispatch<SetStateAction<InternalPreview | null>>;
  setInternalUnavailableAttachments: Dispatch<SetStateAction<Set<string>>>;
  setMentions: Dispatch<SetStateAction<string[]>>;
  setMessages: Dispatch<SetStateAction<ConversationMessage[]>>;
  setSending: Dispatch<SetStateAction<boolean>>;
  setUploads: Dispatch<SetStateAction<UploadState[]>>;
  showScrollToBottom: boolean;
  socketRef: RefObject<WebSocket | null>;
  t: TFunction;
  target: SessionViewerTarget;
  targetReadOnly: boolean;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  turnRuntimeBySessionRef: RefObject<Record<string, ConversationTurnRuntime>>;
  unavailableAttachmentKeys?: ReadonlySet<string>;
  uploadAbortRef: RefObject<Map<string, () => void>>;
  uploads: UploadState[];
};

export default function SessionViewerReady(props: SessionViewerReadyProps) {
  const {
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
  } = props;
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
            setComposerError(t("agent.sessionViewer.uploadError"));
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
    if (!canCompose || !connected || sending || confirmationPending) return;
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
          client_message_id: clientMessageId,
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
        const responseRuns = Array.isArray(result.subagent_runs) ? result.subagent_runs : [];
        const responseReduction = result.turn
          ? reduceConversationTurnEvent(
              turnRuntimeBySessionRef.current[sessionId] || IDLE_CONVERSATION_TURN,
              { type: "turn_state", turn: result.turn },
            )
          : null;
        if (responseReduction?.accepted)
          turnRuntimeBySessionRef.current[sessionId] = responseReduction.runtime;
        const responseAgentIds = Array.isArray(result.turn?.active_agent_ids)
          ? result.turn.active_agent_ids.map(String)
          : [];
        const responseTurn: GroupTurnState | null =
          responseReduction && conversationTurnIsRunning(responseReduction.runtime)
            ? {
                phase: "active",
                anchorMessageId: String(result.turn?.anchor_message_id || result.message?.id || clientMessageId),
                agentIds: responseAgentIds,
                runCount: Number(result.turn?.run_count || responseAgentIds.length),
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
      } catch {
        setMessages((previous) =>
          previous.filter((message) => message.id !== clientMessageId),
        );
        setComposerError(t("agent.sessionViewer.sendError"));
        const recoveredTurn = await loadSession(true);
        if (recoveredTurn !== undefined) {
          setGroupTurn(recoveredTurn);
          setSending(recoveredTurn?.phase === "active");
        }
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
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN || !sessionId) return;
    const snapshot = (
      turnRuntimeBySessionRef.current[sessionId] || IDLE_CONVERSATION_TURN
    ).snapshot;
    socket.send(
      JSON.stringify({
        type: "abort",
        turn_anchor_id: snapshot.turnAnchorId,
        generation: snapshot.generation,
      }),
    );
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
  const progressMessage: Partial<ConversationMessage> = groupConfig
    ? {
        id: `group-pending-${groupTurn?.anchorMessageId || "sending"}`,
        created_at: null,
        sender_agent_id:
          activeGroupAgents[0]?.agentId || groupLeader?.agentId,
        sender_name:
          activeGroupAgents[0]?.name ||
          groupLeader?.name ||
          t("projectTerminology.groupProcessingFallback"),
      }
    : {
        id: `conversation-turn-progress:${sessionId || "unknown"}:${turnRuntimeBySessionRef.current[String(sessionId || "")]?.snapshot.generation || 0}`,
      };
  const confirmationPending = hasPendingConfirmation(messages);
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
              {session?.title || target.title || t("agent.sessionViewer.untitled")}
            </span>
          </span>
          <span className="session-viewer-drawer__header-actions">
            {currentStatus && (
              <span
                className={`session-viewer-drawer__live${active ? "" : " session-viewer-drawer__live--terminal"}`}
              >
                {active && <i />}
                {t(`agent.sessionViewer.status.${currentStatus}`, {
                  defaultValue: t("agent.sessionViewer.status.unknown"),
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
          ) : messages.length === 0 && !active ? (
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
                messages={messages}
                scrollerRef={scrollerRef}
                focusMessageId={resolvedAnchorMessageId || undefined}
                isRunning={active}
                runningLabel={groupProcessingLabel || undefined}
                progressMessage={progressMessage}
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
        <SessionViewerComposer
          {...{
            abortTurn,
            attachedFiles,
            canCompose,
            composerError,
            confirmationPending,
            connected,
            draft,
            fileInputRef,
            groupConfig,
            groupMode,
            groupTurn,
            handleComposerKeyDown,
            handleFileChange,
            handlePaste,
            interactive,
            mentionLimit,
            mentionOptions,
            richMentionComposerRef,
            sendMessage,
            sending,
            serverReadOnly,
            setAttachedFiles,
            setComposerError,
            setDraft,
            setMentions,
            t,
            targetReadOnly,
            textareaRef,
            uploadAbortRef,
            uploads,
          }}
        />
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
