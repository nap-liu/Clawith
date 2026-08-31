type SceneQuickActionState = {
    id: string;
    menu_visible?: boolean;
    /** Compatibility-only field returned by older backends. */
    enabled?: boolean;
    type?: string;
};

type SceneQuickActionTextStyle = {
    bold: boolean;
    italic: boolean;
    color?: string | null;
    font: 'default' | 'sans' | 'serif' | 'monospace';
};

const QUICK_ACTION_FONT_FAMILIES: Record<SceneQuickActionTextStyle['font'], string | undefined> = {
    default: undefined,
    sans: '"IBM Plex Sans", var(--font-family)',
    serif: '"Newsreader", Georgia, "Times New Roman", serif',
    monospace: 'var(--font-mono)',
};

export function horizontalSceneQuickActionStyle(style: SceneQuickActionTextStyle | null | undefined) {
    if (!style) return undefined;
    return {
        color: style.color || undefined,
        fontFamily: QUICK_ACTION_FONT_FAMILIES[style.font],
        fontStyle: style.italic ? 'italic' : 'normal',
        fontWeight: style.bold ? 700 : 400,
    };
}

export function menuVisibleSceneQuickActions<T extends SceneQuickActionState>(
    actions: readonly T[] | null | undefined,
): T[] {
    return (actions || []).filter((action) => (
        action.menu_visible ?? action.enabled ?? true
    ));
}

export function findMenuVisibleSceneQuickAction<T extends SceneQuickActionState>(
    actions: readonly T[] | null | undefined,
    actionId: string,
): T | undefined {
    return menuVisibleSceneQuickActions(actions).find((action) => action.id === actionId);
}

export function isSceneQuickActionUnavailable(
    action: SceneQuickActionState,
    options: {
        confirmationPending: boolean;
        sendMessageUnavailable: boolean;
    },
): boolean {
    return options.confirmationPending
        || (action.type === 'send_message' && options.sendMessageUnavailable);
}
