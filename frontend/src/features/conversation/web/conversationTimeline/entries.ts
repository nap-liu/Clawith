import { buildConversationEntries } from "../../core/chatTimeline";

export type ConversationEntry = ReturnType<typeof buildConversationEntries>[number];

export function findConversationAnchorEntryIndex(
  entries: ReturnType<typeof buildConversationEntries>,
  messageId?: string,
): number {
  if (!messageId) return -1;
  return entries.findIndex((entry) =>
    entry.type === "analysis_group"
      ? entry.messageIds.includes(messageId)
      : entry.msg.id === messageId,
  );
}

export function entryMessageId(
  entry: ConversationEntry,
  focusMessageId?: string,
): string | undefined {
  if (entry.type === "analysis_group") {
    return focusMessageId && entry.messageIds.includes(focusMessageId)
      ? focusMessageId
      : entry.messageIds.find(Boolean);
  }
  return entry.msg.id;
}
