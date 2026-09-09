import { createClientId } from './clientId';

const STORAGE_PREFIX = 'digital-employee:host-context:';

export type HostContextBootstrap = {
    version: 1;
    embed_origin: string;
    frame_origin: string;
    instance_ref: string;
};

function isOrigin(value: unknown): value is string {
    if (typeof value !== 'string') return false;
    try {
        const url = new URL(value);
        return ['https:', 'http:'].includes(url.protocol) && url.origin === value;
    } catch {
        return false;
    }
}

function isBootstrap(value: unknown): value is HostContextBootstrap {
    if (!value || typeof value !== 'object') return false;
    const config = value as HostContextBootstrap;
    return config.version === 1 && isOrigin(config.embed_origin)
        && isOrigin(config.frame_origin) && typeof config.instance_ref === 'string'
        && !!config.instance_ref.trim();
}

/** Store each launch independently; an ordinary H5 URL never reads this state. */
export function bindHostContextToDestination(
    destination: string,
    bootstrap: unknown,
    userId: string,
): string {
    if (bootstrap === undefined) return destination;
    if (!isBootstrap(bootstrap)) throw new Error('invalid_host_context');
    const url = new URL(destination, window.location.origin);
    if (url.origin !== bootstrap.frame_origin || url.origin !== window.location.origin) {
        throw new Error('invalid_host_context');
    }
    const reference = createClientId();
    sessionStorage.setItem(STORAGE_PREFIX + reference, JSON.stringify({ ...bootstrap, user_id: userId }));
    url.searchParams.set('host_context', reference);
    return `${url.pathname}${url.search}${url.hash}`;
}

export function readHostContextBootstrap(reference: string, userId: string | undefined): HostContextBootstrap | null {
    if (!reference || !userId) return null;
    try {
        const value = JSON.parse(sessionStorage.getItem(STORAGE_PREFIX + reference) || 'null');
        return isBootstrap(value) && value.frame_origin === window.location.origin
            && (value as HostContextBootstrap & { user_id?: string }).user_id === userId ? value : null;
    } catch {
        return null;
    }
}
