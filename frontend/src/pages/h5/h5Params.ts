export type H5Theme = 'light' | 'dark';

export function parseH5Theme(value: string | null | undefined): H5Theme {
    return value === 'dark' ? 'dark' : 'light';
}

export function parseH5SessionId(value: string | null | undefined): string | null {
    const trimmed = (value || '').trim();
    return trimmed || null;
}

export function writeH5SessionIdToHref(href: string, sessionId: string | null | undefined): string {
    const url = new URL(href);
    const parsed = parseH5SessionId(sessionId);
    if (parsed) {
        url.searchParams.set('session_id', parsed);
    } else {
        url.searchParams.delete('session_id');
    }
    const search = url.searchParams.toString();
    return `${url.pathname}${search ? `?${search}` : ''}${url.hash}`;
}
