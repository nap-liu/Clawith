/**
 * Runtime-only link policy helpers for the embedded H5 chat surface.
 *
 * Embedded containers are deliberately detected from platform runtime
 * capabilities:
 * the H5 entry must not depend on channel/provider query parameters to decide
 * how a browser link behaves.
 */
import type { H5ContainerRuntime } from './h5ContainerRuntime';
import { isMiniProgramUri, parseMiniProgramUri } from './miniProgramUri';

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
    | { type: 'dingtalk-miniapp-navigate'; route: string }
    | { type: 'wechat-miniapp-navigate'; route: string }
    | { type: 'miniprogram-unavailable' }
    | { type: 'invalid-miniprogram-uri' }
    | { type: 'dingtalk-open'; url: string }
    | { type: 'wechat-miniapp-open'; url: string }
    | {
        type: 'blocked';
        reason: 'dingtalk-cross-origin' | 'wechat-cross-origin';
        url: string;
    }
    | { type: 'native' };

type ResolveH5LinkActionOptions = {
    currentHref?: string;
    runtime?: H5ContainerRuntime;
};

/**
 * Decide how an H5 chat link should be handled.
 *
 * Standard browsers retain native behavior. Embedded mini-program WebViews
 * receive explicit navigation actions while both platforms block cross-origin
 * URLs.
 */
export function resolveH5LinkAction(
    href: string,
    options: ResolveH5LinkActionOptions = {},
): H5LinkAction {
    const miniProgramUri = parseMiniProgramUri(href);
    if (miniProgramUri) {
        if (options.runtime === 'dingtalk-miniapp-webview') {
            return { type: 'dingtalk-miniapp-navigate', route: miniProgramUri.route };
        }
        if (options.runtime === 'wechat-miniapp-webview') {
            return { type: 'wechat-miniapp-navigate', route: miniProgramUri.route };
        }
        return { type: 'miniprogram-unavailable' };
    }
    if (isMiniProgramUri(href)) return { type: 'invalid-miniprogram-uri' };

    const currentHref = options.currentHref
        ?? (typeof window !== 'undefined' ? window.location.href : '');
    let current: URL;
    let target: URL;
    try {
        current = new URL(currentHref);
        target = new URL(href, current);
    } catch {
        return { type: 'native' };
    }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') {
        return { type: 'native' };
    }

    if (options.runtime === 'dingtalk-miniapp-webview') {
        if (target.origin !== current.origin) {
            return {
                type: 'blocked',
                reason: 'dingtalk-cross-origin',
                url: target.href,
            };
        }
        return { type: 'dingtalk-open', url: target.href };
    }

    if (options.runtime === 'wechat-miniapp-webview') {
        if (target.origin !== current.origin) {
            return {
                type: 'blocked',
                reason: 'wechat-cross-origin',
                url: target.href,
            };
        }
        return { type: 'wechat-miniapp-open', url: target.href };
    }

    return { type: 'native' };
}
