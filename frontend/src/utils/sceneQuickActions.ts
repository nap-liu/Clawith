type SceneQuickActionState = {
    id: string;
    menu_visible?: boolean;
    /** Compatibility-only field returned by older backends. */
    enabled?: boolean;
    type?: string;
};

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
