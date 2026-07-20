type DingTalkNavigateToOptions = {
    url: string;
    success?: () => void;
    fail?: (error?: unknown) => void;
};

type DingTalkNavigateTo = (options: DingTalkNavigateToOptions) => unknown;

export type DingTalkWebViewSdk = {
    navigateTo?: DingTalkNavigateTo;
    postMessage?: (message: Record<string, unknown>) => unknown;
};

export type DingTalkHostWindow = {
    dd?: DingTalkWebViewSdk;
};

type DingTalkSdkDocument = Pick<
    Document,
    'createElement' | 'head' | 'querySelector'
>;

type DetectDingTalkMiniProgramOptions = {
    targetWindow?: DingTalkHostWindow;
    targetDocument?: DingTalkSdkDocument;
    userAgent?: string;
    sdkUrl?: string;
    loadTimeoutMs?: number;
};

type OpenDingTalkMiniProgramLinkOptions = DetectDingTalkMiniProgramOptions & {
    currentHref?: string;
    route?: string;
    navigateTimeoutMs?: number;
    duplicateWindowMs?: number;
    now?: () => number;
};

export const DEFAULT_DINGTALK_WEBVIEW_SDK_URL = 'https://appx/web-view.min.js';
export const DEFAULT_DINGTALK_WEBVIEW_ROUTE = '/subPackages/webview/index';

const DEFAULT_SDK_LOAD_TIMEOUT_MS = 1500;
const DEFAULT_NAVIGATE_TIMEOUT_MS = 1500;
const DEFAULT_DUPLICATE_WINDOW_MS = 500;

let sdkLoadPromise: Promise<void> | null = null;
let lastOpenUrl = '';
let lastOpenStartedAt = 0;

function defaultTargetWindow(): DingTalkHostWindow | undefined {
    return typeof window !== 'undefined'
        ? (window as unknown as DingTalkHostWindow)
        : undefined;
}

function defaultTargetDocument(): DingTalkSdkDocument | undefined {
    return typeof document !== 'undefined' ? document : undefined;
}

function defaultUserAgent(): string {
    return typeof navigator !== 'undefined' ? navigator.userAgent : '';
}

function hasWebViewNavigationSdk(targetWindow?: DingTalkHostWindow): boolean {
    return typeof targetWindow?.dd?.navigateTo === 'function';
}

/**
 * DingTalk marks H5 pages hosted by a mini-program web-view with `dd-web`.
 * A plain DingTalk client WebView only carries the broader DingTalk marker and
 * must retain normal browser behavior.
 */
export function isDingTalkMiniProgramWebViewCandidate(userAgent: string): boolean {
    return /dd-web/i.test(userAgent);
}

function normalizeError(error: unknown, fallbackMessage: string): Error {
    if (error instanceof Error) return error;
    if (
        typeof error === 'object'
        && error !== null
        && 'message' in error
        && typeof error.message === 'string'
    ) {
        return new Error(error.message);
    }
    return new Error(fallbackMessage);
}

function loadDingTalkWebViewSdk(
    targetWindow: DingTalkHostWindow | undefined,
    targetDocument: DingTalkSdkDocument | undefined,
    sdkUrl: string,
    timeoutMs: number,
): Promise<void> {
    if (hasWebViewNavigationSdk(targetWindow)) return Promise.resolve();
    if (!targetDocument) {
        return Promise.reject(new Error('Cannot load DingTalk WebView SDK without a document'));
    }
    if (sdkLoadPromise) return sdkLoadPromise;

    const loadPromise = new Promise<void>((resolve, reject) => {
        const existing = targetDocument.querySelector<HTMLScriptElement>(
            'script[data-clawith-dingtalk-webview-sdk]',
        );
        const script = existing ?? targetDocument.createElement('script');
        let settled = false;

        const cleanupListeners = () => {
            script.removeEventListener('load', onLoad);
            script.removeEventListener('error', onError);
        };
        const removeScript = () => {
            if (script.parentNode) script.parentNode.removeChild(script);
        };
        const finish = (callback: () => void) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            cleanupListeners();
            callback();
        };
        const onLoad = () => finish(() => {
            if (hasWebViewNavigationSdk(targetWindow)) {
                resolve();
            } else {
                removeScript();
                reject(new Error('DingTalk WebView SDK loaded without navigateTo'));
            }
        });
        const onError = () => finish(() => {
            removeScript();
            reject(new Error('Failed to load DingTalk WebView SDK'));
        });
        const timer = setTimeout(
            () => finish(() => {
                removeScript();
                reject(new Error('Timed out loading DingTalk WebView SDK'));
            }),
            Math.max(0, timeoutMs),
        );

        script.addEventListener('load', onLoad, { once: true });
        script.addEventListener('error', onError, { once: true });

        if (!existing) {
            script.async = true;
            script.src = sdkUrl;
            script.dataset.clawithDingtalkWebviewSdk = '1';
            targetDocument.head.appendChild(script);
        }
    }).catch((error) => {
        sdkLoadPromise = null;
        throw error;
    });
    sdkLoadPromise = loadPromise;

    return loadPromise;
}

