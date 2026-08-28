import {
  normalizeChatQuotedMessage,
  type ChatMessageAttachment,
  type ChatPreviewImage,
  type ChatQuotedMessage,
} from "../../../utils/chatAttachments";
import { createClientId } from "../../../utils/clientId";
import {
  getChatToolRenderIdentity,
  getChatToolRenderType,
} from "../../../components/ChatToolCallRenderer";

export type ConversationToolStatus = "running" | "done";

export type ConversationMessage = {
  id: string;
  role: "user" | "assistant" | "system" | "tool_call";
  content: string;
  thinking?: string;
  streaming?: boolean;
  created_at?: string | null;
  display_content?: string;
  attachments?: ChatMessageAttachment[];
  quoted_message?: ChatQuotedMessage;
  toolCallId?: string;
  toolName?: string;
  toolArgs?: any;
  toolStatus?: ConversationToolStatus;
  toolResult?: string;
  toolThinking?: string;
  fileName?: string;
  imageUrl?: string;
  previewImages?: ChatPreviewImage[];
  timestamp?: string;
  sender_name?: string;
  sender_user_id?: string;
  sender_agent_id?: string;
  confirmationToolCalls?: ConversationMessage[];
  turnAnchorId?: string;
  turnGeneration?: number;
  producerScope?: string;
  _conversationTurnProgress?: boolean;
  _streaming?: boolean;
  _canonicalDone?: boolean;
  _toolCallIdExplicit?: boolean;
};

/**
 * Resolve one A2A row from the perspective of the Agent page being viewed.
 *
 * A2A storage roles describe the receiving LLM turn (an incoming Agent message
 * is commonly persisted as `user`), so `role` is not a visual ownership signal.
 * Canonical sender IDs are the only reliable contract: every Agent-authored
 * message renders on the left and every human-authored message on the right.
 * Rows without canonical attribution fail closed to the Agent side instead of
 * guessing from role.
 */
export function isA2AMessageLeft(
  message: Pick<ConversationMessage, "sender_agent_id" | "sender_user_id">,
): boolean {
  const senderAgentId = String(message.sender_agent_id || "").trim();
  if (senderAgentId) return true;
  if (message.sender_user_id) return false;
  return true;
}

export type ConversationAnalysisItem =
  | { type: "thinking"; content: string }
  | {
      type: "tool";
      name: string;
      args: any;
      status: ConversationToolStatus;
      result?: string;
    };

export type ConversationEntry =
  | {
      type: "analysis_group";
      items: ConversationAnalysisItem[];
      key: string;
      running: boolean;
      messageIds: string[];
    }
  | {
      type: "special_render";
      renderType: string;
      msg: ConversationMessage;
      key: string;
    }
  | { type: "message"; msg: ConversationMessage; key: string };

export type AssistantStreamMessage = {
  type: "thinking" | "chunk" | "done";
  content?: string;
  now?: string;
  messageId?: string;
  sender_name?: string;
  sender_agent_id?: string;
  preserveTransient?: boolean;
  turnAnchorId?: string;
  turnGeneration?: number;
  turnSuspended?: boolean;
  producerScope?: string;
  transientMessageId?: string;
};

const CONFIRMATION_TOOL = "request_confirmation";

const defaultMakeId = createClientId;

