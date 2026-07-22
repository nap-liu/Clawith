type WeChatGetEnvResult = {
    miniprogram?: boolean;
};

type WeChatNavigateToOptions = {
    url: string;
    success?: () => void;
    fail?: (error?: unknown) => void;
};

type WeChatMiniProgramSdk = {
    getEnv?: (callback: (result: WeChatGetEnvResult) => void) => void;
    navigateTo?: (options: WeChatNavigateToOptions) => unknown;
};

export type WeChatHostWindow = {
    __wxjs_environment?: string;
    wx?: {
        miniProgram?: WeChatMiniProgramSdk;
    };
};

type DetectWeChatMiniProgramOptions = {
    targetWindow?: WeChatHostWindow;
    targetDocument?: Document;
    bridgeWaitTimeoutMs?: number;
    envTimeoutMs?: number;
    jssdkUrl?: string;
};

type OpenWeChatMiniProgramLinkOptions = {
    targetWindow?: WeChatHostWindow;
    currentHref?: string;
    route?: string;
    duplicateWindowMs?: number;
    now?: () => number;
};

type NavigateWeChatMiniProgramPageOptions = Omit<
    OpenWeChatMiniProgramLinkOptions,
    'currentHref' | 'route'
>;

const DEFAULT_JSSDK_URL = 'https://res.wx.qq.com/open/js/jweixin-1.3.2.js';
const DEFAULT_BRIDGE_WAIT_TIMEOUT_MS = 800;
const DEFAULT_ENV_TIMEOUT_MS = 800;
const DEFAULT_DUPLICATE_WINDOW_MS = 500;
export const DEFAULT_WECHAT_WEBVIEW_ROUTE = '/subPackages/webview/index';

let jssdkLoadPromise: Promise<void> | null = null;
let lastOpenUrl = '';
let lastOpenStartedAt = 0;

function defaultTargetWindow(): WeChatHostWindow | undefined {
    return typeof window !== 'undefined'
        ? (window as unknown as WeChatHostWindow)
        : undefined;
}

function defaultTargetDocument(): Document | undefined {
    return typeof document !== 'undefined' ? document : undefined;
}

function hasMiniProgramSdk(targetWindow?: WeChatHostWindow): boolean {
    const miniProgram = targetWindow?.wx?.miniProgram;
    return (
        typeof miniProgram?.getEnv === 'function'
        && typeof miniProgram.navigateTo === 'function'
    );
}

function waitForMiniProgramEnvironmentMarker(
    targetWindow: WeChatHostWindow | undefined,
    targetDocument: Document | undefined,
    timeoutMs: number,
): Promise<boolean> {
    if (targetWindow?.__wxjs_environment === 'miniprogram') {
        return Promise.resolve(true);
    }
    if (!targetDocument) return Promise.resolve(false);

    return new Promise((resolve) => {
        let settled = false;
        let timer: ReturnType<typeof setTimeout> | null = null;

        const finish = () => {
            if (settled) return;
            settled = true;
            targetDocument.removeEventListener('WeixinJSBridgeReady', onBridgeReady);
            if (timer !== null) clearTimeout(timer);
            resolve(targetWindow?.__wxjs_environment === 'miniprogram');
        };
        const onBridgeReady = () => finish();

        targetDocument.addEventListener('WeixinJSBridgeReady', onBridgeReady);
        timer = setTimeout(finish, Math.max(0, timeoutMs));
        if (targetWindow?.__wxjs_environment === 'miniprogram') finish();
    });
}

function loadWechatJssdk(
    targetWindow: WeChatHostWindow | undefined,
    targetDocument: Document | undefined,
    jssdkUrl: string,
): Promise<void> {
    if (hasMiniProgramSdk(targetWindow)) return Promise.resolve();
    if (!targetDocument) {
        return Promise.reject(new Error('Cannot load WeChat JSSDK without a document'));
    }
    if (jssdkLoadPromise) return jssdkLoadPromise;

    const loadPromise = new Promise<void>((resolve, reject) => {
        const existing = targetDocument.querySelector<HTMLScriptElement>(
            'script[data-clawith-wechat-jssdk]',
        );
        const script = existing ?? targetDocument.createElement('script');

        const removeScript = () => {
            if (script.parentNode) script.parentNode.removeChild(script);
        };
        const onLoad = () => {
            if (hasMiniProgramSdk(targetWindow)) {
                resolve();
            } else {
                removeScript();
                reject(new Error('WeChat JSSDK loaded without miniProgram APIs'));
            }
        };
        const onError = () => {
            removeScript();
            reject(new Error('Failed to load WeChat JSSDK'));
        };

        script.addEventListener('load', onLoad, { once: true });
        script.addEventListener('error', onError, { once: true });

        if (!existing) {
            script.async = true;
            script.src = jssdkUrl;
            script.dataset.clawithWechatJssdk = '1';
            targetDocument.head.appendChild(script);
        }
    }).catch((error) => {
        jssdkLoadPromise = null;
        throw error;
    });
    jssdkLoadPromise = loadPromise;

    return loadPromise;
}

