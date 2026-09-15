import { createClientId } from "../../../../utils/clientId";
import { getChatToolRenderType } from "../../../../components/ChatToolCallRenderer";
import type {
  ConversationMessage,
  ConversationToolStatus,
} from "./types";

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

export function parseStoredToolPayload(content: any): Record<string, any> {
  if (!content || typeof content !== "string") return {};
  try {
    const parsed = JSON.parse(content);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function normalizeToolStatus(status: any): ConversationToolStatus {
  return status === "running" || status === "pending" ? "running" : "done";
}

export function normalizeToolResult(result: any): string | undefined {
  if (result == null) return undefined;
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result);
  } catch {
    return String(result);
  }
}

export function eventTimelineAnchorId(data: any): string | undefined {
  const value =
    data?.timeline_anchor_id ||
    data?.timelineAnchorId ||
    data?.turn?.turn_anchor_id;
  return value ? String(value) : undefined;
}

export function toolTurnScope(message: ConversationMessage): string {
  const producer = message.producerScope
    ? `:producer:${message.producerScope}`
    : "";
  if (message.turnAnchorId) return `anchor:${message.turnAnchorId}${producer}`;
  if (message.turnGeneration != null)
    return `generation:${message.turnGeneration}${producer}`;
  if (producer) return producer.slice(1);
  return "legacy";
}

export function sameToolTurn(
  a: ConversationMessage,
  b: ConversationMessage,
): boolean {
  const aScope = toolTurnScope(a);
  const bScope = toolTurnScope(b);
  return aScope === bScope;
}

export function hasExplicitToolCallId(message: ConversationMessage): boolean {
  return Boolean(
    message.toolCallId && message._toolCallIdExplicit !== false,
  );
}

export function mergeToolCallProjection(
  previous: ConversationMessage,
  incoming: ConversationMessage,
  identitySource: "previous" | "incoming",
): ConversationMessage {
  const previousParsed = parseStoredToolPayload(previous.content);
  const incomingParsed = parseStoredToolPayload(incoming.content);
  const previousStatus = normalizeToolStatus(
    previous.toolStatus || previousParsed.status,
  );
  const incomingStatus = normalizeToolStatus(
    incoming.toolStatus || incomingParsed.status,
  );
  const incomingIsStale =
    previousStatus === "done" && incomingStatus !== "done";
  const primary = incomingIsStale ? previous : incoming;
  const secondary = incomingIsStale ? incoming : previous;
  const identity = identitySource === "incoming" ? incoming : previous;
  const fallbackIdentity = identity === incoming ? previous : incoming;
  const primaryParsed = primary === incoming ? incomingParsed : previousParsed;
  const secondaryParsed = primary === incoming ? previousParsed : incomingParsed;
  const primaryArgs = primary.toolArgs ?? primaryParsed.args;
  const secondaryArgs = secondary.toolArgs ?? secondaryParsed.args;
  const primaryResult = normalizeToolResult(
    primary.toolResult || primaryParsed.result,
  );
  const secondaryResult = normalizeToolResult(
    secondary.toolResult || secondaryParsed.result,
  );
  const selectedStatus = incomingIsStale ? previousStatus : incomingStatus;

  return {
    ...secondary,
    ...primary,
    id: identity.id || fallbackIdentity.id,
    created_at: identity.created_at || fallbackIdentity.created_at,
    timestamp: identity.timestamp || fallbackIdentity.timestamp,
    toolArgs:
      Object.keys(parseToolArgs(primaryArgs)).length > 0
        ? primaryArgs
        : secondaryArgs,
    toolStatus: selectedStatus,
    persistedMessageId: primary.persistedMessageId ?? secondary.persistedMessageId,
    toolResult: primaryResult || secondaryResult || "",
    toolResultContent: primary.toolResultContent ?? secondary.toolResultContent,
    toolSessionRef: primary.toolSessionRef ?? primaryParsed.session_ref
      ?? secondary.toolSessionRef ?? secondaryParsed.session_ref,
    streaming:
      selectedStatus === "running" &&
      Boolean(primary.streaming || secondary.streaming),
    _streaming:
      selectedStatus === "running" &&
      Boolean(primary._streaming || secondary._streaming),
  };
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
    persistedMessageId: data.persistedMessageId,
    _toolCallIdExplicit: Boolean(explicitCallId),
    toolName,
    toolArgs: parseToolArgs(data.args ?? data.toolArgs),
    toolStatus: status,
    toolResult: normalizeToolResult(data.result ?? data.toolResult) || "",
    toolResultContent: data.toolResultContent,
    toolSessionRef: data.session_ref ?? data.toolSessionRef,
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

export {
  CONFIRMATION_TOOL,
  defaultMakeId,
};
