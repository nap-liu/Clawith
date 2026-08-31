import { useState } from "react";
import { useTranslation } from "react-i18next";
import { IconAlertTriangle } from "@tabler/icons-react";

import ChatAttachmentIcon from "../../../../components/ChatAttachmentIcon";
import ChatMediaCard from "../../../../components/ChatMediaCard";
import MarkdownRenderer from "../../../../components/MarkdownRenderer";
import { copyToClipboard } from "../../../../utils/clipboard";
import {
  buildPreviewImage,
  extractChatImageDataMarkers,
  getChatQuotedMessageTypeLabel,
  partitionChatQuotedContent,
  splitAttachmentFileNames,
  stripChatImageDataMarkers,
  type ChatMessageAttachment,
  type ChatPreviewImage,
} from "../../../../utils/chatAttachments";
import type { ConversationMessage } from "../../core/chatTimeline";
import type {
  ConversationMessageView,
  ConversationTimelineProps,
} from "./types";

function CopyMessageButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="conversation-copy-button"
      title="Copy"
      onClick={() =>
        void copyToClipboard(text).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        })
      }
    >
      {copied ? "✓" : "⧉"}
    </button>
  );
}

export function MessageItem({
  agentId,
  msg,
  view,
  unavailable,
  onDownload,
  onUnavailable,
  onPreview,
  runningLabel,
}: {
  agentId: string;
  msg: ConversationMessage;
  view: ConversationMessageView;
  unavailable: ReadonlySet<string>;
  onDownload?: ConversationTimelineProps["onAttachmentDownload"];
  onUnavailable?: ConversationTimelineProps["onAttachmentUnavailable"];
  onPreview?: ConversationTimelineProps["onPreviewImages"];
  runningLabel?: string;
}) {
  const { t, i18n } = useTranslation();
  const quotedMessage = msg.quoted_message;
  const allPreviews: ChatPreviewImage[] = msg.previewImages?.length
    ? msg.previewImages
    : msg.imageUrl
      ? [buildPreviewImage(msg.imageUrl, msg.fileName)]
      : [];
  const inlinePreviews = allPreviews.length
    ? []
    : extractChatImageDataMarkers(msg.content || "");
  const content = stripChatImageDataMarkers(
    msg.display_content ?? msg.content ?? "",
  );
  const allAttachments = Array.isArray(msg.attachments) ? msg.attachments : [];
  const {
    attachments,
    quotedAttachments,
    previewImages: previews,
    quotedPreviewImages: quotedPreviews,
  } = partitionChatQuotedContent(quotedMessage, allAttachments, allPreviews);
  const media = attachments.filter(
    (item) => item.kind === "audio" || item.kind === "video",
  );
  const quotedMedia = quotedAttachments.filter(
    (item) => item.kind === "audio" || item.kind === "video",
  );
  const previewNames = new Set(
    previews.map((image) => image.filename).filter(Boolean),
  );
  const files: Array<{
    name: string;
    path?: string;
    kind?: ChatMessageAttachment["kind"];
    mimeType?: string;
  }> = attachments.length
    ? attachments
        .filter((item) => !["image", "audio", "video"].includes(item.kind))
        .map((item) => ({
          name: item.display_name,
          path: item.path,
          kind: item.kind,
          mimeType: item.mime_type,
        }))
    : splitAttachmentFileNames(msg.fileName)
        .filter((name) => !previewNames.has(name))
        .map((name) => ({ name }));
  const quotedFiles = quotedAttachments
    .filter((item) => !["image", "audio", "video"].includes(item.kind))
    .map((item) => ({
      name: item.display_name,
      path: item.path,
      kind: item.kind,
      mimeType: item.mime_type,
    }));
  const sender = msg.sender_name || view.senderLabel;
  const avatar = view.avatarText || sender?.[0] || (view.isLeft ? "A" : "U");
  const showSender = !!sender && (view.forceSenderLabel || !!msg.sender_name);
  const timestamp = msg.timestamp || msg.created_at || undefined;
  const formattedTime = timestamp
    ? new Date(timestamp).toLocaleString(
        i18n.language?.startsWith("zh") ? "zh-CN" : "en-US",
        { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" },
      )
    : "";
  const renderPreviews = (images: ChatPreviewImage[]) =>
    images.map((image, index) => {
      const key = image.path || image.src;
      if (unavailable.has(key))
        return (
          <div key={`${key}-${index}`} className="chat-msg-file-chip">
            <IconAlertTriangle size={14} />
            <span>{image.filename || "图片"} · 当前不可访问</span>
          </div>
        );
      return (
        <button
          key={`${key}-${index}`}
          className="chat-msg-image-preview"
          onClick={() => onPreview?.(images, index)}
          title={t("common.preview", "Preview")}
        >
          <img
            src={image.src}
            alt={image.alt || image.filename || "image"}
            loading="lazy"
            onError={() => onUnavailable?.(key)}
          />
        </button>
      );
    });
  return (
    <div className={`chat-msg-row${view.isLeft ? "" : " chat-msg-row--user"}`}>
      <div
        className={`chat-msg-avatar${view.isLeft ? "" : " chat-msg-avatar--user"}`}
        style={view.hideAvatar ? { visibility: "hidden" } : undefined}
      >
        {avatar}
      </div>
      <div className="chat-msg-col">
        <div className="chat-msg-content-line">
          <div
            className={`chat-msg-bubble${view.isLeft ? "" : " chat-msg-bubble--user"}${msg._streaming && !msg.content && !msg.thinking ? " chat-msg-bubble--thinking" : ""}`}
          >
            {showSender && <div className="chat-msg-sender">{sender}</div>}
            {quotedMessage && (
              <div className="conversation-quoted-message">
                <div className="conversation-quoted-message__header">
                  <span>↪ {quotedMessage.sender_name || "发送人未知"}</span>
                  <span>
                    {getChatQuotedMessageTypeLabel(quotedMessage.message_type)}
                  </span>
                </div>
                {quotedPreviews.length > 0 && (
                  <div className="conversation-image-list">
                    {renderPreviews(quotedPreviews)}
                  </div>
                )}
                {quotedMedia.length > 0 && (
                  <div className="conversation-media-list">
                    {quotedMedia.map((attachment, index) => (
                      <ChatMediaCard
                        key={`${attachment.path}-${index}`}
                        agentId={agentId}
                        messageId={msg.id}
                        attachment={attachment}
                        onDownload={() =>
                          void onDownload?.(
                            attachment.path,
                            attachment.display_name,
                          )
                        }
                        onUnavailable={() => onUnavailable?.(attachment.path)}
                      />
                    ))}
                  </div>
                )}
                {quotedFiles.length > 0 && (
                  <div className="conversation-file-list">
                    {quotedFiles.map((file, index) => (
                      <button
                        key={`${file.path}-${index}`}
                        className="chat-msg-file-chip"
                        disabled={unavailable.has(file.path)}
                        onClick={() => void onDownload?.(file.path, file.name)}
                      >
                        <ChatAttachmentIcon
                          name={file.name}
                          kind={file.kind}
                          mimeType={file.mimeType}
                          size={16}
                        />
                        <span>{file.name}</span>
                      </button>
                    ))}
                  </div>
                )}
                {quotedMessage.text ? (
                  <MarkdownRenderer content={quotedMessage.text} />
                ) : null}
                {!quotedMessage.text &&
                  quotedMessage.attachments.length === 0 && (
                    <div className="conversation-quoted-message__unavailable">
                      {quotedMessage.content_status === "failed"
                        ? "引用内容获取失败"
                        : "引用内容不可用"}
                    </div>
                  )}
                {quotedMessage.content_status === "partial" && (
                  <div className="conversation-quoted-message__unavailable">
                    部分引用内容未能获取
                  </div>
                )}
              </div>
            )}
            {(previews.length > 0 || inlinePreviews.length > 0) && (
              <div className="conversation-image-list">
                {renderPreviews(previews.length ? previews : inlinePreviews)}
              </div>
            )}
            {media.length > 0 && (
              <div className="conversation-media-list">
                {media.map((attachment, index) => (
                  <ChatMediaCard
                    key={`${attachment.path}-${index}`}
                    agentId={agentId}
                    messageId={msg.id}
                    attachment={attachment}
                    onDownload={() =>
                      void onDownload?.(
                        attachment.path,
                        attachment.display_name,
                      )
                    }
                    onUnavailable={() => onUnavailable?.(attachment.path)}
                  />
                ))}
              </div>
            )}
            {files.length > 0 && (
              <div className="conversation-file-list">
                {files.map((file, index) => (
                  <button
                    key={`${file.path || file.name}-${index}`}
                    className="chat-msg-file-chip"
                    disabled={!file.path || unavailable.has(file.path)}
                    onClick={() =>
                      file.path && void onDownload?.(file.path, file.name)
                    }
                  >
                    <ChatAttachmentIcon
                      name={file.name}
                      kind={file.kind as ChatMessageAttachment["kind"]}
                      mimeType={file.mimeType}
                      size={16}
                    />
                    <span>{file.name}</span>
                  </button>
                ))}
              </div>
            )}
            {msg._streaming && !msg.content && !msg.thinking ? (
              <div className="thinking-indicator">
                <div className="thinking-dots">
                  <span />
                  <span />
                  <span />
                </div>
                <span>
                  {runningLabel || t("agent.chat.thinking", "Thinking...")}
                </span>
              </div>
            ) : (
              <MarkdownRenderer content={content} />
            )}
          </div>
        </div>
        {formattedTime && (
          <div className="chat-msg-timestamp">
            {formattedTime}
            {content && <CopyMessageButton text={content} />}
          </div>
        )}
      </div>
    </div>
  );
}