export function parseToolArgs(raw: any): Record<string, any> {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  if (typeof raw !== "string") return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function parseStoredToolPayload(content: any): Record<string, any> {
  if (!content || typeof content !== "string") return {};
  try {
    const parsed = JSON.parse(content);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function normalizeToolStatus(status: any): ConversationToolStatus {
  return status === "running" || status === "pending" ? "running" : "done";
}

function normalizeToolResult(result: any): string | undefined {
  if (result == null) return undefined;
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result);
  } catch {
    return String(result);
  }
}

function eventTimelineAnchorId(data: any): string | undefined {
  const value =
    data?.timeline_anchor_id ||
    data?.timelineAnchorId ||
    data?.turn?.turn_anchor_id;
  return value ? String(value) : undefined;
}

function normalizeTurnTimelinePartition<T extends ConversationMessage>(
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
      toolThinking: raw.toolThinking || parsed.reasoning_content || "",
      timestamp: raw.timestamp || raw.created_at || undefined,
      sender_name: raw.sender_name || undefined,
      sender_user_id: raw.sender_user_id || undefined,
      sender_agent_id: raw.sender_agent_id || undefined,
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
    created_at: raw.created_at || null,
    timestamp: raw.timestamp || raw.created_at || undefined,
    sender_name: raw.sender_name || undefined,
    sender_user_id: raw.sender_user_id || undefined,
    sender_agent_id: raw.sender_agent_id || undefined,
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
    _canonicalDone: raw.canonicalDone === true || raw._canonicalDone === true,
    ...(Object.prototype.hasOwnProperty.call(raw, "display_content")
      ? { display_content: raw.display_content || "" }
      : {}),
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
    ...(Object.prototype.hasOwnProperty.call(event, "attachments")
      ? { attachments: event.attachments || [] }
      : {}),
    ...(event.sender_name ? { sender_name: event.sender_name } : {}),
    ...(event.sender_user_id || event.user_id
      ? { sender_user_id: String(event.sender_user_id || event.user_id) }
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

export function findStreamingAssistantIndex(messages: ConversationMessage[]) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.role === "assistant" && (msg.streaming || (msg as any)._streaming))
      return i;
  }
  return -1;
}

function findStreamingAssistantIndexAfterLastTool(
  messages: ConversationMessage[],
) {
  let lastToolIndex = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "tool_call") {
      lastToolIndex = i;
      break;
    }
  }
  for (let i = messages.length - 1; i > lastToolIndex; i -= 1) {
    const msg = messages[i];
    if (msg.role === "assistant" && (msg.streaming || (msg as any)._streaming))
      return i;
  }
  return -1;
}

function findStreamingAssistantIndexForAnchor(
  messages: ConversationMessage[],
  turnAnchorId: string,
) {
  const lastToolIndex = findLastScopedToolIndex(messages, turnAnchorId);
  for (let i = messages.length - 1; i > lastToolIndex; i -= 1) {
    const message = messages[i];
    if (
      message.role === "assistant" &&
      Boolean(message.streaming || message._streaming) &&
      message.turnAnchorId === turnAnchorId
    ) {
      return i;
    }
  }
  return -1;
}

function findStreamingAssistantIndexForProducer(
  messages: ConversationMessage[],
  turnAnchorId: string | undefined,
  producerScope: string,
) {
  const lastToolIndex = findLastScopedToolIndex(
    messages,
    turnAnchorId,
    producerScope,
  );
  for (let i = messages.length - 1; i > lastToolIndex; i -= 1) {
    const message = messages[i];
    if (
      message.role === "assistant" &&
      Boolean(message.streaming || message._streaming) &&
      message.producerScope === producerScope &&
      (!turnAnchorId || message.turnAnchorId === turnAnchorId)
    ) {
      return i;
    }
  }
  return -1;
}

function findLastScopedToolIndex(
  messages: ConversationMessage[],
  turnAnchorId?: string,
  producerScope?: string,
): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (
      message.role === "tool_call" &&
      (!turnAnchorId || message.turnAnchorId === turnAnchorId) &&
      (!producerScope || message.producerScope === producerScope)
    ) {
      return i;
    }
  }
  return -1;
}

