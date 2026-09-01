import {
  type ChangeEventHandler,
  type ClipboardEventHandler,
  type Dispatch,
  type KeyboardEventHandler,
  type RefObject,
  type SetStateAction,
} from "react";
import {
  IconPaperclip,
  IconPlayerStopFilled,
  IconSend,
  IconTrash,
  IconX,
} from "@tabler/icons-react";
import { type TFunction } from "i18next";

import RichMentionComposer, {
  type RichMentionComposerHandle,
} from "../ui/RichMentionComposer";
import { type ChatAttachedFile } from "../../utils/chatAttachments";
import {
  type GroupTurnState,
  type SessionViewerGroupConfig,
} from "./model";

type UploadState = { id: string; name: string; percent: number };

type SessionViewerComposerProps = {
  abortTurn: () => void;
  attachedFiles: ChatAttachedFile[];
  canCompose: boolean;
  composerError: string;
  confirmationPending: boolean;
  connected: boolean;
  draft: string;
  fileInputRef: RefObject<HTMLInputElement | null>;
  groupConfig?: SessionViewerGroupConfig;
  groupMode: boolean;
  groupTurn: GroupTurnState | null;
  handleComposerKeyDown: KeyboardEventHandler<HTMLTextAreaElement>;
  handleFileChange: ChangeEventHandler<HTMLInputElement>;
  handlePaste: ClipboardEventHandler<HTMLElement>;
  interactive: boolean;
  mentionLimit: number;
  mentionOptions: Array<{ value: string; label: string }>;
  richMentionComposerRef: RefObject<RichMentionComposerHandle | null>;
  sendMessage: (draftOverride?: string, mentionsOverride?: string[]) => Promise<void>;
  sending: boolean;
  serverReadOnly: boolean;
  setAttachedFiles: Dispatch<SetStateAction<ChatAttachedFile[]>>;
  setComposerError: Dispatch<SetStateAction<string>>;
  setDraft: Dispatch<SetStateAction<string>>;
  setMentions: Dispatch<SetStateAction<string[]>>;
  t: TFunction;
  targetReadOnly: boolean;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  uploadAbortRef: RefObject<Map<string, () => void>>;
  uploads: UploadState[];
};

export default function SessionViewerComposer(
  props: SessionViewerComposerProps,
) {
  const {
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
  } = props;
  return (
    <>
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
                  confirmationPending ||
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
                  disabled={!canCompose || sending || confirmationPending}
                  placeholder={
                    targetReadOnly || (serverReadOnly && !groupMode)
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
                  disabled={!canCompose || sending || confirmationPending}
                  placeholder={
                    targetReadOnly || (serverReadOnly && !groupMode)
                      ? t("agent.sessionViewer.readOnlySession")
                      : connected
                        ? t("chat.placeholder")
                        : t("agent.sessionViewer.connecting")
                  }
                />
              )}
              {sending ? (
                <button
                  type="button"
                  className="session-viewer-drawer__composer-send session-viewer-drawer__composer-send--stop"
                  onClick={abortTurn}
                  title={t("chat.stop")}
                >
                  <IconPlayerStopFilled size={16} />
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
            {targetReadOnly || (serverReadOnly && !groupMode) || !connected || groupConfig ? (
              <small className="session-viewer-drawer__composer-status">
                {targetReadOnly || (serverReadOnly && !groupMode)
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
          </>
  );
}
