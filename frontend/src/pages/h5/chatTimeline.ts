/**
 * Compatibility facade for the H5 chat page.
 *
 * The canonical message model and timeline transformations now live in the
 * shared conversation core. Keep this module as the stable H5 import surface
 * while the page continues to own its mobile-specific rendering.
 */
export * from '../../features/conversation/core/chatTimeline';
export {
    buildConversationEntries as buildH5ConversationEntries,
    getConversationScrollAnchor as getH5ScrollAnchor,
} from '../../features/conversation/core/chatTimeline';
export type {
    AssistantStreamMessage as H5AssistantStreamMessage,
    ConversationAnalysisItem as H5AnalysisItem,
    ConversationEntry as H5ConversationEntry,
    ConversationMessage as H5ChatMessage,
    ConversationToolStatus as H5ToolStatus,
} from '../../features/conversation/core/chatTimeline';