export function applyAssistantDoneMessage<T extends Record<string, any>>(
  messages: T[],
  event: {
    content?: string;
    now?: string;
    messageId?: string;
    sender_name?: string;
    sender_agent_id?: string;
    preserveTransient?: boolean;
    turnAnchorId?: string;
    turnGeneration?: number;
    turnSuspended?: boolean;
    producerScope?: string;
    transientMessageId?: string;
  },
  makeId: () => string = defaultMakeId,
): T[] {
  let identifiedIdx = event.messageId
    ? messages.findIndex(
        (message) => String(message.id || "") === event.messageId,
      )
    : -1;
  const content = event.content || "";
  const now = event.now || new Date().toISOString();
  if (event.preserveTransient) {
    if (!content.trim()) return messages;
    const committedId = event.messageId || makeId();
    const existingIndex = messages.findIndex(
      (message) => String(message.id || "") === committedId,
    );
    const committed = {
      ...(existingIndex >= 0 ? messages[existingIndex] : {}),
      id: committedId,
      role: "assistant",
      content,
      created_at: now,
      timestamp: now,
      sender_name: event.sender_name,
      sender_agent_id: event.sender_agent_id,
      streaming: false,
      _streaming: false,
    } as unknown as T;
    if (existingIndex < 0) return [...messages, committed];
    return [
      ...messages.slice(0, existingIndex),
      committed,
      ...messages.slice(existingIndex + 1),
    ];
  }
  let lastUserIdx = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") {
      lastUserIdx = i;
      break;
    }
  }
  const identifiedMessage =
    identifiedIdx >= 0 ? messages[identifiedIdx] : undefined;
  const isCurrentTurnStream = (message: T, index: number) =>
    message.role === "assistant" &&
    Boolean(message.streaming || message._streaming) &&
    (event.producerScope
      ? String(message.producerScope || "") === event.producerScope &&
        (!event.turnAnchorId || message.turnAnchorId === event.turnAnchorId)
      : event.transientMessageId
        ? String(message.id || "") === event.transientMessageId
        : event.turnAnchorId
          ? String(message.turnAnchorId || "") === event.turnAnchorId
          : index > lastUserIdx);
  const streamed = messages.filter(isCurrentTurnStream);
  const streamedThinking = streamed
    .map((message) => message.thinking || "")
    .filter(Boolean)
    .join("\n\n");
  const streamedContent = streamed
    .map((message) => message.content || "")
    .filter((segment) => segment.trim())
    .join("\n\n");

  // Suspension closes the active transport streams without creating or
  // relocating a timeline message. Every row keeps the position in which the
  // turn produced it; resuming the turn may append new rows later.
  if (event.turnSuspended) {
    if (streamed.length === 0) return messages;
    const explicitContentTarget = event.messageId
      ? streamed.find(
          (message) => String(message.id || "") === event.messageId,
        )
      : streamed.length === 1
        ? streamed[0]
        : undefined;
    return messages.map((message, index) => {
      if (!isCurrentTurnStream(message, index)) return message;
      return {
        ...message,
        ...(message === explicitContentTarget && content
          ? { content }
          : {}),
        streaming: false,
        _streaming: false,
      } as T;
    });
  }

  // Explicit ids address one committed message (currently onboarding and
  // server-committed rows). Preserve its full shape and position, but still
  // collapse every transient assistant row from the same turn. A committed
  // event can arrive before `done`; returning after updating only that row
  // would strand earlier streamed fragments until a full history reload.
  if (
    event.messageId &&
    identifiedMessage &&
    !identifiedMessage.streaming &&
    !identifiedMessage._streaming
  ) {
    const next = messages.filter(
      (message, index) => !isCurrentTurnStream(message, index),
    );
    const nextIdentifiedIdx = next.findIndex(
      (message) =>
        message === identifiedMessage ||
        (identifiedMessage.id &&
          String(message.id || "") === String(identifiedMessage.id)),
    );
    if (nextIdentifiedIdx < 0) return next;
    next[nextIdentifiedIdx] = {
      ...identifiedMessage,
      content: content || identifiedMessage.content || "",
      ...(identifiedMessage.thinking || streamedThinking
        ? { thinking: identifiedMessage.thinking || streamedThinking }
        : {}),
      streaming: false,
      _streaming: false,
      _canonicalDone: true,
      created_at: identifiedMessage.created_at || now,
      timestamp: identifiedMessage.timestamp || now,
    };
    return next;
  }

  // done.content is the canonical reply for the whole logical turn. Remove
  // all temporary stream bubbles so A + tool + B becomes one durable A+B
  // reply, while ordinary non-stream assistant rows (for example a media
  // caption) remain independent.
  const next = messages.filter(
    (message, index) =>
      !isCurrentTurnStream(message, index) &&
      !(identifiedIdx === index && message.role === "assistant"),
  );
  const canonicalContent = content || streamedContent;
  if (!canonicalContent && !streamedThinking) return next;

  const canonical = {
    ...(identifiedMessage || {}),
    id: event.messageId || streamed[0]?.id || makeId(),
    role: "assistant",
    content: canonicalContent,
    ...(streamedThinking || identifiedMessage?.thinking
      ? { thinking: streamedThinking || identifiedMessage?.thinking }
      : {}),
    created_at: identifiedMessage?.created_at || streamed[0]?.created_at || now,
    timestamp: identifiedMessage?.timestamp || streamed[0]?.timestamp || now,
    sender_name: event.sender_name || identifiedMessage?.sender_name || streamed[0]?.sender_name,
    sender_agent_id:
      event.sender_agent_id || identifiedMessage?.sender_agent_id || streamed[0]?.sender_agent_id,
    turnAnchorId:
      event.turnAnchorId || identifiedMessage?.turnAnchorId || streamed[0]?.turnAnchorId,
    turnGeneration:
      event.turnGeneration ?? identifiedMessage?.turnGeneration ?? streamed[0]?.turnGeneration,
    producerScope:
      event.producerScope || identifiedMessage?.producerScope || streamed[0]?.producerScope,
    streaming: false,
    _streaming: false,
    _canonicalDone: true,
  } as unknown as T;

  // A late terminal for turn A can arrive after turn B's user message and
  // stream. Place A's durable reply at A's timeline boundary and remove only
  // A-scoped transport rows; never append it after or collapse B.
  if (event.turnAnchorId) {
    const anchorIndex = messages.findIndex(
      (message) => String(message.id || "") === event.turnAnchorId,
    );
    if (anchorIndex >= 0) {
      const nextUser = messages
        .slice(anchorIndex + 1)
        .find((message) => message.role === "user");
      const insertionIndex = nextUser
        ? next.findIndex((message) => message === nextUser)
        : next.length;
      const safeIndex = insertionIndex >= 0 ? insertionIndex : next.length;
      return [
        ...next.slice(0, safeIndex),
        canonical,
        ...next.slice(safeIndex),
      ];
    }
  }

  const last = next[next.length - 1];
  if (
    !event.messageId &&
    last?.role === "assistant" &&
    !last.streaming &&
    !last._streaming &&
    last._canonicalDone &&
    last.content === content
  ) {
    return next;
  }
  return [...next, canonical];
}

