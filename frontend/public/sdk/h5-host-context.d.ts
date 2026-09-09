export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type HostContextBridgeOptions = {
    iframe: HTMLIFrameElement;
    frameOrigin: string;
    /** Keep the original authorized page/selection snapshot stable for each requestId, including retries. */
    getContext: (request: { requestId: string; signal: AbortSignal }) => JsonValue | Promise<JsonValue>;
};

export declare function createHostContextBridge(options: HostContextBridgeOptions): { dispose(): void };
