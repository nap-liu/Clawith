import {
  normalizeChatQuotedMessage,
  type ChatMessageAttachment,
} from "../../../../utils/chatAttachments";
import { getChatToolRenderIdentity } from "../../../../components/ChatToolCallRenderer";
import type { ConversationMessage } from "./types";
import {
  CONFIRMATION_TOOL,
  defaultMakeId,
  hasExplicitToolCallId,
  mergeToolCallProjection,
  normalizeToolResult,
  normalizeToolStatus,
  parseStoredToolPayload,
  parseToolArgs,
  sameToolTurn,
  toolTurnScope,
} from "./tooling";

function userContextProjection(raw: Record<string, any>) {
  const metadata = raw.message_meta ?? raw.metadata ?? {};
  const contextSource = Object.prototype.hasOwnProperty.call(raw, "external_context")
    ? raw
    : metadata;
  return {
    ...(typeof raw.display_content === "string"
      ? { display_content: raw.display_content }
      : typeof metadata.display_content === "string"
        ? { display_content: metadata.display_content }
        : {}),
    ...(Object.prototype.hasOwnProperty.call(contextSource, "external_context")
      ? { external_context: contextSource.external_context }
      : {}),
  };
}

export function normalizeTurnTimelinePartition<T extends ConversationMessage>(
  messages: T[],
  turnAnchorId?: string,
): T[] {
  if (!turnAnchorId) return messages;
  const anchorIndex = messages.findIndex(
    (message) => message.role === "user" && message.id === turnAnchorId,
  );
  if (anchorIndex < 0) return messages;
  const scoped = messages.filter(
    (message) =>
      message.role !== "user" && message.turnAnchorId === turnAnchorId,
  );
  if (!scoped.length) return messages;
  const scopedSet = new Set(scoped);
  const remaining = messages.filter((message) => !scopedSet.has(message));
  const remainingAnchorIndex = remaining.findIndex(
    (message) => message.role === "user" && message.id === turnAnchorId,
  );
  const nextUserOffset = remaining
    .slice(remainingAnchorIndex + 1)
    .findIndex((message) => message.role === "user");
  const insertionIndex =
    nextUserOffset < 0
      ? remaining.length
      : remainingAnchorIndex + 1 + nextUserOffset;
  return [
    ...remaining.slice(0, insertionIndex),
    ...scoped,
    ...remaining.slice(insertionIndex),
  ];
}

