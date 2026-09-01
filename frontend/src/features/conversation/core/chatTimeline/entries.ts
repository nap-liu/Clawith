import { getChatToolRenderType } from "../../../../components/ChatToolCallRenderer";
import { normalizeChatTimelineMessages } from "./history";
import { isConfirmationToolCall, normalizeToolResult, parseStoredToolPayload } from "./tooling";
import type {
  ConversationAnalysisItem,
  ConversationEntry,
  ConversationMessage,
} from "./types";

function hasConversationMessagePayload(message: ConversationMessage): boolean {
  return Boolean(
    String(message.display_content ?? message.content ?? "").trim() ||
      message.fileName ||
      message.imageUrl ||
      message.attachments?.length ||
      message.previewImages?.length,
  );
}

/**
 * Project the single ephemeral progress row from lifecycle state.
 *
 * Empty streaming assistant rows are transport artifacts, not durable timeline
 * entries. Rebuilding the one visible placeholder here keeps it after every
 * persisted tool/message row and prevents reconnect/history merges from
 * accumulating multiple "Thinking" bubbles.
 */
export function projectConversationTurnProgress<
  T extends ConversationMessage,
>(messages: T[], running: boolean, progressMessage?: Partial<T>): T[] {
  const withoutTransportPlaceholders = messages.flatMap((message) => {
    if (message.role !== "assistant" || !(message.streaming || message._streaming)) {
      return [message];
    }
    const hasRenderablePayload = hasConversationMessagePayload(message);
    if (hasRenderablePayload) return [message];
    // Preserve reasoning for the analysis group, but strip its transport-only
    // streaming marker so it cannot render a second progress bubble.
    if (String(message.thinking || "").trim()) {
      return [{ ...message, streaming: false, _streaming: false } as T];
    }
    return [];
  });
  if (!running) return withoutTransportPlaceholders;
  return [
    ...withoutTransportPlaceholders,
    {
      id: "conversation-turn-progress",
      role: "assistant",
      content: "",
      streaming: true,
      _streaming: true,
      _conversationTurnProgress: true,
      ...progressMessage,
    } as T,
  ];
}

/**
 * Keep the turn progress row visible until the active turn starts rendering
 * its answer. The latest expanded reasoning/tool group also exposes current
 * activity, while expanded groups from older turns never suppress progress.
 */
export function shouldProjectConversationTurnProgress(
  entries: ConversationEntry[],
  running: boolean,
  expandedAnalysis: Readonly<Record<string, boolean>>,
): boolean {
  if (!running) return false;
  let latestAnalysisKey: string | undefined;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.type === "analysis_group") {
      latestAnalysisKey ||= entry.key;
      continue;
    }
    if (
      entry.type === "special_render" &&
      isConfirmationToolCall(entry.msg) &&
      entry.msg.toolStatus === "done"
    ) {
      // A resolved suspension card starts a new visible activity phase inside
      // the same logical turn. Content before the card must not suppress the
      // resumed progress row; content emitted after it still does.
      return latestAnalysisKey ? !expandedAnalysis[latestAnalysisKey] : true;
    }
    if (
      entry.type === "message" &&
      entry.msg.role === "assistant" &&
      hasConversationMessagePayload(entry.msg)
    ) {
      return false;
    }
    if (entry.type === "message" && entry.msg.role === "user") {
      return latestAnalysisKey ? !expandedAnalysis[latestAnalysisKey] : true;
    }
  }
  return latestAnalysisKey ? !expandedAnalysis[latestAnalysisKey] : true;
}

function pushThinking(items: ConversationAnalysisItem[], content?: string) {
  const text = (content || "").trim();
  if (!text) return;
  const previous = items[items.length - 1];
  if (previous?.type === "thinking" && previous.content === text) return;
  items.push({ type: "thinking", content: text });
}

function toolItemFromMessage(
  msg: ConversationMessage,
): Extract<ConversationAnalysisItem, { type: "tool" }> {
  const parsed = parseStoredToolPayload(msg.content);
  const name = msg.toolName || parsed.name || "tool";
  const args = msg.toolArgs ?? parsed.args ?? {};
  const result = normalizeToolResult(msg.toolResult ?? parsed.result);
  return {
    type: "tool",
    name,
    args,
    status: msg.toolStatus === "running" ? "running" : "done",
    result: result || undefined,
  };
}

