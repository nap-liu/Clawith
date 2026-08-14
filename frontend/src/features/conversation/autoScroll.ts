export const CONVERSATION_AUTO_SCROLL_SETTLE_MS = 600;

export interface ConversationScrollTarget {
    scrollHeight: number;
    scrollTop: number;
}

export interface ConversationAutoScrollEnvironment {
    requestFrame: (callback: FrameRequestCallback) => number;
    cancelFrame: (handle: number) => void;
    setTimer: (callback: () => void, delay: number) => number;
    clearTimer: (handle: number) => void;
    observeResize: (targets: Element[], callback: () => void) => () => void;
}

export interface ScheduleStableConversationBottomScrollOptions {
    alignBottom: () => void;
    resizeTargets?: Element[];
    settleMs?: number;
    environment?: ConversationAutoScrollEnvironment;
}

const browserEnvironment = (): ConversationAutoScrollEnvironment => ({
    requestFrame: (callback) => window.requestAnimationFrame(callback),
    cancelFrame: (handle) => window.cancelAnimationFrame(handle),
    setTimer: (callback, delay) => window.setTimeout(callback, delay),
    clearTimer: (handle) => window.clearTimeout(handle),
    observeResize: (targets, callback) => {
        if (typeof ResizeObserver === 'undefined' || targets.length === 0) return () => undefined;
        const observer = new ResizeObserver(callback);
        targets.forEach((target) => observer.observe(target));
        return () => observer.disconnect();
    },
});

export function alignConversationScrollerToBottom(scroller: ConversationScrollTarget): void {
    scroller.scrollTop = scroller.scrollHeight;
}

export function isConversationScrollerAtBottom(
    scroller: Pick<HTMLElement, 'scrollHeight' | 'scrollTop' | 'clientHeight'>,
    threshold = 3,
): boolean {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= threshold;
}

export function scheduleStableConversationBottomScroll({
    alignBottom,
    resizeTargets = [],
    settleMs = CONVERSATION_AUTO_SCROLL_SETTLE_MS,
    environment = browserEnvironment(),
}: ScheduleStableConversationBottomScrollOptions): () => void {
    let stopped = false;
    let resizeFrameHandle: number | null = null;
    const frameHandles = new Set<number>();
    const timerHandles = new Set<number>();

    const align = () => {
        if (!stopped) alignBottom();
    };
    const requestFrame = (callback: () => void) => {
        const handle = environment.requestFrame(() => {
            frameHandles.delete(handle);
            if (!stopped) callback();
        });
        frameHandles.add(handle);
    };
    const setTimer = (callback: () => void, delay: number) => {
        const handle = environment.setTimer(() => {
            timerHandles.delete(handle);
            if (!stopped) callback();
        }, delay);
        timerHandles.add(handle);
    };

    align();
    requestFrame(() => {
        align();
        requestFrame(align);
    });
    [80, 240, 600].forEach((delay) => setTimer(align, Math.min(delay, settleMs)));
    // Stay subscribed until the content key changes or the user takes control.
    // This covers late image loads and user-expanded Markdown/tool sections.
    const disconnectResize = environment.observeResize(resizeTargets, () => {
        if (resizeFrameHandle !== null) return;
        const handle = environment.requestFrame(() => {
            frameHandles.delete(handle);
            resizeFrameHandle = null;
            align();
        });
        resizeFrameHandle = handle;
        frameHandles.add(handle);
    });

    return () => {
        if (stopped) return;
        stopped = true;
        resizeFrameHandle = null;
        frameHandles.forEach((handle) => environment.cancelFrame(handle));
        timerHandles.forEach((handle) => environment.clearTimer(handle));
        frameHandles.clear();
        timerHandles.clear();
        disconnectResize();
    };
}

export function isConversationScrollKey(event: Pick<KeyboardEvent, 'key' | 'altKey' | 'ctrlKey' | 'metaKey'>): boolean {
    if (event.altKey || event.ctrlKey || event.metaKey) return false;
    return ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key);
}

export function isConversationScrollbarPointer(
    element: Pick<HTMLElement, 'clientWidth' | 'clientHeight' | 'offsetWidth' | 'offsetHeight' | 'getBoundingClientRect'>,
    point: Pick<PointerEvent, 'clientX' | 'clientY' | 'pointerType'>,
): boolean {
    if (point.pointerType !== 'mouse') return false;
    const rect = element.getBoundingClientRect();
    const verticalGutter = Math.max(0, element.offsetWidth - element.clientWidth);
    const horizontalGutter = Math.max(0, element.offsetHeight - element.clientHeight);
    return (verticalGutter > 0 && point.clientX >= rect.right - verticalGutter)
        || (horizontalGutter > 0 && point.clientY >= rect.bottom - horizontalGutter);
}
