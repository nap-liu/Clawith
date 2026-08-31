import type {
  AssistantStreamMessage,
  ConversationMessage,
  ConversationTimelineFoldResult,
} from "./types";
import {
  CONFIRMATION_TOOL,
  defaultMakeId,
  eventTimelineAnchorId,
  getToolTargetKey,
  mergeToolCallProjection,
  parseToolArgs,
  sameToolTurn,
  toolCallMessageFromEvent,
} from "./tooling";
import {
  applyUserMessageCommitted,
  mapHistoryMessage,
  normalizeTurnTimelinePartition,
} from "./history";

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
  const identifiedIdx = event.messageId
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
  const nextToolArgs =
    Object.keys(parseToolArgs(toolMsg.toolArgs)).length > 0
      ? toolMsg.toolArgs
      : previous.toolArgs;
  const merged = mergeToolCallProjection(previous, {
    ...toolMsg,
    toolArgs: nextToolArgs,
  }, "previous");
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
