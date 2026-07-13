/** Shared URL helpers for the H5 and desktop web chat surfaces. */

export function parseChatSessionId(value: string | null | undefined): string | null {
    const trimmed = (value || '').trim();
    return trimmed || null;
}

export function writeChatSessionIdToHref(href: string, sessionId: string | null | undefined): string {
    const url = new URL(href);
    const parsed = parseChatSessionId(sessionId);
    if (parsed) {
        url.searchParams.set('session_id', parsed);
    } else {
        url.searchParams.delete('session_id');
    }
    const search = url.searchParams.toString();
    return `${url.pathname}${search ? `?${search}` : ''}${url.hash}`;
}
