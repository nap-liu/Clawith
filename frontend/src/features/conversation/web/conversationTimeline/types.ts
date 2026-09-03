import type { RefObject } from "react";

import type { SubagentRunCardData } from "../../../../components/SubagentRunCard";
import type { ChatPreviewImage } from "../../../../utils/chatAttachments";
import type { ConversationMessage } from "../../core/chatTimeline";

export type ConversationMessageView = {
  isLeft: boolean;
  senderLabel?: string;
  avatarText?: string;
  avatarUrl?: string | null;
  forceSenderLabel?: boolean;
  hideAvatar?: boolean;
};

export type ConversationTimelineProps = {
  agentId: string;
  agentName: string;
  messages: ConversationMessage[];
  mode?: "h5" | "pc";
  viewOf: (message: ConversationMessage) => ConversationMessageView;
  isRunning?: boolean;
  runningLabel?: string;
  progressMessage?: Partial<ConversationMessage>;
  unavailableAttachmentKeys?: ReadonlySet<string>;
  onAttachmentDownload?: (
    path: string,
    displayName: string,
  ) => void | Promise<void>;
  onAttachmentUnavailable?: (key: string) => void;
  onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
  onToolResolved?: (message: ConversationMessage, result: string) => void;
  onOpenSubagentSession?: (data: SubagentRunCardData) => void;
  scrollerRef?: RefObject<HTMLElement | null>;
  focusMessageId?: string;
  resumeMeasurementKey?: string | number | null;
  provenance?: {
    source?: string;
    status?: string;
    scheduled_at?: string;
    finished_at?: string;
    last_error?: string | null;
  } | null;
};
