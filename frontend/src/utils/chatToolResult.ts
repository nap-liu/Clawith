import type { ChatMessageAttachment } from './chatAttachments';

/** Backend-normalized Web content; the tool/provider contract remains MCP. */
export type ToolResultDisplay = {
    content: (
        | { type: 'text'; text: string }
        | { type: 'attachment'; attachment: ChatMessageAttachment }
        | { type: 'unavailable' }
    )[];
    isError: boolean;
    hasMedia: boolean;
};
