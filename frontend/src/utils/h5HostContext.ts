import { createClientId } from './clientId';
import { readHostContextBootstrap } from './h5HostContextBootstrap';
import type { ChatAttachedFile } from './chatAttachments';
import type { ConversationMessage } from '../features/conversation/core/chatTimeline';

const CONTEXT_RESPONSE_TIMEOUT_MS = 10_000;
const REQUEST_TYPE = 'digital_employee.context.request';
const RESPONSE_TYPE = 'digital_employee.context.response';

export type HostContextDraft = {
    key: string;
    composerKey: string;
    sessionId: string;
    messageId: string;
    rawContent: string;
    files: ChatAttachedFile[];
    consumeComposer: boolean;
    frame: Record<string, unknown>;
    preview: ConversationMessage;
    contextReady?: boolean;
};

type OutboxEntry = { draft: HostContextDraft; socket: WebSocket; retryAllowed: boolean };

function jsonSnapshot(value: unknown): unknown {
    const ancestors = new Set<object>();
    const validate = (item: unknown): void => {
        if (item === null || typeof item === 'string' || typeof item === 'boolean') return;
        if (typeof item === 'number' && Number.isFinite(item)) return;
        if (!item || typeof item !== 'object' || ancestors.has(item)) throw new Error('hostContext.unavailable');
        if (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype) throw new Error('hostContext.unavailable');
        ancestors.add(item);
        Object.values(item).forEach(validate);
        ancestors.delete(item);
    };
    validate(value);
    return JSON.parse(JSON.stringify(value));
}

function requestParentContext(reference: string, userId: string | undefined, requestId: string, signal: AbortSignal) {
    const bootstrap = readHostContextBootstrap(reference, userId);
    if (!bootstrap || window.parent === window) return Promise.reject(new Error('hostContext.unavailable'));
    const attemptId = createClientId();
    return new Promise<unknown>((resolve, reject) => {
        const finish = (error?: Error, context?: unknown) => {
            window.clearTimeout(timeout);
            window.removeEventListener('message', onMessage);
            signal.removeEventListener('abort', onAbort);
            if (error) reject(error);
            else resolve(context);
        };
        const onAbort = () => finish(new DOMException('Aborted', 'AbortError'));
        const onMessage = (event: MessageEvent) => {
            const data = event.data;
            if (event.source !== window.parent || event.origin !== bootstrap.embed_origin
                || !data || data.type !== RESPONSE_TYPE || data.version !== 1
                || data.request_id !== requestId || data.attempt_id !== attemptId) return;
            if (data.status !== 'ready' || !Object.prototype.hasOwnProperty.call(data, 'context')) {
                finish(new Error('hostContext.unavailable'));
                return;
            }
            try { finish(undefined, jsonSnapshot(data.context)); }
            catch { finish(new Error('hostContext.unavailable')); }
        };
        const timeout = window.setTimeout(() => finish(new Error('hostContext.timeout')), CONTEXT_RESPONSE_TIMEOUT_MS);
        window.addEventListener('message', onMessage);
        signal.addEventListener('abort', onAbort, { once: true });
        if (signal.aborted) onAbort();
        else window.parent.postMessage({
            type: REQUEST_TYPE, version: 1, request_id: requestId, attempt_id: attemptId,
        }, bootstrap.embed_origin);
    });
}

/** One frame owns its pending draft and independent unacknowledged sends. */
export class H5HostContext {
    readonly enabled: boolean;
    private pending: HostContextDraft | null = null;
    private controller: AbortController | null = null;
    private outbox = new Map<string, OutboxEntry>();
    private reconciledSocket: WebSocket | null = null;
    onRejected: ((draft: HostContextDraft) => boolean) | null = null;

    constructor(private reference: string, private userId: string | undefined) {
        this.enabled = !!reference;
    }

    cancelPending() {
        this.controller?.abort();
        this.controller = null;
        this.pending = null;
    }

    stopSession(sessionId: string | null) {
        this.cancelPending();
        for (const entry of this.outbox.values()) {
            if (entry.draft.sessionId === sessionId) entry.retryAllowed = false;
        }
    }

    cancelChangedDraft(composerKey: string, sessionId: string | null) {
        if (this.pending && (this.pending.composerKey !== composerKey || this.pending.sessionId !== sessionId)) {
            this.cancelPending();
        }
    }

    async prepare(candidate: HostContextDraft): Promise<HostContextDraft> {
        if (this.pending?.key !== candidate.key) {
            this.cancelPending();
            this.pending = candidate;
        }
        const draft = this.pending!;
        if (draft.contextReady) return draft;
        const controller = new AbortController();
        this.controller = controller;
        const context = await requestParentContext(this.reference, this.userId, draft.messageId, controller.signal);
        if (controller.signal.aborted || this.pending !== draft) throw new DOMException('Aborted', 'AbortError');
        draft.frame.external_context = context;
        draft.preview.external_context = context;
        draft.contextReady = true;
        this.controller = null;
        return draft;
    }

    send(draft: HostContextDraft, socket: WebSocket) {
        this.outbox.set(draft.messageId, { draft, socket, retryAllowed: true });
        try { socket.send(JSON.stringify(draft.frame)); }
        catch (error) {
            this.outbox.delete(draft.messageId);
            throw error;
        }
        if (this.pending === draft) this.pending = null;
    }

    acknowledge(event: Record<string, unknown>) {
        const rejected = event.event_kind === 'turn_rejected' || event.type === 'error'
            || event.type === 'quota_exceeded' || event.type === 'confirmation_required';
        const clientId = String(event.client_message_id || event.rejected_message_id
            || (rejected ? event.message_id : '') || '');
        if (!clientId) return;
        if (event.type === 'user_message_committed' || event.type === 'history'
            || (event.type === 'turn_receipt' && event.status === 'accepted')) {
            this.outbox.delete(clientId);
        } else if (rejected) {
            const entry = this.outbox.get(clientId);
            if (!entry) return;
            this.outbox.delete(clientId);
            const restored = this.onRejected?.(entry.draft);
            // A definitive rejection may be retried with the frozen snapshot.
            if (restored && !this.pending && event.code !== 'message_conflict') this.pending = entry.draft;
        }
    }

    reconcile(socket: WebSocket) {
        this.reconciledSocket = socket;
    }

    retry(socket: WebSocket | null, sessionId: string | null, allowed: boolean) {
        if (!allowed || !socket || socket !== this.reconciledSocket || socket.readyState !== WebSocket.OPEN) return;
        for (const entry of this.outbox.values()) {
            if (!entry.retryAllowed || entry.socket === socket || entry.draft.sessionId !== sessionId) continue;
            try {
                socket.send(JSON.stringify(entry.draft.frame));
                entry.socket = socket;
            } catch { return; }
        }
    }

    dispose() {
        this.cancelPending();
        this.outbox.clear();
        this.onRejected = null;
    }
}
