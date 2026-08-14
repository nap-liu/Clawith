export type H5PageResumeReason = 'visible' | 'pageshow' | 'online';

type H5PageLifecycleOptions = {
    targetWindow?: Window;
    targetDocument?: Document;
    onSuspend?: () => void;
    onResume?: (reason: H5PageResumeReason) => void;
};

/**
 * Embedded WebViews can emit visibilitychange and pageshow for the same return.
 * Model them as one suspend/resume cycle so callers rebuild resources only once.
 */
export function installH5PageLifecycle(options: H5PageLifecycleOptions = {}) {
    const targetWindow = options.targetWindow
        ?? (typeof window !== 'undefined' ? window : undefined);
    const targetDocument = options.targetDocument
        ?? (typeof document !== 'undefined' ? document : undefined);
    if (!targetWindow || !targetDocument) return () => {};

    let suspended = targetDocument.visibilityState === 'hidden';

    const suspend = () => {
        if (suspended) return;
        suspended = true;
        options.onSuspend?.();
    };
    const resume = (reason: H5PageResumeReason) => {
        if (targetDocument.visibilityState === 'hidden') return;
        if (!suspended) return;
        suspended = false;
        options.onResume?.(reason);
    };
    const onVisibilityChange = () => {
        if (targetDocument.visibilityState === 'hidden') {
            suspend();
        } else {
            resume('visible');
        }
    };
    const onPageHide = () => suspend();
    const onPageShow = () => resume('pageshow');
    const onOnline = () => resume('online');

    targetDocument.addEventListener('visibilitychange', onVisibilityChange);
    targetWindow.addEventListener('pagehide', onPageHide);
    targetWindow.addEventListener('pageshow', onPageShow);
    targetWindow.addEventListener('online', onOnline);

    return () => {
        targetDocument.removeEventListener('visibilitychange', onVisibilityChange);
        targetWindow.removeEventListener('pagehide', onPageHide);
        targetWindow.removeEventListener('pageshow', onPageShow);
        targetWindow.removeEventListener('online', onOnline);
    };
}
