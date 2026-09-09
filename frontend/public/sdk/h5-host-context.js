const REQUEST_TYPE = 'digital_employee.context.request';
const RESPONSE_TYPE = 'digital_employee.context.response';

/** Connect one iframe to the host's existing authorized, request-stable snapshot owner. */
export function createHostContextBridge({ iframe, frameOrigin, getContext }) {
    const parsed = new URL(frameOrigin);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.origin !== frameOrigin
        || !iframe || typeof getContext !== 'function') throw new TypeError('Invalid host context bridge');
    const pending = new Map();
    let disposed = false;

    function onMessage(event) {
        const data = event.data;
        if (disposed || event.source !== iframe.contentWindow || event.origin !== frameOrigin
            || !data || data.type !== REQUEST_TYPE || data.version !== 1
            || typeof data.request_id !== 'string' || !data.request_id
            || typeof data.attempt_id !== 'string' || !data.attempt_id) return;
        const target = event.source;
        const requestId = data.request_id;
        const attemptId = data.attempt_id;
        let entry = pending.get(requestId);
        if (!entry) {
            const controller = new AbortController();
            const promise = Promise.resolve().then(() => getContext({ requestId, signal: controller.signal }));
            entry = { controller, promise };
            pending.set(requestId, entry);
            void promise.finally(() => {
                if (pending.get(requestId) === entry) pending.delete(requestId);
            }).catch(() => undefined);
        }
        const reply = (payload) => {
            if (disposed || target !== iframe.contentWindow) return;
            target.postMessage({
                type: RESPONSE_TYPE, version: 1, request_id: requestId, attempt_id: attemptId, ...payload,
            }, frameOrigin);
        };
        void entry.promise.then(
            (context) => reply({ status: 'ready', context }),
            () => reply({ status: 'unavailable', error: { code: 'context_unavailable' } }),
        ).catch(() => reply({ status: 'unavailable', error: { code: 'context_unavailable' } }));
    }

    window.addEventListener('message', onMessage);
    return {
        dispose() {
            disposed = true;
            window.removeEventListener('message', onMessage);
            for (const entry of pending.values()) entry.controller.abort();
            pending.clear();
        },
    };
}