export function applyAssistantStreamMessage(
  messages: ConversationMessage[],
  event: AssistantStreamMessage,
  makeId: () => string = defaultMakeId,
): ConversationMessage[] {
  if (
    event.type !== "done" &&
    event.producerScope &&
    messages.some(
      (message) =>
        message.role === "assistant" &&
        Boolean((message as any)._canonicalDone) &&
        message.producerScope === event.producerScope &&
        (!event.turnAnchorId || message.turnAnchorId === event.turnAnchorId),
    )
  ) {
    return messages;
  }
  const scopedToolBoundary = findLastScopedToolIndex(
    messages,
    event.turnAnchorId,
    event.producerScope,
  );
  const identifiedIdx = event.messageId
    ? messages.findIndex(
        (message, index) =>
          message.id === event.messageId &&
          ((!event.turnAnchorId && !event.producerScope) ||
            index > scopedToolBoundary),
      )
    : -1;
  const producerStreamingIdx = event.producerScope
    ? findStreamingAssistantIndexForProducer(
        messages,
        event.turnAnchorId,
        event.producerScope,
      )
    : -1;
  const scopedStreamingIdx = !event.producerScope && event.turnAnchorId
    ? findStreamingAssistantIndexForAnchor(messages, event.turnAnchorId)
    : -1;
  const idx = identifiedIdx >= 0
    ? identifiedIdx
    : producerStreamingIdx >= 0
        ? producerStreamingIdx
        : scopedStreamingIdx >= 0
          ? scopedStreamingIdx
          : event.messageId
            ? -1
          : event.turnAnchorId || event.producerScope
        ? -1
        : findStreamingAssistantIndexAfterLastTool(messages);
  const content = event.content || "";
  const now = event.now || new Date().toISOString();
  const nextSegmentId = () =>
    event.messageId && !messages.some((message) => message.id === event.messageId)
      ? event.messageId
      : makeId();

  if (event.type === "thinking") {
    if (idx >= 0) {
      const next = [...messages];
      next[idx] = {
        ...next[idx],
        thinking: (next[idx].thinking || "") + content,
        sender_name: event.sender_name || next[idx].sender_name,
        sender_agent_id: event.sender_agent_id || next[idx].sender_agent_id,
        turnAnchorId: event.turnAnchorId || next[idx].turnAnchorId,
        turnGeneration: event.turnGeneration ?? next[idx].turnGeneration,
        producerScope: event.producerScope || next[idx].producerScope,
        streaming: true,
        _streaming: true,
      };
      return next;
    }
    return [
      ...messages,
      {
        id: nextSegmentId(),
        role: "assistant",
        content: "",
        thinking: content,
        sender_name: event.sender_name,
        sender_agent_id: event.sender_agent_id,
        turnAnchorId: event.turnAnchorId,
        turnGeneration: event.turnGeneration,
        producerScope: event.producerScope,
        streaming: true,
        _streaming: true,
      },
    ];
  }

  if (event.type === "chunk") {
    if (idx >= 0) {
      const next = [...messages];
      next[idx] = {
        ...next[idx],
        content: next[idx].content + content,
        sender_name: event.sender_name || next[idx].sender_name,
        sender_agent_id: event.sender_agent_id || next[idx].sender_agent_id,
        turnAnchorId: event.turnAnchorId || next[idx].turnAnchorId,
        turnGeneration: event.turnGeneration ?? next[idx].turnGeneration,
        producerScope: event.producerScope || next[idx].producerScope,
        streaming: true,
        _streaming: true,
      };
      return next;
    }
    return [
      ...messages,
      {
        id: nextSegmentId(),
        role: "assistant",
        content,
        sender_name: event.sender_name,
        sender_agent_id: event.sender_agent_id,
        turnAnchorId: event.turnAnchorId,
        turnGeneration: event.turnGeneration,
        producerScope: event.producerScope,
        streaming: true,
        _streaming: true,
      },
    ];
  }

  return applyAssistantDoneMessage(messages, event, makeId);
}