export function mapHistoryMessage(
  raw: any,
  makeId: () => string = defaultMakeId,
): ConversationMessage | null {
  if (!raw || !["user", "assistant", "system", "tool_call"].includes(raw.role))
    return null;

  if (raw.role === "tool_call") {
    const parsed = parseStoredToolPayload(raw.content);
    const metadata =
      raw.message_meta && typeof raw.message_meta === "object"
        ? raw.message_meta
        : raw.metadata && typeof raw.metadata === "object"
          ? raw.metadata
          : {};
    const id = String(raw.toolCallId || raw.id || makeId());
    const toolArgs = raw.toolArgs ?? parsed.args ?? parsed.arguments ?? {};
    return {
      id,
      role: "tool_call",
      content: parsed.name ? "" : raw.content || "",
      created_at: raw.created_at || null,
      toolCallId: String(raw.toolCallId || parsed.call_id || parsed.id || id),
      _toolCallIdExplicit:
        typeof raw.toolCallIdExplicit === "boolean"
          ? raw.toolCallIdExplicit
          : Boolean(parsed.call_id || parsed.id),
      toolName: raw.toolName || parsed.name || parsed.tool_name || "tool",
      toolArgs,
      toolStatus: normalizeToolStatus(raw.toolStatus || parsed.status),
      toolResult: normalizeToolResult(raw.toolResult ?? parsed.result) || "",
      toolSessionRef: raw.toolSessionRef ?? parsed.session_ref,
      toolThinking: raw.toolThinking || parsed.reasoning_content || "",
      mediaTaskId: raw.mediaTaskId || undefined,
      timestamp: raw.timestamp || raw.created_at || undefined,
      sender_name: raw.sender_name || undefined,
      sender_user_id: raw.sender_user_id || undefined,
      sender_agent_id: raw.sender_agent_id || undefined,
      sender_avatar_url: raw.sender_avatar_url || undefined,
      turnAnchorId:
        raw.timeline_anchor_id ||
        raw.turnAnchorId ||
        metadata.turn_anchor_id ||
        undefined,
      turnGeneration:
        raw.turnGeneration ?? metadata.turn_generation ?? undefined,
      producerScope:
        raw.producerScope ||
        raw.producer_scope ||
        metadata.producer_scope ||
        metadata.project_timeline?.subagent_session_id ||
        undefined,
    };
  }

  const metadata =
    raw.message_meta && typeof raw.message_meta === "object"
      ? raw.message_meta
      : raw.metadata && typeof raw.metadata === "object"
        ? raw.metadata
        : {};
  const quotedMessage = normalizeChatQuotedMessage(raw.quoted_message);
  return {
    id: String(raw.id || makeId()),
    role: raw.role,
    content: raw.content || "",
    thinking: raw.thinking || undefined,
    mediaTaskId: raw.mediaTaskId || undefined,
    created_at: raw.created_at || null,
    timestamp: raw.timestamp || raw.created_at || undefined,
    sender_name: raw.sender_name || undefined,
    sender_user_id: raw.sender_user_id || undefined,
    sender_agent_id: raw.sender_agent_id || undefined,
    sender_avatar_url: raw.sender_avatar_url || undefined,
    turnAnchorId:
      raw.timeline_anchor_id ||
      raw.turnAnchorId ||
      raw.turnAnchorId ||
      metadata.turn_anchor_id ||
      undefined,
    turnGeneration:
      raw.turnGeneration ?? metadata.turn_generation ?? undefined,
    producerScope:
      raw.producerScope ||
      raw.producer_scope ||
      metadata.producer_scope ||
      metadata.project_timeline?.subagent_session_id ||
      undefined,
    _canonicalDone: raw.canonicalDone === true || raw._canonicalDone === true,
    ...(Object.prototype.hasOwnProperty.call(raw, "display_content")
      ? { display_content: raw.display_content || "" }
      : {}),
    ...(raw.role === "user" ? userContextProjection(raw) : {}),
    ...(Object.prototype.hasOwnProperty.call(raw, "attachments")
      ? { attachments: raw.attachments || [] }
      : {}),
    ...(quotedMessage ? { quoted_message: quotedMessage } : {}),
  };
}

export function hasPendingConfirmation(
  messages: ConversationMessage[],
): boolean {
  return messages.some(
    (message) =>
      message.role === "tool_call" &&
      message.toolName === CONFIRMATION_TOOL &&
      message.toolStatus === "running" &&
      parseToolArgs(message.toolArgs).force_confirmation !== false,
  );
}

