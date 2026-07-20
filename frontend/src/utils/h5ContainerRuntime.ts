export type H5ContainerRuntime =
    | 'standard'
    | 'wechat-miniapp-webview'
    | 'dingtalk-miniapp-webview';

type DingTalkRuntimeWindow = {
    dd?: {
        env?: {
            appType?: string;
        };
        openLink?: unknown;
    };
};

type WeChatRuntimeWindow = {
    __wxjs_environment?: string;
};

export type DetectH5ContainerRuntimeOptions = {
    targetWindow?: DingTalkRuntimeWindow & WeChatRuntimeWindow;
    targetDocument?: Document;
    userAgent?: string;
    wechatBridgeWaitTimeoutMs?: number;
    wechatEnvTimeoutMs?: number;
};

function defaultTargetWindow(): DetectH5ContainerRuntimeOptions['targetWindow'] {
    return typeof window !== 'undefined'
        ? (window as unknown as DingTalkRuntimeWindow & WeChatRuntimeWindow)
        : undefined;
}

export function isDingTalkMiniProgramWebViewRuntime(
    targetWindow: DingTalkRuntimeWindow | undefined = defaultTargetWindow(),
): boolean {
    return (
        targetWindow?.dd?.env?.appType === 'WEBVIEW_IN_MINIAPP'
        && typeof targetWindow.dd.openLink === 'function'
    );
}

function isWeChatRuntimeCandidate(
    targetWindow: WeChatRuntimeWindow | undefined,
    userAgent: string,
): boolean {
    if (targetWindow?.__wxjs_environment === 'miniprogram') return true;
    // UA is only a cheap pre-filter to avoid loading the WeChat adapter in
    // unrelated browsers. It never activates mini-program behavior itself.
    return userAgent.toLowerCase().includes('micromessenger');
}

export async function detectH5ContainerRuntime(
    options: DetectH5ContainerRuntimeOptions = {},
): Promise<H5ContainerRuntime> {
    const targetWindow = options.targetWindow ?? defaultTargetWindow();
    if (isDingTalkMiniProgramWebViewRuntime(targetWindow)) {
        return 'dingtalk-miniapp-webview';
    }

    const userAgent = options.userAgent
        ?? (typeof navigator !== 'undefined' ? navigator.userAgent : '');
    if (!isWeChatRuntimeCandidate(targetWindow, userAgent)) {
        return 'standard';
    }

    const { isWechatMiniProgramWebViewRuntime } = await import('./wechatMiniProgramLink');
    const isWechatMiniProgram = await isWechatMiniProgramWebViewRuntime({
        targetWindow,
        targetDocument: options.targetDocument,
        bridgeWaitTimeoutMs: options.wechatBridgeWaitTimeoutMs,
        envTimeoutMs: options.wechatEnvTimeoutMs,
    });
    return isWechatMiniProgram ? 'wechat-miniapp-webview' : 'standard';
}
