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

type DetectDingTalkMiniProgramOptions = {
    targetWindow?: DingTalkHostWindow;
    userAgent?: string;
};

type OpenDingTalkMiniProgramLinkOptions = DetectDingTalkMiniProgramOptions & {
    currentHref?: string;
    route?: string;
    navigateTimeoutMs?: number;
    duplicateWindowMs?: number;
    now?: () => number;
};

type NavigateDingTalkMiniProgramPageOptions = Omit<
    OpenDingTalkMiniProgramLinkOptions,
    'currentHref' | 'route'
>;

export const DEFAULT_DINGTALK_WEBVIEW_ROUTE = '/subPackages/webview/index';

const DEFAULT_NAVIGATE_TIMEOUT_MS = 1500;
const DEFAULT_DUPLICATE_WINDOW_MS = 500;

let lastOpenUrl = '';
let lastOpenStartedAt = 0;

function defaultTargetWindow(): DingTalkHostWindow | undefined {
    return typeof window !== 'undefined'
        ? (window as unknown as DingTalkHostWindow)
        : undefined;
}

function defaultUserAgent(): string {
    return typeof navigator !== 'undefined' ? navigator.userAgent : '';
}

function hasWebViewNavigationSdk(targetWindow?: DingTalkHostWindow): boolean {
    return typeof targetWindow?.dd?.navigateTo === 'function';
}

/**
 * DingTalk marks H5 pages hosted by a mini-program WebView with `dd-web`.
 * SDK loading is handled separately during HTML parsing and must not decide
 * whether the business navigation policy is active.
 */
export function isDingTalkMiniProgramWebViewCandidate(userAgent: string): boolean {
    return /dd-web/i.test(userAgent)
        || (/AliApp\(DingTalk/i.test(userAgent) && /MiniProgram/i.test(userAgent));
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

export async function isDingTalkMiniProgramWebViewRuntime(
    options: DetectDingTalkMiniProgramOptions = {},
): Promise<boolean> {
    const userAgent = options.userAgent ?? defaultUserAgent();
    if (!isDingTalkMiniProgramWebViewCandidate(userAgent)) return false;

    return hasWebViewNavigationSdk(options.targetWindow ?? defaultTargetWindow());
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

    const route = buildDingTalkMiniProgramWebviewRoute(
        parsedUrl.href,
        options.route,
    );
    await navigateDingTalkMiniProgramPage(route, options);
}

/** Navigate directly to an internal page exposed by the host mini-program. */
export async function navigateDingTalkMiniProgramPage(
    route: string,
    options: NavigateDingTalkMiniProgramPageOptions = {},
): Promise<void> {
    if (!route.startsWith('/') || route.startsWith('//') || route.includes('#')) {
        throw new Error('DingTalk mini-program navigation requires an internal page route');
    }

    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    const isMiniProgram = await isDingTalkMiniProgramWebViewRuntime({
        targetWindow,
        userAgent: options.userAgent,
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
        && lastOpenUrl === route
        && startedAt - lastOpenStartedAt < duplicateWindowMs
    ) {
        return;
    }
    lastOpenUrl = route;
    lastOpenStartedAt = startedAt;
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