export function applyUserMessageCommitted<T extends Record<string, any>>(
  messages: T[],
  event: Record<string, any>,
  makeId: () => string = defaultMakeId,
): T[] {
  const clientId = String(event.client_message_id || "");
  const durableId = String(event.message_id || event.id || "");
  if (!durableId) return messages;
  const clientIndex = clientId
    ? messages.findIndex((message) => String(message.id || "") === clientId)
    : -1;
  const durableIndex = messages.findIndex(
    (message) => String(message.id || "") === durableId,
  );
  const index = clientIndex >= 0 ? clientIndex : durableIndex;
  const hasFullRow = Object.prototype.hasOwnProperty.call(event, "content");
  if (index < 0 && !hasFullRow) return messages;
  const existing: Record<string, any> = index >= 0 ? messages[index] : {};
  const committed = {
    ...existing,
    id: durableId || makeId(),
    role: "user",
    ...(hasFullRow ? { content: String(event.content || "") } : {}),
    ...(Object.prototype.hasOwnProperty.call(event, "display_content")
      ? { display_content: String(event.display_content || "") }
      : {}),
    ...userContextProjection(event),
    ...(Object.prototype.hasOwnProperty.call(event, "attachments")
      ? { attachments: event.attachments || [] }
      : {}),
    ...(event.sender_name ? { sender_name: event.sender_name } : {}),
    ...(event.sender_user_id || event.user_id
      ? { sender_user_id: String(event.sender_user_id || event.user_id) }
      : {}),
    ...(event.sender_avatar_url
      ? { sender_avatar_url: event.sender_avatar_url }
      : {}),
    created_at: event.created_at || existing.created_at,
    timestamp: event.created_at || existing.timestamp,
  } as unknown as T;
  if (index < 0) return [...messages, committed];
  const next = [...messages];
  next[index] = committed;
  if (clientIndex >= 0 && durableIndex >= 0 && durableIndex !== clientIndex) {
    next.splice(durableIndex, 1);
  }
  return next;
}

export function isSameMessage(a: ConversationMessage, b: ConversationMessage) {
  if (a.role === "tool_call" || b.role === "tool_call") {
    if (a.role !== b.role) return false;
    if (a.toolCallId && a.toolCallId === b.toolCallId && sameToolTurn(a, b))
      return true;
    if (hasExplicitToolCallId(a) || hasExplicitToolCallId(b)) return false;
    const aRenderIdentity = getChatToolRenderIdentity(a);
    return (
      !!aRenderIdentity &&
      sameToolTurn(a, b) &&
      aRenderIdentity === getChatToolRenderIdentity(b)
    );
  }
  return (
    a.role === b.role &&
    a.content === b.content &&
    (a.thinking || "") === (b.thinking || "") &&
    (a.toolCallId || "") === (b.toolCallId || "")
  );
}