function verifyMiniProgramEnvironment(
    targetWindow: WeChatHostWindow | undefined,
    timeoutMs: number,
): Promise<boolean> {
    const getEnv = targetWindow?.wx?.miniProgram?.getEnv;
    if (typeof getEnv !== 'function') return Promise.resolve(false);

    return new Promise((resolve) => {
        let settled = false;
        const finish = (value: boolean) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve(value);
        };
        const timer = setTimeout(() => finish(false), Math.max(0, timeoutMs));

        try {
            getEnv((result) => finish(result?.miniprogram === true));
        } catch {
            finish(false);
        }
    });
}

export async function isWechatMiniProgramWebViewRuntime(
    options: DetectWeChatMiniProgramOptions = {},
): Promise<boolean> {
    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    const targetDocument = options.targetDocument ?? defaultTargetDocument();
    const markerMatched = await waitForMiniProgramEnvironmentMarker(
        targetWindow,
        targetDocument,
        options.bridgeWaitTimeoutMs ?? DEFAULT_BRIDGE_WAIT_TIMEOUT_MS,
    );
    if (!markerMatched) return false;

    try {
        await loadWechatJssdk(
            targetWindow,
            targetDocument,
            options.jssdkUrl ?? DEFAULT_JSSDK_URL,
        );
    } catch {
        // The platform marker is authoritative. Keep the mini-program policy
        // active even when the optional SDK enhancement fails to load, so a
        // cross-origin link cannot bypass the allowlist through native fallback.
        return true;
    }

    return verifyMiniProgramEnvironment(
        targetWindow,
        options.envTimeoutMs ?? DEFAULT_ENV_TIMEOUT_MS,
    );
}

export function buildWechatMiniProgramWebviewRoute(
    url: string,
    route = DEFAULT_WECHAT_WEBVIEW_ROUTE,
): string {
    return `${route}?url=${encodeURIComponent(url)}`;
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
    return (
        (typeof value === 'object' && value !== null)
        || typeof value === 'function'
    ) && typeof (value as PromiseLike<unknown>).then === 'function';
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

export async function openWechatMiniProgramWebview(
    url: string,
    options: OpenWeChatMiniProgramLinkOptions = {},
): Promise<void> {
    let parsedUrl: URL;
    try {
        parsedUrl = new URL(url);
    } catch {
        throw new Error('Invalid URL for WeChat mini-program WebView');
    }
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
        throw new Error('WeChat mini-program WebView only accepts HTTP(S) URLs');
    }
    const currentHref = options.currentHref
        ?? (typeof window !== 'undefined' ? window.location.href : '');
    let currentUrl: URL;
    try {
        currentUrl = new URL(currentHref);
    } catch {
        throw new Error('Current H5 origin is unavailable for WeChat navigation');
    }
    if (parsedUrl.origin !== currentUrl.origin) {
        throw new Error('WeChat mini-program WebView only allows same-origin URLs');
    }

    const route = buildWechatMiniProgramWebviewRoute(
        parsedUrl.href,
        options.route,
    );
    await navigateWechatMiniProgramPage(route, options);
}

/** Navigate directly to an internal page exposed by the host mini-program. */
export async function navigateWechatMiniProgramPage(
    route: string,
    options: NavigateWeChatMiniProgramPageOptions = {},
): Promise<void> {
    if (!route.startsWith('/') || route.startsWith('//') || route.includes('#')) {
        throw new Error('WeChat mini-program navigation requires an internal page route');
    }

    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    const miniProgram = targetWindow?.wx?.miniProgram;
    if (
        targetWindow?.__wxjs_environment !== 'miniprogram'
        || typeof miniProgram?.navigateTo !== 'function'
    ) {
        throw new Error('WeChat mini-program WebView navigation SDK is unavailable');
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
            callback();
        };

        try {
            const result = miniProgram.navigateTo?.({
                url: route,
                success: () => finish(resolve),
                fail: (error) => finish(() => reject(normalizeError(
                    error,
                    'WeChat mini-program navigateTo failed',
                ))),
            });
            if (isThenable(result)) {
                void Promise.resolve(result).then(
                    () => finish(resolve),
                    (error) => finish(() => reject(normalizeError(
                        error,
                        'WeChat mini-program navigateTo failed',
                    ))),
                );
            }
        } catch (error) {
            finish(() => reject(normalizeError(
                error,
                'WeChat mini-program navigateTo failed',
            )));
        }
    });
}
