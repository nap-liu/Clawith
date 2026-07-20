export type H5ContainerRuntime =
    | 'standard'
    | 'wechat-miniapp-webview'
    | 'dingtalk-miniapp-webview';

import type { DingTalkHostWindow } from './dingtalkLink';

type WeChatRuntimeWindow = {
    __wxjs_environment?: string;
};

export type DetectH5ContainerRuntimeOptions = {
    targetWindow?: DingTalkHostWindow & WeChatRuntimeWindow;
    targetDocument?: Document;
    userAgent?: string;
    dingtalkSdkLoadTimeoutMs?: number;
    wechatBridgeWaitTimeoutMs?: number;
    wechatEnvTimeoutMs?: number;
};

function defaultTargetWindow(): DetectH5ContainerRuntimeOptions['targetWindow'] {
    return typeof window !== 'undefined'
        ? (window as unknown as DingTalkHostWindow & WeChatRuntimeWindow)
        : undefined;
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
    const userAgent = options.userAgent
        ?? (typeof navigator !== 'undefined' ? navigator.userAgent : '');
    const {
        isDingTalkMiniProgramWebViewCandidate,
        isDingTalkMiniProgramWebViewRuntime,
    } = await import('./dingtalkLink');
    if (isDingTalkMiniProgramWebViewCandidate(userAgent)) {
        const isDingTalkMiniProgram = await isDingTalkMiniProgramWebViewRuntime({
            targetWindow,
            targetDocument: options.targetDocument,
            userAgent,
            loadTimeoutMs: options.dingtalkSdkLoadTimeoutMs,
        });
        if (isDingTalkMiniProgram) return 'dingtalk-miniapp-webview';
    }

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