function toolTurnScope(message: ConversationMessage): string {
  const producer = message.producerScope
    ? `:producer:${message.producerScope}`
    : "";
  if (message.turnAnchorId) return `anchor:${message.turnAnchorId}${producer}`;
  if (message.turnGeneration != null)
    return `generation:${message.turnGeneration}${producer}`;
  if (producer) return producer.slice(1);
  return "legacy";
}

function sameToolTurn(a: ConversationMessage, b: ConversationMessage): boolean {
  const aScope = toolTurnScope(a);
  const bScope = toolTurnScope(b);
  return aScope === bScope;
}

function hasExplicitToolCallId(message: ConversationMessage): boolean {
  return Boolean(
    message.toolCallId && message._toolCallIdExplicit !== false,
  );
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

  if (localOnly.length === 0) return history;

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
      nextHistoryIndex >= 0 ? nextHistoryIndex : history.length;
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
        const historyTime = messageTime(history[index]);
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
  for (let index = 0; index <= history.length; index += 1) {
    const localBucket = buckets.get(index);
    if (localBucket) merged.push(...localBucket);
    if (index < history.length) merged.push(history[index]);
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

export function getToolTargetKey(args: any): string {
  const parsed = parseToolArgs(args);
  const value =
    parsed.path ||
    parsed.file_path ||
    parsed.output_path ||
    parsed.target_path ||
    parsed.filename ||
    parsed.url ||
    parsed.query ||
    parsed.name ||
    "";
  return typeof value === "string" ? value.trim() : "";
}

export function upsertToolCallMessage(
  messages: ConversationMessage[],
  toolMsg: ConversationMessage,
) {
  const incomingTarget = getToolTargetKey(toolMsg.toolArgs);
  const sameTurnIdentity = (msg: ConversationMessage) =>
    sameToolTurn(msg, toolMsg);
  const exactIdMatch = (msg: ConversationMessage) =>
    msg.role === "tool_call" &&
    !!toolMsg.toolCallId &&
    msg.toolCallId === toolMsg.toolCallId &&
    sameTurnIdentity(msg);
  const sameTool = (msg: ConversationMessage) =>
    exactIdMatch(msg) ||
    (msg.role === "tool_call" &&
      !toolMsg._toolCallIdExplicit &&
      sameTurnIdentity(msg) &&
      msg.toolName === toolMsg.toolName &&
      msg.toolStatus === "running" &&
      ((!!incomingTarget &&
        getToolTargetKey(msg.toolArgs) === incomingTarget) ||
        (!toolMsg.toolCallId && !incomingTarget)));
  const runningIdx = [...messages].reverse().findIndex(sameTool);
  if (runningIdx < 0) return [...messages, toolMsg];

  const idx = messages.length - 1 - runningIdx;
  const previous = messages[idx];
  if (previous.toolStatus === "done" && toolMsg.toolStatus !== "done") {
    return messages;
  }
  const nextToolArgs =
    Object.keys(parseToolArgs(toolMsg.toolArgs)).length > 0
      ? toolMsg.toolArgs
      : previous.toolArgs;
  const merged = {
    ...previous,
    ...toolMsg,
    toolArgs: nextToolArgs,
    id: previous.id || toolMsg.id,
    created_at: previous.created_at || toolMsg.created_at,
  };
  return [...messages.slice(0, idx), merged, ...messages.slice(idx + 1)];
}

export function applyConfirmationRequiredEvent(
  messages: ConversationMessage[],
  data: any,
  makeId: () => string = defaultMakeId,
  now = new Date().toISOString(),
): ConversationMessage[] {
  const rejectedOptimisticId = String(data.message_id || "");
  const withoutRejectedAttempt = rejectedOptimisticId
    ? messages.filter(
        (message) => String(message.id || "") !== rejectedOptimisticId,
      )
    : messages;
  return upsertToolCallMessage(
    withoutRejectedAttempt,
    toolCallMessageFromEvent(
      { ...data, status: "running", name: data.name || CONFIRMATION_TOOL },
      makeId,
      now,
    ),
  );
}

export function applyAssistantMessageCommitted(
  messages: ConversationMessage[],
  data: any,
  makeId: () => string = defaultMakeId,
  preserveTransient = false,
): ConversationMessage[] {
  const committed = mapHistoryMessage(
    { ...data, id: data.id || data.message_id, role: "assistant" },
    makeId,
  );
  if (!committed) return messages;
  const index = messages.findIndex((message) => message.id === committed.id);
  const staged =
    index < 0
      ? [...messages, committed]
      : [
          ...messages.slice(0, index),
          { ...messages[index], ...committed },
          ...messages.slice(index + 1),
        ];
  if (preserveTransient) return staged;
  const folded = applyAssistantDoneMessage(index < 0 ? messages : staged, {
    content: committed.content,
    now: committed.created_at || committed.timestamp || undefined,
    messageId: committed.id,
    transientMessageId: data.transient_message_id
      ? String(data.transient_message_id)
      : undefined,
    producerScope: data.producer_scope
      ? String(data.producer_scope)
      : committed.producerScope,
    sender_name: committed.sender_name,
    sender_agent_id: committed.sender_agent_id,
    turnAnchorId: eventTimelineAnchorId(data) || committed.turnAnchorId,
    turnGeneration: Number.isInteger(Number(data.turn?.generation))
      ? Number(data.turn.generation)
      : committed.turnGeneration,
  }, makeId);
  const committedIndex = folded.findIndex(
    (message) => message.id === committed.id,
  );
  if (committedIndex < 0) return folded;
  const finalized = {
    ...folded[committedIndex],
    ...committed,
    thinking: committed.thinking || folded[committedIndex].thinking,
    streaming: false,
    _streaming: false,
    _canonicalDone: true,
  };
  return [
    ...folded.slice(0, committedIndex),
    finalized,
    ...folded.slice(committedIndex + 1),
  ];
}

export type ConversationTimelineFoldResult<T extends ConversationMessage> = {
  messages: T[];
  handled: boolean;
};

/**
 * The single message-semantic fold used by every chat surface.
 *
 * Page adapters may still batch stream frames or react to workspace/toast
 * events, but they do not decide how a transport event mutates the timeline.
 * In particular, workspace drafts are page side effects and never tool rows.
 */
export function foldConversationTimelineEvent<T extends ConversationMessage>(
  messages: T[],
  data: Record<string, any>,
  options: {
    makeId?: () => string;
    now?: string;
    preserveTransient?: boolean;
  } = {},
): ConversationTimelineFoldResult<T> {
  const makeId = options.makeId || defaultMakeId;
  const now = options.now || new Date().toISOString();
  const type = String(data.type || "");
  const rejectedMessageId = String(data.rejected_message_id || "");
  const baseMessages = rejectedMessageId
    ? messages.filter((message) => String(message.id || "") !== rejectedMessageId)
    : messages;
  if (type === "workspace_draft") return { messages, handled: false };
  if (type === "turn_receipt") return { messages: baseMessages, handled: true };
  if (type === "user_message_committed") {
    return {
      messages: applyUserMessageCommitted(baseMessages, data, makeId),
      handled: true,
    };
  }
  if (type === "assistant_message_committed") {
    const folded = applyAssistantMessageCommitted(
      baseMessages,
      data,
      makeId,
      options.preserveTransient === true,
    ) as T[];
    return {
      messages: normalizeTurnTimelinePartition(
        folded,
        eventTimelineAnchorId(data),
      ),
      handled: true,
    };
  }
  if (type === "confirmation_required") {
    const folded = applyConfirmationRequiredEvent(
      baseMessages,
      data,
      makeId,
      now,
    ) as T[];
    return {
      messages: normalizeTurnTimelinePartition(
        folded,
        eventTimelineAnchorId(data),
      ),
      handled: true,
    };
  }
  if (type === "tool_call") {
    const folded = upsertToolCallMessage(
      baseMessages,
      toolCallMessageFromEvent(data, makeId, now),
    ) as T[];
    return {
      messages: normalizeTurnTimelinePartition(
        folded,
        eventTimelineAnchorId(data),
      ),
      handled: true,
    };
  }
  if (type === "thinking" || type === "chunk" || type === "done") {
    const folded = applyAssistantStreamMessage(
      baseMessages,
      {
        type,
        content: String(data.content || ""),
        now,
        messageId: data.message_id ? String(data.message_id) : undefined,
        sender_name: data.sender_name,
        sender_agent_id: data.sender_agent_id,
        preserveTransient: options.preserveTransient === true,
        turnAnchorId: eventTimelineAnchorId(data),
        turnGeneration: Number.isInteger(Number(data.turn?.generation))
          ? Number(data.turn.generation)
          : undefined,
        turnSuspended: data.turn?.phase === "suspended",
        producerScope: data.producer_scope
          ? String(data.producer_scope)
          : undefined,
        transientMessageId: data.transient_message_id
          ? String(data.transient_message_id)
          : undefined,
      },
      makeId,
    ) as T[];
    return {
      messages: normalizeTurnTimelinePartition(
        folded,
        eventTimelineAnchorId(data),
      ),
      handled: true,
    };
  }
  if (type === "channel_user_message") {
    const committed = mapHistoryMessage(
      { ...data, role: "user", id: data.id || makeId() },
      makeId,
    ) as T | null;
    return {
      messages:
        committed && !messages.some((message) => message.id === committed.id)
          ? [...baseMessages, committed]
          : baseMessages,
      handled: true,
    };
  }
  if (rejectedMessageId) return { messages: baseMessages, handled: true };
  return { messages, handled: false };
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
    const previousParsed = parseStoredToolPayload(previous.content);
    const previousStatus = normalizeToolStatus(
      previous.toolStatus || previousParsed.status,
    );
    const incomingStatus = normalizeToolStatus(
      message.toolStatus || parsed.status,
    );
    if (previousStatus === "done" && incomingStatus !== "done") continue;

    normalized[index] = {
      ...previous,
      ...message,
      id: previous.id || message.id,
      created_at: previous.created_at || message.created_at,
      toolArgs:
        Object.keys(parseToolArgs(message.toolArgs ?? parsed.args)).length > 0
          ? (message.toolArgs ?? parsed.args)
          : (previous.toolArgs ?? previousParsed.args),
    };
  }

  return normalized;
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
    const hasRenderablePayload = Boolean(
      String(message.content || "").trim() ||
        message.fileName ||
        message.imageUrl ||
        message.attachments?.length ||
        message.previewImages?.length,
    );
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
 * Keep the turn progress row visible for the whole active lifecycle unless
 * the latest reasoning/tool group in the active turn is already exposing that
 * activity. Expanded groups from older turns never suppress new-turn progress.
 */
export function shouldProjectConversationTurnProgress(
  entries: ConversationEntry[],
  running: boolean,
  expandedAnalysis: Readonly<Record<string, boolean>>,
): boolean {
  if (!running) return false;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.type === "analysis_group") {
      return !expandedAnalysis[entry.key];
    }
    if (entry.type === "message" && entry.msg.role === "user") return true;
  }
  return true;
}