export async function isDingTalkMiniProgramWebViewRuntime(
    options: DetectDingTalkMiniProgramOptions = {},
): Promise<boolean> {
    const userAgent = options.userAgent ?? defaultUserAgent();
    if (!isDingTalkMiniProgramWebViewCandidate(userAgent)) return false;

    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    try {
        await loadDingTalkWebViewSdk(
            targetWindow,
            options.targetDocument ?? defaultTargetDocument(),
            options.sdkUrl ?? DEFAULT_DINGTALK_WEBVIEW_SDK_URL,
            options.loadTimeoutMs ?? DEFAULT_SDK_LOAD_TIMEOUT_MS,
        );
    } catch {
        return false;
    }

    return hasWebViewNavigationSdk(targetWindow);
}

export function buildDingTalkMiniProgramWebviewRoute(
    url: string,
    route = DEFAULT_DINGTALK_WEBVIEW_ROUTE,
): string {
    return `${route}?url=${encodeURIComponent(url)}`;
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
    return (
        (typeof value === 'object' && value !== null)
        || typeof value === 'function'
    ) && typeof (value as PromiseLike<unknown>).then === 'function';
}

export async function openDingTalkMiniProgramWebview(
    url: string,
    options: OpenDingTalkMiniProgramLinkOptions = {},
): Promise<void> {
    let parsedUrl: URL;
    try {
        parsedUrl = new URL(url);
    } catch {
        throw new Error('Invalid URL for DingTalk mini-program WebView');
    }
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
        throw new Error('DingTalk mini-program WebView only accepts HTTP(S) URLs');
    }
    const currentHref = options.currentHref
        ?? (typeof window !== 'undefined' ? window.location.href : '');
    let currentUrl: URL;
    try {
        currentUrl = new URL(currentHref);
    } catch {
        throw new Error('Current H5 origin is unavailable for DingTalk navigation');
    }
    if (parsedUrl.origin !== currentUrl.origin) {
        throw new Error('DingTalk mini-program WebView only allows same-origin URLs');
    }

    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    const isMiniProgram = await isDingTalkMiniProgramWebViewRuntime({
        targetWindow,
        targetDocument: options.targetDocument,
        userAgent: options.userAgent,
        sdkUrl: options.sdkUrl,
        loadTimeoutMs: options.loadTimeoutMs,
    });
    const sdk = targetWindow?.dd;
    const navigateTo = sdk?.navigateTo;
    if (!isMiniProgram || !sdk || typeof navigateTo !== 'function') {
        throw new Error('DingTalk mini-program WebView navigation SDK is unavailable');
    }

    const now = options.now ?? Date.now;
    const duplicateWindowMs = options.duplicateWindowMs ?? DEFAULT_DUPLICATE_WINDOW_MS;
    const startedAt = now();
    if (
        duplicateWindowMs > 0
        && lastOpenUrl === parsedUrl.href
        && startedAt - lastOpenStartedAt < duplicateWindowMs
    ) {
        return;
    }
    lastOpenUrl = parsedUrl.href;
    lastOpenStartedAt = startedAt;

    const route = buildDingTalkMiniProgramWebviewRoute(
        parsedUrl.href,
        options.route,
    );
    await new Promise<void>((resolve, reject) => {
        let settled = false;
        const finish = (callback: () => void) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            callback();
        };
        const timer = setTimeout(
            () => finish(() => reject(new Error(
                'Timed out navigating DingTalk mini-program WebView',
            ))),
            Math.max(0, options.navigateTimeoutMs ?? DEFAULT_NAVIGATE_TIMEOUT_MS),
        );

        try {
            const result = navigateTo.call(sdk, {
                url: route,
                success: () => finish(resolve),
                fail: (error) => finish(() => reject(normalizeError(
                    error,
                    'DingTalk mini-program navigateTo failed',
                ))),
            });
            if (isThenable(result)) {
                void Promise.resolve(result).then(
                    () => finish(resolve),
                    (error) => finish(() => reject(normalizeError(
                        error,
                        'DingTalk mini-program navigateTo failed',
                    ))),
                );
            }
        } catch (error) {
            finish(() => reject(normalizeError(
                error,
                'DingTalk mini-program navigateTo failed',
            )));
        }
    });
}
