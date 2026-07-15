/**
 * Runtime-only link policy helpers for the embedded H5 chat surface.
 *
 * The mini-program container is deliberately detected from the user agent:
 * the H5 entry must not depend on channel/provider query parameters to decide
 * how a browser link behaves.
 */

export function isWechatMiniProgramWebView(
    userAgent = typeof navigator !== 'undefined' ? navigator.userAgent : '',
): boolean {
    const normalized = userAgent.toLowerCase();
    return normalized.includes('micromessenger') && normalized.includes('miniprogram');
}

/**
 * Return an absolute cross-origin HTTP(S) URL, or null when the link should
 * retain the browser's native behavior (same-origin, non-HTTP, or malformed).
 */
export function resolveExternalHttpLink(
    href: string,
    currentHref = typeof window !== 'undefined' ? window.location.href : '',
): string | null {
    if (!href || !currentHref) return null;

    try {
        const current = new URL(currentHref);
        const target = new URL(href, current);
        if (target.protocol !== 'http:' && target.protocol !== 'https:') return null;
        if (target.origin === current.origin) return null;
        return target.href;
    } catch {
        return null;
    }
}
