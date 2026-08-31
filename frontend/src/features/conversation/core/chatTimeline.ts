export type {
  AssistantStreamMessage,
  ConversationAnalysisItem,
  ConversationEntry,
  ConversationMessage,
  ConversationTimelineFoldResult,
  ConversationToolStatus,
} from "./chatTimeline/types";

export { isA2AMessageLeft } from "./chatTimeline/types";

export { parseToolArgs } from "./chatTimeline/tooling";

export {
  applyUserMessageCommitted,
  hasPendingConfirmation,
  isSameMessage,
  latestHistoryWindowOverlaps,
  mapHistoryMessage,
  mergeHistoryMessages,
  normalizeChatTimelineMessages,
  reconcileLatestHistoryWindow,
} from "./chatTimeline/history";

export {
  applyAssistantDoneMessage,
  applyAssistantMessageCommitted,
  applyAssistantStreamMessage,
  applyConfirmationRequiredEvent,
  findStreamingAssistantIndex,
  foldConversationTimelineEvent,
  upsertToolCallMessage,
} from "./chatTimeline/assistantStreaming";

export {
  getToolTargetKey,
  isConfirmationToolCall,
  toolCallMessageFromEvent,
} from "./chatTimeline/tooling";

export {
  buildConversationEntries,
  getConversationScrollAnchor,
  projectConversationTurnProgress,
  shouldProjectConversationTurnProgress,
} from "./chatTimeline/entries";