export function mergeHistoryMessages(
  prev: ConversationMessage[],
  history: ConversationMessage[],
) {
  if (history.length === 0) return prev;
  const projectedHistory = [...history];

  const mergeKeys = (message: ConversationMessage) => {
    if (message.role === "tool_call") {
      const keys: string[] = [];
      const scope = toolTurnScope(message);
      if (hasExplicitToolCallId(message))
        keys.push(`tool-call:${scope}:${message.toolCallId}`);
      else {
        const renderIdentity = getChatToolRenderIdentity(message);
        if (renderIdentity) keys.push(`tool-render:${scope}:${renderIdentity}`);
      }
      return keys;
    }
    const keys: string[] = [];
    if (message.id) keys.push(`message:${message.id}`);
    keys.push(
      JSON.stringify([
        message.role,
        message.content,
        message.thinking || "",
        message.toolCallId || "",
      ]),
    );
    return keys;
  };
  const historyBuckets = new Map<string, number[]>();
  history.forEach((message, index) => {
    mergeKeys(message).forEach((key) => {
      const bucket = historyBuckets.get(key);
      if (bucket) bucket.push(index);
      else historyBuckets.set(key, [index]);
    });
  });
  const bucketOffsets = new Map<string, number>();
  const usedHistoryIndexes = new Set<number>();
  const assistantContents = history
    .filter((item) => item.role === "assistant" && !!item.content)
    .map((item) => item.content);
  const localOnly: Array<{
    message: ConversationMessage;
    localIndex: number;
    previousHistoryIndex: number;
  }> = [];
  const matchedHistoryByLocalIndex = new Map<number, number>();
  let previousHistoryIndex = -1;

  for (let localIndex = 0; localIndex < prev.length; localIndex += 1) {
    const local = prev[localIndex];
    let matchedHistoryIndex = -1;
    for (const key of mergeKeys(local)) {
      const bucket = historyBuckets.get(key);
      let offset = bucketOffsets.get(key) || 0;
      while (
        bucket &&
        offset < bucket.length &&
        usedHistoryIndexes.has(bucket[offset])
      )
        offset += 1;
      bucketOffsets.set(key, offset);
      if (bucket && offset < bucket.length) {
        matchedHistoryIndex = bucket[offset];
        bucketOffsets.set(key, offset + 1);
        break;
      }
    }
    if (matchedHistoryIndex >= 0) {
      if (
        local.role === "tool_call" &&
        projectedHistory[matchedHistoryIndex]?.role === "tool_call"
      ) {
        projectedHistory[matchedHistoryIndex] = mergeToolCallProjection(
          local,
          projectedHistory[matchedHistoryIndex],
          "incoming",
        );
      }
      usedHistoryIndexes.add(matchedHistoryIndex);
      matchedHistoryByLocalIndex.set(localIndex, matchedHistoryIndex);
      previousHistoryIndex = matchedHistoryIndex;
      continue;
    }

    if (
      local.streaming &&
      assistantContents.some(
        (content) => !local.content || content.includes(local.content),
      )
    ) {
      continue;
    }

    localOnly.push({ message: local, localIndex, previousHistoryIndex });
  }

  if (localOnly.length === 0) return projectedHistory;

  const messageTime = (message: ConversationMessage) => {
    const value = message.created_at || message.timestamp;
    const parsed = value ? Date.parse(value) : Number.NaN;
    return Number.isFinite(parsed) ? parsed : null;
  };
  const buckets = new Map<number, ConversationMessage[]>();
  for (const local of localOnly) {
    let nextHistoryIndex = -1;
    for (let index = local.localIndex + 1; index < prev.length; index += 1) {
      const matched = matchedHistoryByLocalIndex.get(index);
      if (matched != null) {
        nextHistoryIndex = matched;
        break;
      }
    }

    const lowerBound = Math.max(0, local.previousHistoryIndex + 1);
    const upperBound =
      nextHistoryIndex >= 0 ? nextHistoryIndex : projectedHistory.length;
    const localTime = messageTime(local.message);
    // A transient without a server timestamp was already visible before
    // this history request began. Durable rows that appear only in the new
    // snapshot were committed during the disconnect/recovery window, so
    // keep the transient immediately after its previous matched anchor.
    // Non-transient local rows retain the conservative upper-bound policy.
    let insertionIndex =
      localTime == null && (local.message.streaming || local.message._streaming)
        ? lowerBound
        : upperBound;
    if (localTime != null) {
      for (let index = lowerBound; index < upperBound; index += 1) {
        const historyTime = messageTime(projectedHistory[index]);
        if (historyTime != null && historyTime > localTime) {
          insertionIndex = index;
          break;
        }
      }
    }
    const bucket = buckets.get(insertionIndex);
    if (bucket) bucket.push(local.message);
    else buckets.set(insertionIndex, [local.message]);
  }

  const merged: ConversationMessage[] = [];
  for (let index = 0; index <= projectedHistory.length; index += 1) {
    const localBucket = buckets.get(index);
    if (localBucket) merged.push(...localBucket);
    if (index < projectedHistory.length) merged.push(projectedHistory[index]);
  }
  return merged;
}

function stableMessageKeys(message: ConversationMessage): string[] {
  const keys: string[] = [];
  if (message.id) keys.push(`message:${message.id}`);
  const scope = toolTurnScope(message);
  if (hasExplicitToolCallId(message))
    keys.push(`tool-call:${scope}:${message.toolCallId}`);
  else {
    const renderIdentity = getChatToolRenderIdentity(message);
    if (renderIdentity) keys.push(`tool-render:${scope}:${renderIdentity}`);
  }
  return keys;
}

/**
 * Reconcile a freshly fetched latest-N window without discarding an already
 * loaded older prefix. This is used after reconnect/resume; unlike
 * mergeHistoryMessages(), the fetched history is not the complete timeline.
 */
