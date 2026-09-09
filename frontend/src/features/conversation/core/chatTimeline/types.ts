import type {
  ChatMessageAttachment,
  ChatPreviewImage,
  ChatQuotedMessage,
} from "../../../../utils/chatAttachments";

export type ConversationToolStatus = "running" | "done";

export type ConversationMessage = {
  id: string;
  role: "user" | "assistant" | "system" | "tool_call";
  content: string;
  thinking?: string;
  streaming?: boolean;
  created_at?: string | null;
  display_content?: string;
  external_context?: unknown;
  attachments?: ChatMessageAttachment[];
  quoted_message?: ChatQuotedMessage;
  toolCallId?: string;
  toolName?: string;
  toolArgs?: any;
  toolStatus?: ConversationToolStatus;
  toolResult?: string;
  toolThinking?: string;
  mediaTaskId?: string;
  fileName?: string;
  imageUrl?: string;
  previewImages?: ChatPreviewImage[];
  timestamp?: string;
  sender_name?: string;
  sender_user_id?: string;
  sender_agent_id?: string;
  sender_avatar_url?: string;
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

export type ConversationTimelineFoldResult<T extends ConversationMessage> = {
  messages: T[];
  handled: boolean;
};
