import { useEffect, useRef, type ReactNode } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

type VirtualSessionListProps<T extends { id: string }> = {
    items: T[];
    hasMore: boolean;
    initialLoading: boolean;
    loadingMore: boolean;
    estimateSize: number;
    emptyState: ReactNode;
    loadingState: ReactNode;
    loadMoreLabel: ReactNode;
    renderItem: (item: T) => ReactNode;
    onLoadMore: () => Promise<unknown> | void;
};

export default function VirtualSessionList<T extends { id: string }>({
    items,
    hasMore,
    initialLoading,
    loadingMore,
    estimateSize,
    emptyState,
    loadingState,
    loadMoreLabel,
    renderItem,
    onLoadMore,
}: VirtualSessionListProps<T>) {
    const scrollRef = useRef<HTMLDivElement>(null);
    const loadRequestedRef = useRef(false);
    const attemptedItemCountRef = useRef(-1);
    const itemCount = items.length + (hasMore ? 1 : 0);
    const virtualizer = useVirtualizer({
        count: itemCount,
        getScrollElement: () => scrollRef.current,
        estimateSize: () => estimateSize,
        overscan: 6,
        getItemKey: index => index < items.length ? items[index].id : 'load-more',
    });
    const virtualItems = virtualizer.getVirtualItems();

    const requestMore = (force = false) => {
        if (!hasMore || loadingMore || loadRequestedRef.current) return;
        if (!force && attemptedItemCountRef.current === items.length) return;
        attemptedItemCountRef.current = items.length;
        loadRequestedRef.current = true;
        void Promise.resolve(onLoadMore()).finally(() => {
            loadRequestedRef.current = false;
        });
    };

    useEffect(() => {
        const lastItem = virtualItems[virtualItems.length - 1];
        if (
            !lastItem
            || !hasMore
            || loadingMore
            || loadRequestedRef.current
            || lastItem.index < Math.max(0, items.length - 5)
        ) return;
        requestMore();
    }, [hasMore, items.length, loadingMore, onLoadMore, virtualItems]);

    if (initialLoading && items.length === 0) return <>{loadingState}</>;
    if (items.length === 0 && !hasMore) return <>{emptyState}</>;

    return (
        <div
            ref={scrollRef}
            role="list"
            style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '4px 0' }}
        >
            <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative', width: '100%' }}>
                {virtualItems.map(virtualItem => {
                    const isLoader = virtualItem.index >= items.length;
                    return (
                        <div
                            key={virtualItem.key}
                            ref={virtualizer.measureElement}
                            data-index={virtualItem.index}
                            role={isLoader ? 'status' : 'listitem'}
                            style={{
                                position: 'absolute',
                                top: 0,
                                left: 0,
                                width: '100%',
                                transform: `translateY(${virtualItem.start}px)`,
                            }}
                        >
                            {isLoader ? (
                                <button
                                    type="button"
                                    onClick={() => requestMore(true)}
                                    disabled={loadingMore}
                                    style={{
                                        width: '100%',
                                        padding: '12px',
                                        border: 0,
                                        background: 'transparent',
                                        color: 'var(--text-tertiary)',
                                        fontSize: '11px',
                                        cursor: loadingMore ? 'default' : 'pointer',
                                    }}
                                >
                                    {loadMoreLabel}
                                </button>
                            ) : renderItem(items[virtualItem.index])}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
