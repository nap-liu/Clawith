export type H5PageResumeReason = 'visible' | 'pageshow' | 'online';

type H5PageLifecycleOptions = {
    targetWindow?: Window;
    targetDocument?: Document;
    onSuspend?: () => void;
    onResume?: (reason: H5PageResumeReason) => void;
};

const VIEWPORT_HEIGHT_PROPERTY = '--h5-viewport-height';

function readViewportHeight(targetWindow: Window): number {
    const visualHeight = targetWindow.visualViewport?.height;
    const height = visualHeight && visualHeight > 0
        ? visualHeight
        : targetWindow.innerHeight;
    return Number.isFinite(height) && height > 0 ? Math.round(height) : 0;
}

/**
 * Embedded mini-program WebViews can freeze timers, sockets and dynamic viewport
 * units while a native page is covering the H5 page. Keep the lifecycle bridge
 * small and platform-neutral: release resources on suspend and let the caller
 * rebuild them once the page is visible again.
 */
export function installH5PageLifecycle(options: H5PageLifecycleOptions = {}) {
    const targetWindow = options.targetWindow
        ?? (typeof window !== 'undefined' ? window : undefined);
    const targetDocument = options.targetDocument
        ?? (typeof document !== 'undefined' ? document : undefined);
    if (!targetWindow || !targetDocument) return () => {};

    let suspended = targetDocument.visibilityState === 'hidden';

    const updateViewportHeight = () => {
        const height = readViewportHeight(targetWindow);
        if (height > 0) {
            targetDocument.documentElement.style.setProperty(
                VIEWPORT_HEIGHT_PROPERTY,
                `${height}px`,
            );
        }
    };
    const suspend = () => {
        if (suspended) return;
        suspended = true;
        options.onSuspend?.();
    };
    const resume = (reason: H5PageResumeReason, force = false) => {
        if (targetDocument.visibilityState === 'hidden') return;
        const shouldNotify = suspended || force;
        suspended = false;
        updateViewportHeight();
        if (shouldNotify) options.onResume?.(reason);
    };
    const onVisibilityChange = () => {
        if (targetDocument.visibilityState === 'hidden') {
            suspend();
        } else {
            resume('visible');
        }
    };
    const onPageHide = () => suspend();
    const onPageShow = (event: PageTransitionEvent) => {
        resume('pageshow', event.persisted === true);
    };
    const onOnline = () => resume('online', true);

    updateViewportHeight();
    targetDocument.addEventListener('visibilitychange', onVisibilityChange);
    targetWindow.addEventListener('pagehide', onPageHide);
    targetWindow.addEventListener('pageshow', onPageShow);
    targetWindow.addEventListener('online', onOnline);
    targetWindow.addEventListener('resize', updateViewportHeight);
    targetWindow.addEventListener('orientationchange', updateViewportHeight);
    targetWindow.visualViewport?.addEventListener('resize', updateViewportHeight);

    return () => {
        targetDocument.removeEventListener('visibilitychange', onVisibilityChange);
        targetWindow.removeEventListener('pagehide', onPageHide);
        targetWindow.removeEventListener('pageshow', onPageShow);
        targetWindow.removeEventListener('online', onOnline);
        targetWindow.removeEventListener('resize', updateViewportHeight);
        targetWindow.removeEventListener('orientationchange', updateViewportHeight);
        targetWindow.visualViewport?.removeEventListener('resize', updateViewportHeight);
        targetDocument.documentElement.style.removeProperty(VIEWPORT_HEIGHT_PROPERTY);
    };
}
