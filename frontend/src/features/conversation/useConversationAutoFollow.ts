import {
    useCallback,
    useLayoutEffect,
    useRef,
    useState,
    type KeyboardEvent as ReactKeyboardEvent,
    type PointerEvent as ReactPointerEvent,
    type RefObject,
    type TouchEvent as ReactTouchEvent,
    type UIEvent as ReactUIEvent,
    type WheelEvent as ReactWheelEvent,
} from 'react';

import {
    alignConversationScrollerToBottom,
    isConversationScrollerAtBottom,
    isConversationScrollbarPointer,
    isConversationScrollKey,
    scheduleStableConversationBottomScroll,
} from './autoScroll';

type ConversationScroller = HTMLElement;

export interface ConversationAutoFollowOptions {
    scrollerRef: RefObject<ConversationScroller | null>;
    contentKey: string;
    resetKey: string | number | null | undefined;
    enabled?: boolean;
    alignBottom?: (scroller: ConversationScroller) => void;
}

export function useConversationAutoFollow({
    scrollerRef,
    contentKey,
    resetKey,
    enabled = true,
    alignBottom = alignConversationScrollerToBottom,
}: ConversationAutoFollowOptions) {
    const [manualMode, setManualMode] = useState(false);
    const manualModeRef = useRef(false);
    const cleanupRef = useRef<(() => void) | null>(null);
    const previousResetKeyRef = useRef<string | number | null | undefined | symbol>(Symbol('initial'));
    const touchStartRef = useRef<{ x: number; y: number } | null>(null);
    const lastPointerDownAtRef = useRef(0);

    const cancelPendingScroll = useCallback(() => {
        cleanupRef.current?.();
        cleanupRef.current = null;
    }, []);

    const requestAutoScroll = useCallback((force = false) => {
        if (!enabled || (!force && manualModeRef.current)) return;
        const scroller = scrollerRef.current;
        if (!scroller) return;
        cancelPendingScroll();
        cleanupRef.current = scheduleStableConversationBottomScroll({
            alignBottom: () => alignBottom(scroller),
            resizeTargets: [scroller, ...Array.from(scroller.children)],
        });
    }, [alignBottom, cancelPendingScroll, enabled, scrollerRef]);

    const pauseAutoFollow = useCallback(() => {
        if (manualModeRef.current) return;
        manualModeRef.current = true;
        cancelPendingScroll();
        setManualMode(true);
    }, [cancelPendingScroll]);

    const resumeAutoFollow = useCallback(() => {
        manualModeRef.current = false;
        setManualMode(false);
        requestAutoScroll(true);
    }, [requestAutoScroll]);

    useLayoutEffect(() => {
        if (previousResetKeyRef.current !== resetKey) {
            previousResetKeyRef.current = resetKey;
            manualModeRef.current = false;
            setManualMode(false);
        }
        requestAutoScroll();
        return cancelPendingScroll;
    }, [cancelPendingScroll, contentKey, requestAutoScroll, resetKey]);

    const onWheelCapture = useCallback((event: ReactWheelEvent<ConversationScroller>) => {
        if (event.deltaX === 0 && event.deltaY === 0) return;
        if (event.deltaX === 0 && event.deltaY > 0 && isConversationScrollerAtBottom(event.currentTarget)) {
            if (manualModeRef.current) resumeAutoFollow();
            return;
        }
        pauseAutoFollow();
    }, [pauseAutoFollow, resumeAutoFollow]);

    const onTouchStartCapture = useCallback((event: ReactTouchEvent<ConversationScroller>) => {
        const touch = event.touches[0];
        touchStartRef.current = touch ? { x: touch.clientX, y: touch.clientY } : null;
    }, []);

    const onTouchMoveCapture = useCallback((event: ReactTouchEvent<ConversationScroller>) => {
        const start = touchStartRef.current;
        const touch = event.touches[0];
        if (!start || !touch) return;
        const deltaX = touch.clientX - start.x;
        const deltaY = touch.clientY - start.y;
        if (Math.abs(deltaX) <= 4 && Math.abs(deltaY) <= 4) return;
        if (Math.abs(deltaX) <= Math.abs(deltaY) && deltaY < 0 && isConversationScrollerAtBottom(event.currentTarget)) {
            if (manualModeRef.current) resumeAutoFollow();
            return;
        }
        pauseAutoFollow();
    }, [pauseAutoFollow, resumeAutoFollow]);

    const onPointerDownCapture = useCallback((event: ReactPointerEvent<ConversationScroller>) => {
        lastPointerDownAtRef.current = Date.now();
        if (isConversationScrollbarPointer(event.currentTarget, event.nativeEvent)) pauseAutoFollow();
    }, [pauseAutoFollow]);

    const onScrollCapture = useCallback((event: ReactUIEvent<ConversationScroller>) => {
        if (isConversationScrollerAtBottom(event.currentTarget)) {
            if (manualModeRef.current) resumeAutoFollow();
            return;
        }
        // Overlay scrollbars (notably macOS) do not reserve a measurable gutter.
        // A scroll immediately following a pointer press is therefore the
        // reliable signal that the user is dragging the native scrollbar.
        if (Date.now() - lastPointerDownAtRef.current < 800) pauseAutoFollow();
    }, [pauseAutoFollow, resumeAutoFollow]);

    const onKeyDownCapture = useCallback((event: ReactKeyboardEvent<ConversationScroller>) => {
        const target = event.target as HTMLElement;
        if (target.closest('input, textarea, select, [contenteditable="true"]')) return;
        if (!isConversationScrollKey(event.nativeEvent)) return;
        const movesTowardBottom = ['ArrowDown', 'PageDown', 'End'].includes(event.key)
            || (event.key === ' ' && !event.shiftKey);
        if (movesTowardBottom && isConversationScrollerAtBottom(event.currentTarget)) {
            if (manualModeRef.current) resumeAutoFollow();
            return;
        }
        pauseAutoFollow();
    }, [pauseAutoFollow, resumeAutoFollow]);

    return {
        autoFollowEnabled: !manualMode,
        showScrollToBottom: manualMode,
        pauseAutoFollow,
        resumeAutoFollow,
        requestAutoScroll,
        cancelPendingAutoFollow: cancelPendingScroll,
        interactionProps: {
            onWheelCapture,
            onTouchStartCapture,
            onTouchMoveCapture,
            onPointerDownCapture,
            onScrollCapture,
            onKeyDownCapture,
        },
    };
}