function latestHistoryOverlapIndex(
  prev: ConversationMessage[],
  latestWindow: ConversationMessage[],
) {
  const latestStableKeys = new Set<string>();
  latestWindow.forEach((message) => {
    stableMessageKeys(message).forEach((key) => latestStableKeys.add(key));
  });
  let overlapIndex = prev.findIndex((message) =>
    stableMessageKeys(message).some((key) => latestStableKeys.has(key)),
  );

  // Optimistic/live rows can have a temporary id that differs from the
  // durable row. Limit content fallback to the recent tail so an old repeated
  // message cannot be mistaken for the latest-window overlap boundary.
  if (overlapIndex < 0) {
    const fallbackStart = Math.max(0, prev.length - latestWindow.length * 2);
    const fallbackOffset = prev
      .slice(fallbackStart)
      .findIndex((message) =>
        latestWindow.some((candidate) => isSameMessage(message, candidate)),
      );
    if (fallbackOffset >= 0) overlapIndex = fallbackStart + fallbackOffset;
  }
  return overlapIndex;
}

export function latestHistoryWindowOverlaps(
  prev: ConversationMessage[],
  latestWindow: ConversationMessage[],
) {
  return (
    prev.length === 0 ||
    latestWindow.length === 0 ||
    latestHistoryOverlapIndex(prev, latestWindow) >= 0
  );
}

export function reconcileLatestHistoryWindow(
  prev: ConversationMessage[],
  latestWindow: ConversationMessage[],
) {
  if (prev.length === 0) return latestWindow;
  if (latestWindow.length === 0) return prev;

  const overlapIndex = latestHistoryOverlapIndex(prev, latestWindow);

  // No overlap means more than one window may have arrived while the page was
  // suspended. Reset to the latest complete window instead of creating a
  // permanent middle gap; callers reset the older-page cursor accordingly.
  if (overlapIndex < 0) return latestWindow;
  return [
    ...prev.slice(0, overlapIndex),
    ...mergeHistoryMessages(prev.slice(overlapIndex), latestWindow),
  ];
}

export function normalizeChatTimelineMessages<T extends Record<string, any>>(
  messages: T[],
): T[] {
  const normalized: T[] = [];
  const toolIndexByCallId = new Map<string, number>();

  for (const message of messages) {
    if (message?.role === "assistant") {
      const hasContent =
        typeof message.content === "string"
          ? message.content.trim().length > 0
          : Boolean(message.content);
      const hasThinking =
        typeof message.thinking === "string"
          ? message.thinking.trim().length > 0
          : Boolean(message.thinking);
      const hasAttachment = Boolean(
        message.fileName ||
        message.imageUrl ||
        (Array.isArray(message.previewImages) &&
          message.previewImages.length > 0) ||
        (Array.isArray(message.attachments) &&
          message.attachments.some(
            (attachment: ChatMessageAttachment) =>
              attachment.kind !== "audio" && attachment.kind !== "video",
          )),
      );
      const isStreaming = Boolean(message.streaming || message._streaming);
      if (!hasContent && !hasThinking && !hasAttachment && !isStreaming) {
        continue;
      }
    }

    if (message?.role !== "tool_call") {
      normalized.push(message);
      continue;
    }

    const parsed = parseStoredToolPayload(message.content);
    const callId = String(
      message.toolCallId || parsed.call_id || parsed.id || "",
    );
    const identity = `${toolTurnScope(message as unknown as ConversationMessage)}:${callId}`;
    if (!callId || !toolIndexByCallId.has(identity)) {
      if (callId) toolIndexByCallId.set(identity, normalized.length);
      normalized.push(message);
      continue;
    }

    const index = toolIndexByCallId.get(identity)!;
    const previous = normalized[index];
    normalized[index] = mergeToolCallProjection(
      previous as unknown as ConversationMessage,
      {
        ...message,
        toolArgs: message.toolArgs ?? parsed.args,
      } as unknown as ConversationMessage,
      "previous",
    ) as unknown as T;
  }

  return normalized;
}
