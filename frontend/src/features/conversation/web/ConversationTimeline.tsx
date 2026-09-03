import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import ChatToolCallRenderer from "../../../components/ChatToolCallRenderer";
import Avatar from "../../../components/ui/Avatar";
import {
  buildConversationEntries,
  projectConversationTurnProgress,
  shouldProjectConversationTurnProgress,
} from "../core/chatTimeline";
import { AnalysisCard } from "./conversationTimeline/AnalysisCard";
import {
  entryMessageId,
  findConversationAnchorEntryIndex,
  type ConversationEntry,
} from "./conversationTimeline/entries";
import { MessageItem } from "./conversationTimeline/MessageItem";
import type {
  ConversationTimelineProps,
} from "./conversationTimeline/types";

export type {
  ConversationMessageView,
  ConversationTimelineProps,
} from "./conversationTimeline/types";
export { findConversationAnchorEntryIndex } from "./conversationTimeline/entries";

export default function ConversationTimeline({
  agentId,
  agentName,
  messages,
  mode = "pc",
  viewOf,
  isRunning = false,
  runningLabel,
  progressMessage,
  unavailableAttachmentKeys = new Set<string>(),
  onAttachmentDownload,
  onAttachmentUnavailable,
  onPreviewImages,
  onToolResolved,
  onOpenSubagentSession,
  scrollerRef,
  focusMessageId,
  resumeMeasurementKey,
  provenance,
}: ConversationTimelineProps) {
  const { t, i18n } = useTranslation();
  const [expandedAnalysis, setExpandedAnalysis] = useState<
    Record<string, boolean>
  >({});
  const baseEntries = useMemo(
    () =>
      buildConversationEntries(
        projectConversationTurnProgress(messages, false),
      ),
    [messages],
  );
  const showTurnProgress = shouldProjectConversationTurnProgress(
    baseEntries,
    isRunning,
    expandedAnalysis,
  );
  const entries = useMemo(
    () =>
      buildConversationEntries(
        projectConversationTurnProgress(
          messages,
          showTurnProgress,
          progressMessage,
        ),
      ),
    [messages, progressMessage, showTurnProgress],
  );
  const focusEntryIndex = useMemo(
    () => findConversationAnchorEntryIndex(entries, focusMessageId),
    [entries, focusMessageId],
  );
  const provenanceTime = provenance?.finished_at || provenance?.scheduled_at;
  const status = provenance?.status || "";
  const statusText =
    status === "completed"
      ? i18n.language?.startsWith("zh")
        ? "已完成"
        : "Completed"
      : status === "failed"
        ? i18n.language?.startsWith("zh")
          ? "失败"
          : "Failed"
        : status === "processing"
          ? i18n.language?.startsWith("zh")
            ? "执行中"
            : "Running"
          : status === "pending"
            ? i18n.language?.startsWith("zh")
              ? "等待中"
              : "Pending"
            : status;
  const analysisOwners = new Map<
    string,
    Extract<(typeof entries)[number], { type: "message" }>
  >();
  let nextAssistant:
    Extract<(typeof entries)[number], { type: "message" }> | undefined;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.type === "message" && entry.msg.role === "assistant")
      nextAssistant = entry;
    else if (entry.type === "analysis_group" && nextAssistant)
      analysisOwners.set(entry.key, nextAssistant);
  }
  const virtualizeEntries = Boolean(scrollerRef && entries.length > 40);
  const rowVirtualizer = useVirtualizer({
    count: virtualizeEntries ? entries.length : 0,
    getScrollElement: () => scrollerRef?.current ?? null,
    estimateSize: (index) => {
      const entry = entries[index];
      if (!entry) return 88;
      if (entry.type === "analysis_group") return 70;
      if (entry.type === "special_render") return 140;
      return entry.msg.content?.length > 600 ? 180 : 88;
    },
    getItemKey: (index) => entries[index]?.key ?? `conversation-entry-${index}`,
    overscan: 8,
    enabled: virtualizeEntries,
  });
  useEffect(() => {
    if (!virtualizeEntries || resumeMeasurementKey == null || document.hidden)
      return;
    const measureMountedRows = () => {
      scrollerRef?.current
        ?.querySelectorAll<HTMLElement>(".conversation-timeline__virtual-row")
        .forEach((element) => rowVirtualizer.measureElement(element));
    };
    let secondFrame: number | null = null;
    const firstFrame = window.requestAnimationFrame(() => {
      measureMountedRows();
      // The history reconciliation and Markdown layout can commit in the
      // same foreground transition. A second frame catches that layout
      // without clearing offscreen measurements or measuring every chunk.
      secondFrame = window.requestAnimationFrame(measureMountedRows);
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame != null) window.cancelAnimationFrame(secondFrame);
    };
  }, [resumeMeasurementKey, rowVirtualizer, scrollerRef, virtualizeEntries]);
  useEffect(() => {
    if (!focusMessageId || focusEntryIndex < 0 || !scrollerRef?.current) return;
    const timers: number[] = [];
    const reveal = () => {
      if (virtualizeEntries)
        rowVirtualizer.scrollToIndex(focusEntryIndex, { align: "center" });
      window.requestAnimationFrame(() => {
        const row = Array.from(
          scrollerRef.current?.querySelectorAll<HTMLElement>(
            "[data-message-id]",
          ) || [],
        ).find((element) => element.dataset.messageId === focusMessageId);
        row?.scrollIntoView({ block: "center", behavior: "auto" });
      });
    };
    reveal();
    [100, 320, 800].forEach((delay) =>
      timers.push(window.setTimeout(reveal, delay)),
    );
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [
    focusEntryIndex,
    focusMessageId,
    rowVirtualizer,
    scrollerRef,
    virtualizeEntries,
  ]);
  const renderEntry = (entry: ConversationEntry, index: number) => {
    if (entry.type === "analysis_group") {
      const owner = analysisOwners.get(entry.key);
      const ownerView =
        owner?.type === "message"
          ? viewOf(owner.msg)
          : { isLeft: true, avatarText: agentName[0] };
      const running =
        entry.running || (isRunning && index === entries.length - 1);
      return (
        <div
          className={`chat-msg-row chat-msg-row--analysis${ownerView.isLeft ? "" : " chat-msg-row--user"}`}
        >
          <Avatar
            className="chat-msg-avatar"
            src={ownerView.avatarUrl || owner?.msg.sender_avatar_url}
            name={ownerView.avatarText || agentName}
          />
          <AnalysisCard
            items={entry.items}
            running={running}
            expanded={!!expandedAnalysis[entry.key]}
            onToggle={() =>
              setExpandedAnalysis((current) => ({
                ...current,
                [entry.key]: !current[entry.key],
              }))
            }
          />
        </div>
      );
    }
    if (entry.type === "special_render") {
      const view = viewOf(entry.msg);
      const messageAgentId = entry.msg.sender_agent_id || agentId;
      return (
        <div
          className={`chat-msg-row chat-msg-row--special-render chat-msg-row--${entry.renderType}`}
        >
          <Avatar
            className="chat-msg-avatar"
            src={view.avatarUrl || entry.msg.sender_avatar_url}
            name={view.avatarText || agentName}
          />
          <ChatToolCallRenderer
            agentId={messageAgentId}
            message={entry.msg}
            t={t}
            mode={mode}
            onPreviewImages={onPreviewImages}
            onOpenSubagentSession={onOpenSubagentSession}
            onResolved={(result) => onToolResolved?.(entry.msg, result)}
          />
        </div>
      );
    }
    const previous = entries[index - 1];
    const view = viewOf(entry.msg);
    return (
      <MessageItem
        agentId={entry.msg.sender_agent_id || agentId}
        msg={entry.msg}
        view={{
          ...view,
          hideAvatar:
            view.hideAvatar ||
            (entry.msg.role === "assistant" &&
              previous?.type === "analysis_group"),
        }}
        unavailable={unavailableAttachmentKeys}
        onDownload={onAttachmentDownload}
        onUnavailable={onAttachmentUnavailable}
        onPreview={onPreviewImages}
        runningLabel={runningLabel}
      />
    );
  };
  return (
    <div className="conversation-timeline">
      {provenance && (
        <div
          className={`conversation-provenance conversation-provenance--${status || "unknown"}`}
        >
          <span className="conversation-provenance-source">
            {provenance.source || "trigger"}
          </span>
          {statusText && (
            <span className="conversation-provenance-status">{statusText}</span>
          )}
          {provenanceTime && (
            <span className="conversation-provenance-time">
              {new Date(provenanceTime).toLocaleString()}
            </span>
          )}
          {provenance.last_error && (
            <span className="conversation-provenance-error">
              {provenance.last_error}
            </span>
          )}
        </div>
      )}
      {virtualizeEntries ? (
        <div
          className="conversation-timeline__virtual-space"
          style={{ height: `${rowVirtualizer.getTotalSize()}px` }}
        >
          {rowVirtualizer.getVirtualItems().map((virtualItem) => {
            const entry = entries[virtualItem.index];
            if (!entry) return null;
            const messageId = entryMessageId(entry, focusMessageId);
            const focused = Boolean(
              focusMessageId && messageId === focusMessageId,
            );
            return (
              <div
                key={virtualItem.key}
                ref={rowVirtualizer.measureElement}
                data-index={virtualItem.index}
                data-conversation-entry-key={entry.key}
                data-message-id={messageId}
                aria-current={focused ? "true" : undefined}
                className={`conversation-timeline__virtual-row${focused ? " conversation-timeline__focus-anchor" : ""}`}
                style={{ transform: `translateY(${virtualItem.start}px)` }}
              >
                {renderEntry(entry, virtualItem.index)}
              </div>
            );
          })}
        </div>
      ) : (
        entries.map((entry, index) => {
          const messageId = entryMessageId(entry, focusMessageId);
          const focused = Boolean(
            focusMessageId && messageId === focusMessageId,
          );
          return (
            <div
              key={entry.key}
              data-conversation-entry-key={entry.key}
              data-message-id={messageId}
              aria-current={focused ? "true" : undefined}
              className={
                focused ? "conversation-timeline__focus-anchor" : undefined
              }
            >
              {renderEntry(entry, index)}
            </div>
          );
        })
      )}
    </div>
  );
}
