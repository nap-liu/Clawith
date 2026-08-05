import type { ReactNode } from 'react';
import ChatFileDeliveryCard from './ChatFileDeliveryCard';
import ConfirmationCard from './ConfirmationCard';
import { parseFileDeliveryToolResult } from '../utils/chatFileDelivery';
import type { ChatPreviewImage } from '../utils/chatAttachments';

type ToolCallRenderContext = {
    message: any;
    payload: Record<string, any>;
};

type ToolCallRendererProps = {
    agentId: string;
    message: any;
    t: (key: string, options?: any) => string;
    onResolved: (result: string) => void;
    mode: 'h5' | 'pc';
    onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
};

type ToolCallRendererRegistration = {
    type: string;
    resolve: (context: ToolCallRenderContext) => unknown | null;
    render: (props: ToolCallRendererProps, context: ToolCallRenderContext, data: unknown) => ReactNode;
    identity?: (context: ToolCallRenderContext, data: unknown) => string;
};

function parseStoredPayload(message: any): Record<string, any> {
    if (typeof message?.content !== 'string' || !message.content) return {};
    try {
        const payload = JSON.parse(message.content);
        return payload && typeof payload === 'object' ? payload : {};
    } catch {
        return {};
    }
}

function toolName(context: ToolCallRenderContext): string {
    return String(context.message?.toolName || context.payload.name || '').trim().toLowerCase();
}

const TOOL_CALL_RENDERERS: ToolCallRendererRegistration[] = [
    {
        type: 'file-delivery',
        resolve: (context) => parseFileDeliveryToolResult(
            toolName(context),
            context.message.toolResult ?? context.payload.result ?? (!context.payload.name ? context.message.content : undefined),
            context.message.toolArgs ?? context.payload.args ?? {},
            context.message.toolCallId,
        ),
        render: ({ agentId, mode, onPreviewImages }, _context, delivery) => (
            <ChatFileDeliveryCard
                agentId={agentId}
                delivery={delivery as NonNullable<ReturnType<typeof parseFileDeliveryToolResult>>}
                mode={mode}
                onPreviewImages={onPreviewImages}
            />
        ),
        identity: (_context, delivery) => {
            const item = delivery as NonNullable<ReturnType<typeof parseFileDeliveryToolResult>>;
            return [item.path, item.filename, item.message || ''].join('\u0000');
        },
    },
    {
        type: 'confirmation',
        resolve: (context) => toolName(context) === 'request_confirmation' ? true : null,
        render: ({ agentId, message, t, onResolved }, { payload }) => (
            <ConfirmationCard
                agentId={agentId}
                callId={String(message.toolCallId || payload.call_id || payload.id || message.id || '')}
                args={message.toolArgs || payload.args || {}}
                resolved={(message.toolStatus || payload.status) === 'done'}
                result={message.toolResult ?? payload.result ?? ''}
                t={t}
                onResolved={onResolved}
            />
        ),
    },
];

function resolveRenderer(message: any): {
    renderer: ToolCallRendererRegistration;
    context: ToolCallRenderContext;
    data: unknown;
} | null {
    if (!message || message.role !== 'tool_call') return null;
    const context = { message, payload: parseStoredPayload(message) };
    for (const renderer of TOOL_CALL_RENDERERS) {
        const data = renderer.resolve(context);
        if (data !== null) return { renderer, context, data };
    }
    return null;
}

export function getChatToolRenderType(message: any): string | null {
    return resolveRenderer(message)?.renderer.type || null;
}

export function getChatToolRenderIdentity(message: any): string | null {
    const resolved = resolveRenderer(message);
    if (!resolved?.renderer.identity) return null;
    return `${resolved.renderer.type}:${resolved.renderer.identity(resolved.context, resolved.data)}`;
}

export default function ChatToolCallRenderer(props: ToolCallRendererProps) {
    const resolved = resolveRenderer(props.message);
    return resolved ? resolved.renderer.render(props, resolved.context, resolved.data) : null;
}
