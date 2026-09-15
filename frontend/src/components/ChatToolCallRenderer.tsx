import type { ReactNode } from 'react';
import ChatFileDeliveryCard from './ChatFileDeliveryCard';
import ChatMediaDeliveryErrorCard from './ChatMediaDeliveryErrorCard';
import ConfirmationCard from './ConfirmationCard';
import SubagentRunCard, { parseSubagentRunCardData, type SubagentRunCardData } from './SubagentRunCard';
import { parseFileDeliveryToolResult, parseMediaDeliveryErrorResult } from '../utils/chatFileDelivery';
import type { ChatFileDelivery, ChatMediaDeliveryError } from '../utils/chatFileDelivery';
import type { ChatPreviewImage } from '../utils/chatAttachments';
import ChatToolResultCard from './ChatToolResultCard';
import type { ToolResultDisplay } from '../utils/chatToolResult';

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
    onOpenSubagentSession?: (data: SubagentRunCardData) => void;
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
        type: 'tool-result',
        resolve: ({ message }) => message.toolResultContent?.hasMedia ? message.toolResultContent : null,
        render: ({ agentId, mode, onPreviewImages }, { message, payload }, data) => (
            <ChatToolResultCard agentId={agentId} mode={mode} result={data as ToolResultDisplay}
                messageId={String(message.persistedMessageId || message.id || '')}
                name={message.toolName || payload.name || ''} args={message.toolArgs ?? payload.args}
                onPreviewImages={onPreviewImages} />
        ),
    },
    {
        type: 'run-subagent',
        resolve: (context) => {
            const data = parseSubagentRunCardData(context.message, context.payload);
            return toolName(context) === 'run_subagent' || data.taskId ? data : null;
        },
        render: ({ agentId, mode, t, onOpenSubagentSession }, _context, data) => (
            <SubagentRunCard
                agentId={agentId}
                mode={mode}
                data={data as SubagentRunCardData}
                t={t}
                onOpenSession={onOpenSubagentSession}
            />
        ),
        identity: (_context, data) => {
            const run = data as SubagentRunCardData;
            return [run.sessionId || '', run.taskId || '', run.status, run.name || '', run.task || '', run.mode || '', run.model || '', String(run.fork), String(run.soul), String(run.memory)].join('\u0000');
        },
    },
    {
        type: 'media-delivery',
        resolve: (context) => {
            const delivery = parseFileDeliveryToolResult(
                toolName(context),
                context.message.toolResult ?? context.payload.result ?? (!context.payload.name ? context.message.content : undefined),
                context.message.toolArgs ?? context.payload.args ?? {},
                context.message.toolCallId,
            );
            if (delivery?.mediaKind) return delivery;
            return parseMediaDeliveryErrorResult(
                toolName(context),
                context.message.toolResult ?? context.payload.result ?? (!context.payload.name ? context.message.content : undefined),
            );
        },
        render: ({ agentId, mode, onPreviewImages }, context, delivery) => {
            if ((delivery as ChatMediaDeliveryError).deliveryError) {
                return <ChatMediaDeliveryErrorCard error={delivery as ChatMediaDeliveryError} />;
            }
            const item = delivery as ChatFileDelivery;
            return (
                <ChatFileDeliveryCard
                    agentId={agentId}
                    messageId={item.messageId || context.message.persistedMessageId || String(context.message.id || context.message.toolCallId || '')}
                    delivery={item}
                    mode={mode}
                    onPreviewImages={onPreviewImages}
                />
            );
        },
        identity: (_context, delivery) => {
            if ((delivery as ChatMediaDeliveryError).deliveryError) {
                const error = delivery as ChatMediaDeliveryError;
                return [error.status, error.code, error.message].join('\u0000');
            }
            const item = delivery as ChatFileDelivery;
            return [item.messageId || '', item.path || item.url || '', item.filename].join('\u0000');
        },
    },
    {
        type: 'file-delivery',
        resolve: (context) => {
            const delivery = parseFileDeliveryToolResult(
                toolName(context),
                context.message.toolResult ?? context.payload.result ?? (!context.payload.name ? context.message.content : undefined),
                context.message.toolArgs ?? context.payload.args ?? {},
                context.message.toolCallId,
            );
            return delivery && !delivery.mediaKind ? delivery : null;
        },
        render: ({ agentId, mode, onPreviewImages }, context, delivery) => (
            <ChatFileDeliveryCard
                agentId={agentId}
                messageId={String(context.message.id || context.message.toolCallId || '')}
                delivery={delivery as NonNullable<ReturnType<typeof parseFileDeliveryToolResult>>}
                mode={mode}
                onPreviewImages={onPreviewImages}
            />
        ),
        identity: (_context, delivery) => {
            const item = delivery as NonNullable<ReturnType<typeof parseFileDeliveryToolResult>>;
            return [item.path || item.url || '', item.filename, item.message || ''].join('\u0000');
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