export function buildConversationEntries(
  messages: ConversationMessage[],
): ConversationEntry[] {
  messages = normalizeChatTimelineMessages(messages);
  const grouped: ConversationEntry[] = [];
  let currentGroup: ConversationAnalysisItem[] | null = null;
  let currentGroupMessageIds: string[] = [];
  let groupStartIndex = 0;
  let groupStartKey = "";

  const flushGroup = () => {
    if (!currentGroup || currentGroup.length === 0) {
      currentGroup = null;
      return;
    }
    grouped.push({
      type: "analysis_group",
      items: currentGroup,
      key: `analysis-${groupStartKey || groupStartIndex}`,
      running: currentGroup.some(
        (item) => item.type === "tool" && item.status === "running",
      ),
      messageIds: currentGroupMessageIds,
    });
    currentGroup = null;
    currentGroupMessageIds = [];
    groupStartKey = "";
  };

  for (let i = 0; i < messages.length; i += 1) {
    const msg = messages[i];
    const renderType = getChatToolRenderType(msg);
    if (renderType) {
      flushGroup();
      grouped.push({
        type: "special_render",
        renderType,
        msg,
        key: msg.id || `special-${i}`,
      });
      continue;
    }

    if (msg.role === "tool_call") {
      if (!currentGroup) {
        currentGroup = [];
        groupStartIndex = i;
        groupStartKey = msg.id || msg.toolCallId || String(i);
      }
      pushThinking(currentGroup, msg.toolThinking);
      currentGroup.push(toolItemFromMessage(msg));
      currentGroupMessageIds.push(msg.id, msg.toolCallId || "");
      continue;
    }

    if (msg.role === "assistant") {
      const contentText = msg.content?.trim() || "";
      const hasAttachments =
        Array.isArray(msg.attachments) && msg.attachments.length > 0;
      const isStreamingPlaceholder = Boolean(msg.streaming || msg._streaming);
      if (msg.thinking) {
        if (!currentGroup) {
          currentGroup = [];
          groupStartIndex = i;
          groupStartKey = msg.id || msg.toolCallId || String(i);
        }
        pushThinking(currentGroup, msg.thinking);
        currentGroupMessageIds.push(msg.id);
      }
      // Empty assistant rows from streaming boundaries are transport artifacts.
      // Only the lifecycle projection may create the one canonical progress row;
      // a row carrying thinking still contributes to the analysis group above.
      if (
        !contentText &&
        !hasAttachments &&
        isStreamingPlaceholder &&
        !msg._conversationTurnProgress
      ) continue;
      if (!contentText && !hasAttachments && !isStreamingPlaceholder) continue;
      flushGroup();
      grouped.push({
        type: "message",
        msg: msg.thinking ? { ...msg, thinking: undefined } : msg,
        key: msg.id || `msg-${i}`,
      });
      continue;
    }

    flushGroup();
    grouped.push({ type: "message", msg, key: msg.id || `msg-${i}` });
  }

  flushGroup();
  return grouped;
}

function messageAnchor(msg: ConversationMessage) {
  return [
    msg.role,
    msg.id,
    msg.streaming ? "streaming" : "done",
    (msg.content || "").length,
    (msg.thinking || "").length,
    msg.toolStatus || "",
    msg.toolResult?.length || 0,
  ].join(":");
}

function analysisAnchor(items: ConversationAnalysisItem[]) {
  return items
    .map((item) => {
      if (item.type === "thinking") return `thinking:${item.content.length}`;
      return `tool:${item.name}:${item.status}:${item.result?.length || 0}`;
    })
    .join("|");
}

export function getConversationScrollAnchor(
  entries: ConversationEntry[],
  isWaiting: boolean,
) {
  const last = entries[entries.length - 1];
  const lastAnchor = !last
    ? "empty"
    : last.type === "analysis_group"
      ? `${last.key}:${analysisAnchor(last.items)}`
      : `${last.key}:${messageAnchor(last.msg)}`;
  return `${entries.length}:${isWaiting ? "waiting" : "idle"}:${lastAnchor}`;
}
