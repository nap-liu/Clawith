export type ThemeMode = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

export const SYSTEM_THEME_QUERY = '(prefers-color-scheme: dark)';
export const THEME_META_COLORS: Record<ResolvedTheme, string> = {
    light: '#f8f8f7',
    dark: '#0a0a0f',
};

export interface ClawithThemeBridge {
    setTheme?: (theme: ThemeMode) => void;
}

declare global {
    interface Window {
        ClawithThemeBridge?: ClawithThemeBridge;
    }
}

export function parseThemeMode(value: string | null | undefined): ThemeMode {
    const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
    if (normalized === 'light' || normalized === 'dark') return normalized;
    return 'system';
}

export function readSavedTheme(targetWindow: Window = window): ResolvedTheme {
    try {
        return targetWindow.localStorage.getItem('theme') === 'dark' ? 'dark' : 'light';
    } catch {
        return 'light';
    }
}

export function saveTheme(theme: ResolvedTheme, targetWindow: Window = window): boolean {
    try {
        targetWindow.localStorage.setItem('theme', theme);
        return true;
    } catch {
        return false;
    }
}

export function readSystemTheme(
    targetWindow: Window = window,
    mediaQuery?: MediaQueryList | null,
): ResolvedTheme {
    try {
        const query = mediaQuery || targetWindow.matchMedia?.(SYSTEM_THEME_QUERY);
        return query?.matches ? 'dark' : 'light';
    } catch {
        return 'light';
    }
}

export function resolveThemeMode(
    mode: ThemeMode,
    targetWindow: Window = window,
): ResolvedTheme {
    return mode === 'system' ? readSystemTheme(targetWindow) : mode;
}

export function applyDocumentTheme(
    resolvedTheme: ResolvedTheme,
    mode: ThemeMode = resolvedTheme,
    targetDocument: Document = document,
) {
    const root = targetDocument.documentElement;
    root.setAttribute('data-theme', resolvedTheme);
    root.setAttribute('data-theme-mode', mode);
    root.style.colorScheme = resolvedTheme;

    const themeColor = targetDocument.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    themeColor?.setAttribute('content', THEME_META_COLORS[resolvedTheme]);
}

type ThemeControllerOptions = {
    mode: ThemeMode;
    targetWindow?: Window;
    targetDocument?: Document;
    onThemeChange?: (theme: ResolvedTheme) => void;
};

export function installThemeController({
    mode,
    targetWindow = window,
    targetDocument = document,
    onThemeChange,
}: ThemeControllerOptions) {
    let disposed = false;
    let hostTheme: ResolvedTheme | null = null;
    let mediaQuery: MediaQueryList | null = null;
    let detachMediaQuery: (() => void) | null = null;
    const detachWindowListeners: Array<() => void> = [];

    if (mode === 'system') {
        try {
            mediaQuery = targetWindow.matchMedia?.(SYSTEM_THEME_QUERY) || null;
        } catch {
            mediaQuery = null;
        }
    }

    const currentTheme = (): ResolvedTheme => {
        if (mode !== 'system') return mode;
        if (hostTheme) return hostTheme;
        return readSystemTheme(targetWindow, mediaQuery);
    };

    const emit = () => {
        if (disposed) return;
        const resolvedTheme = currentTheme();
        applyDocumentTheme(resolvedTheme, mode, targetDocument);
        onThemeChange?.(resolvedTheme);
    };

    const handleSystemThemeChange = () => {
        if (!hostTheme) emit();
    };

    if (mode === 'system' && mediaQuery) {
        if (
            typeof mediaQuery.addEventListener === 'function'
            && typeof mediaQuery.removeEventListener === 'function'
        ) {
            try {
                mediaQuery.addEventListener('change', handleSystemThemeChange);
                detachMediaQuery = () => mediaQuery?.removeEventListener?.('change', handleSystemThemeChange);
            } catch {
                detachMediaQuery = null;
            }
        }

        if (
            !detachMediaQuery
            && typeof mediaQuery.addListener === 'function'
            && typeof mediaQuery.removeListener === 'function'
        ) {
            try {
                mediaQuery.addListener(handleSystemThemeChange);
                detachMediaQuery = () => mediaQuery?.removeListener?.(handleSystemThemeChange);
            } catch {
                detachMediaQuery = null;
            }
        }
    }

    if (mode === 'system') {
        const handlePageShow = () => emit();
        const handleVisibilityChange = () => {
            if (targetDocument.visibilityState === 'visible') emit();
        };

        targetWindow.addEventListener('pageshow', handlePageShow);
        targetDocument.addEventListener('visibilitychange', handleVisibilityChange);
        detachWindowListeners.push(
            () => targetWindow.removeEventListener('pageshow', handlePageShow),
            () => targetDocument.removeEventListener('visibilitychange', handleVisibilityChange),
        );
    }

    const setHostTheme = (value: ThemeMode) => {
        if (disposed || mode !== 'system') return;
        const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
        if (normalized !== 'light' && normalized !== 'dark' && normalized !== 'system') return;
        hostTheme = normalized === 'system' ? null : normalized;
        emit();
    };

    let bridgeTarget: ClawithThemeBridge | null = null;
    let bridgeWasCreated = false;
    let bridgeHadOwnSetter = false;
    let previousBridgeSetter: ClawithThemeBridge['setTheme'];

    if (mode === 'system') {
        try {
            bridgeTarget = targetWindow.ClawithThemeBridge || {};
            bridgeWasCreated = !targetWindow.ClawithThemeBridge;
            bridgeHadOwnSetter = Object.prototype.hasOwnProperty.call(bridgeTarget, 'setTheme');
            previousBridgeSetter = bridgeTarget.setTheme;
            bridgeTarget.setTheme = setHostTheme;
            if (bridgeWasCreated) targetWindow.ClawithThemeBridge = bridgeTarget;
        } catch {
            bridgeTarget = null;
        }
    }

    emit();

    return () => {
        if (disposed) return;
        disposed = true;
        detachMediaQuery?.();
        detachWindowListeners.forEach((detach) => detach());

        if (!bridgeTarget || bridgeTarget.setTheme !== setHostTheme) return;
        try {
            if (bridgeHadOwnSetter) {
                bridgeTarget.setTheme = previousBridgeSetter;
            } else {
                delete bridgeTarget.setTheme;
            }
            if (bridgeWasCreated && targetWindow.ClawithThemeBridge === bridgeTarget) {
                delete targetWindow.ClawithThemeBridge;
            }
        } catch {
            // A host may freeze its bridge object after installation.
        }
    };
}
