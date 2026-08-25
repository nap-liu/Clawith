export const MINI_PROGRAM_URI_SCHEME = 'miniprogram';

export type MiniProgramUriAction = 'navigate-to';

export type MiniProgramUri = {
    action: MiniProgramUriAction;
    /** Raw mini-program route, including the leading slash and optional query. */
    route: string;
};

const PREFIX = `${MINI_PROGRAM_URI_SCHEME}://`;
const ACTIONS = new Set<MiniProgramUriAction>(['navigate-to']);
const PATH_SEGMENT = /^(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+$/;
const QUERY = /^(?:[A-Za-z0-9._~!$&'()*+,;=:@/?-]|%[0-9A-Fa-f]{2})*$/;

/**
 * Parse the platform mini-program environment URI profile.
 *
 * The profile follows RFC 3986's hierarchical URI shape:
 * `miniprogram://<action>/<path>[?query]`. It deliberately validates the raw
 * components before any WHATWG URL normalization so encoded dot segments or
 * separators cannot change the page route passed to a host mini-program.
 */
export function parseMiniProgramUri(value: string): MiniProgramUri | null {
    const raw = value.trim();
    if (raw.slice(0, PREFIX.length).toLowerCase() !== PREFIX) return null;
    if (raw.includes('#')) return null;

    const hierarchyAndQuery = raw.slice(PREFIX.length);
    const queryIndex = hierarchyAndQuery.indexOf('?');
    const hierarchy = queryIndex >= 0
        ? hierarchyAndQuery.slice(0, queryIndex)
        : hierarchyAndQuery;
    const rawQuery = queryIndex >= 0
        ? hierarchyAndQuery.slice(queryIndex + 1)
        : null;

    const pathIndex = hierarchy.indexOf('/');
    if (pathIndex <= 0) return null;

    const rawAction = hierarchy.slice(0, pathIndex).toLowerCase();
    if (!ACTIONS.has(rawAction as MiniProgramUriAction)) return null;

    const rawPath = hierarchy.slice(pathIndex);
    if (rawPath === '/' || !rawPath.startsWith('/')) return null;
    if (rawQuery !== null && !QUERY.test(rawQuery)) return null;

    const segments = rawPath.slice(1).split('/');
    if (segments.some((segment) => !PATH_SEGMENT.test(segment))) return null;

    for (const segment of segments) {
        let decoded: string;
        try {
            decoded = decodeURIComponent(segment);
        } catch {
            return null;
        }
        if (
            decoded === '.'
            || decoded === '..'
            || decoded.includes('/')
            || decoded.includes('\\')
            || decoded.includes('\0')
        ) {
            return null;
        }
    }

    return {
        action: rawAction as MiniProgramUriAction,
        route: `${rawPath}${rawQuery === null ? '' : `?${rawQuery}`}`,
    };
}

export function isMiniProgramUri(value: string): boolean {
    return value.trim().slice(0, `${MINI_PROGRAM_URI_SCHEME}:`.length).toLowerCase()
        === `${MINI_PROGRAM_URI_SCHEME}:`;
}
