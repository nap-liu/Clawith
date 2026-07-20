/**
 * Runtime-only link policy helpers for the embedded H5 chat surface.
 *
 * Embedded containers are deliberately detected from platform runtime
 * capabilities:
 * the H5 entry must not depend on channel/provider query parameters to decide
 * how a browser link behaves.
 */
import type { H5ContainerRuntime } from './h5ContainerRuntime';

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

export type H5LinkAction =
    | { type: 'dingtalk-open'; url: string }
    | { type: 'wechat-miniapp-open'; url: string }
    | { type: 'native' };

type ResolveH5LinkActionOptions = {
    currentHref?: string;
    runtime?: H5ContainerRuntime;
};

/**
 * Decide how an H5 chat link should be handled.
 *
 * Only cross-origin HTTP(S) links are intercepted. Same-origin and non-HTTP
 * links retain the renderer/browser behavior.
 */
export function resolveH5LinkAction(
    href: string,
    options: ResolveH5LinkActionOptions = {},
): H5LinkAction {
    const currentHref = options.currentHref
        ?? (typeof window !== 'undefined' ? window.location.href : '');
    const externalUrl = resolveExternalHttpLink(href, currentHref);

    if (!externalUrl) return { type: 'native' };

    if (options.runtime === 'dingtalk-miniapp-webview') {
        return { type: 'dingtalk-open', url: externalUrl };
    }

    if (options.runtime === 'wechat-miniapp-webview') {
        return { type: 'wechat-miniapp-open', url: externalUrl };
    }

    return { type: 'native' };
}
