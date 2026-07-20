type BrowserPopup = {
    opener?: unknown;
};

export type BrowserLinkHost = {
    open?: (url?: string | URL, target?: string) => BrowserPopup | null;
    location?: {
        assign: (url: string | URL) => void;
    };
};

function defaultBrowserHost(): BrowserLinkHost | undefined {
    return typeof window !== 'undefined'
        ? (window as unknown as BrowserLinkHost)
        : undefined;
}

/**
 * Restore browser navigation after a container SDK has rejected a link open.
 *
 * Prefer a new window to preserve the chat. If the WebView blocks popups,
 * navigate the current window so the link never becomes unusable.
 */
export function openExternalLinkWithBrowserDefault(
    url: string,
    targetWindow: BrowserLinkHost | undefined = defaultBrowserHost(),
): boolean {
    let parsedUrl: URL;
    try {
        parsedUrl = new URL(url);
    } catch {
        return false;
    }
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
        return false;
    }
    if (!targetWindow) return false;

    try {
        const popup = targetWindow.open?.(parsedUrl.href, '_blank');
        if (popup) {
            try {
                popup.opener = null;
            } catch {
                // The link has already opened; some WebViews make opener readonly.
            }
            return true;
        }
    } catch {
        // Continue to same-window navigation.
    }

    try {
        targetWindow.location?.assign(parsedUrl.href);
        return typeof targetWindow.location?.assign === 'function';
    } catch {
        return false;
    }
}
