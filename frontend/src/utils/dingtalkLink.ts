type DingTalkOpenLinkParams = {
    url: string;
};

type DingTalkOpenLink = (params: DingTalkOpenLinkParams) => unknown;

export type DingTalkSdk = {
    env?: {
        appType?: string;
    };
    openLink?: DingTalkOpenLink;
};

export type DingTalkHostWindow = {
    dd?: DingTalkSdk;
};

type OpenDingTalkExternalLinkOptions = {
    targetWindow?: DingTalkHostWindow;
    duplicateWindowMs?: number;
    now?: () => number;
};

type ResolvedOpenLink = {
    context: object;
    invoke: DingTalkOpenLink;
};

const DEFAULT_DUPLICATE_WINDOW_MS = 500;

let lastOpenUrl = '';
let lastOpenStartedAt = 0;

function defaultTargetWindow(): DingTalkHostWindow | undefined {
    return typeof window !== 'undefined'
        ? (window as unknown as DingTalkHostWindow)
        : undefined;
}

function resolveOpenLink(targetWindow?: DingTalkHostWindow): ResolvedOpenLink | null {
    const sdk = targetWindow?.dd;
    if (!sdk) return null;

    if (
        sdk.env?.appType === 'WEBVIEW_IN_MINIAPP'
        && typeof sdk.openLink === 'function'
    ) {
        return { context: sdk, invoke: sdk.openLink };
    }

    return null;
}

export function hasDingTalkMiniProgramOpenLink(
    targetWindow: DingTalkHostWindow | undefined = defaultTargetWindow(),
): boolean {
    return resolveOpenLink(targetWindow) !== null;
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
    return (
        (typeof value === 'object' && value !== null)
        || typeof value === 'function'
    ) && typeof (value as PromiseLike<unknown>).then === 'function';
}

async function invokeOpenLink(
    openLink: ResolvedOpenLink,
    url: string,
): Promise<void> {
    const result = openLink.invoke.call(openLink.context, { url });
    if (!isThenable(result)) {
        throw new Error('DingTalk openLink did not return a Promise');
    }
    await result;
}

/**
 * Open an external HTTP(S) URL with the SDK injected by the DingTalk WebView.
 * No JSAPI authorization or backend signature is performed here.
 */
export async function openDingTalkExternalLink(
    url: string,
    options: OpenDingTalkExternalLinkOptions = {},
): Promise<void> {
    let parsedUrl: URL;
    try {
        parsedUrl = new URL(url);
    } catch {
        throw new Error('Invalid URL for DingTalk openLink');
    }
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
        throw new Error('DingTalk openLink only accepts HTTP(S) URLs');
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

    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    const openLink = resolveOpenLink(targetWindow);
    if (!openLink) {
        throw new Error('DingTalk mini-program WebView openLink SDK is unavailable');
    }
    await invokeOpenLink(openLink, parsedUrl.href);
}