export function toolCallMessageFromEvent(
  data: any,
  makeId: () => string = defaultMakeId,
  now = new Date().toISOString(),
): ConversationMessage {
  const toolName = data.name || data.toolName || "tool";
  const explicitCallId = data.call_id || data.toolCallId || data.id;
  const callId = String(
    explicitCallId ||
      `${toolName}-${data.index ?? 0}`,
  );
  const status = normalizeToolStatus(data.status);
  const turn = data.turn && typeof data.turn === "object" ? data.turn : {};
  const turnAnchorId = eventTimelineAnchorId(data);
  const producerScope = data.producer_scope
    ? String(data.producer_scope)
    : undefined;
  const scopedUiId =
    turnAnchorId || producerScope
      ? `tool:${turnAnchorId || "unanchored"}:${producerScope || "default"}:${callId}`
      : callId;
  return {
    id: scopedUiId || makeId(),
    role: "tool_call",
    content: "",
    created_at: now,
    streaming: status === "running",
    toolCallId: callId,
    _toolCallIdExplicit: Boolean(explicitCallId),
    toolName,
    toolArgs: parseToolArgs(data.args ?? data.toolArgs),
    toolStatus: status,
    toolResult: normalizeToolResult(data.result ?? data.toolResult) || "",
    toolThinking: data.reasoning_content || data.toolThinking || "",
    turnAnchorId,
    turnGeneration: Number.isInteger(Number(turn.generation))
      ? Number(turn.generation)
      : undefined,
    producerScope,
  };
}

export function isConfirmationToolCall(msg: ConversationMessage) {
  return getChatToolRenderType(msg) === "confirmation";
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
