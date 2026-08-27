import { useCallback, useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

export type AnchoredPopoverPlacement = 'top' | 'bottom';

type AnchoredPopoverOptions = {
    open: boolean;
    anchorRef: RefObject<HTMLElement | null>;
    popoverRef: RefObject<HTMLElement | null>;
    width?: number;
    matchAnchorWidth?: boolean;
    minWidth?: number;
    maxWidth?: number;
    maxHeight?: number;
    minimumVisibleHeight?: number;
    gap?: number;
    viewportPadding?: number;
    contentVersion?: string | number;
};

type AnchoredPopoverPosition = {
    style: CSSProperties | undefined;
    placement: AnchoredPopoverPlacement;
};

/** Shared fixed-position portal geometry with viewport collision and automatic top/bottom flipping. */
export function useAnchoredPopoverPosition({
    open,
    anchorRef,
    popoverRef,
    width,
    matchAnchorWidth = false,
    minWidth = 0,
    maxWidth = Number.POSITIVE_INFINITY,
    maxHeight = 320,
    minimumVisibleHeight = 160,
    gap = 6,
    viewportPadding = 8,
    contentVersion,
}: AnchoredPopoverOptions): AnchoredPopoverPosition {
    const [position, setPosition] = useState<AnchoredPopoverPosition>({ style: undefined, placement: 'bottom' });

    const updatePosition = useCallback(() => {
        const anchor = anchorRef.current;
        const popover = popoverRef.current;
        if (!anchor || !popover) return;

        const anchorRect = anchor.getBoundingClientRect();
        const viewport = window.visualViewport;
        const viewportLeft = viewport?.offsetLeft || 0;
        const viewportTop = viewport?.offsetTop || 0;
        const viewportRight = viewportLeft + (viewport?.width || window.innerWidth);
        const viewportBottom = viewportTop + (viewport?.height || window.innerHeight);
        const viewportWidth = Math.max(0, viewportRight - viewportLeft - viewportPadding * 2);
        const requestedWidth = matchAnchorWidth ? anchorRect.width : (width ?? anchorRect.width);
        const resolvedWidth = Math.min(viewportWidth, maxWidth, Math.max(minWidth, requestedWidth));
        const left = Math.min(
            Math.max(viewportLeft + viewportPadding, anchorRect.left),
            Math.max(viewportLeft + viewportPadding, viewportRight - resolvedWidth - viewportPadding),
        );

        const availableTop = Math.max(0, anchorRect.top - viewportTop - viewportPadding - gap);
        const availableBottom = Math.max(0, viewportBottom - anchorRect.bottom - viewportPadding - gap);
        const desiredHeight = Math.min(maxHeight, popover.scrollHeight || maxHeight);
        const shouldFlipTop = availableBottom < Math.min(desiredHeight, minimumVisibleHeight) && availableTop > availableBottom;
        const placement: AnchoredPopoverPlacement = shouldFlipTop ? 'top' : 'bottom';
        const availableHeight = placement === 'top' ? availableTop : availableBottom;
        const resolvedMaxHeight = Math.max(0, Math.min(maxHeight, availableHeight));
        const visibleHeight = Math.min(desiredHeight, resolvedMaxHeight);
        const top = placement === 'top'
            ? Math.max(viewportTop + viewportPadding, anchorRect.top - visibleHeight - gap)
            : Math.min(viewportBottom - viewportPadding - visibleHeight, anchorRect.bottom + gap);

        setPosition((previous) => {
            const nextStyle = { left, top, width: resolvedWidth, maxHeight: resolvedMaxHeight };
            const previousStyle = previous.style;
            if (previous.placement === placement
                && previousStyle?.left === nextStyle.left
                && previousStyle?.top === nextStyle.top
                && previousStyle?.width === nextStyle.width
                && previousStyle?.maxHeight === nextStyle.maxHeight) return previous;
            return { placement, style: nextStyle };
        });
    }, [anchorRef, gap, matchAnchorWidth, maxHeight, maxWidth, minWidth, minimumVisibleHeight, popoverRef, viewportPadding, width]);

    useLayoutEffect(() => {
        if (!open) {
            setPosition({ style: undefined, placement: 'bottom' });
            return;
        }
        updatePosition();
        const frame = window.requestAnimationFrame(updatePosition);
        const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updatePosition);
        if (anchorRef.current) resizeObserver?.observe(anchorRef.current);
        if (popoverRef.current) resizeObserver?.observe(popoverRef.current);
        const viewport = window.visualViewport;
        window.addEventListener('resize', updatePosition);
        window.addEventListener('scroll', updatePosition, true);
        viewport?.addEventListener('resize', updatePosition);
        viewport?.addEventListener('scroll', updatePosition);
        return () => {
            window.cancelAnimationFrame(frame);
            resizeObserver?.disconnect();
            window.removeEventListener('resize', updatePosition);
            window.removeEventListener('scroll', updatePosition, true);
            viewport?.removeEventListener('resize', updatePosition);
            viewport?.removeEventListener('scroll', updatePosition);
        };
    }, [contentVersion, open, updatePosition]);

    return position;
}
