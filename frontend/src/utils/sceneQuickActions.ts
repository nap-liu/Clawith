type SceneQuickActionState = {
    id: string;
    enabled?: boolean;
    type?: string;
};

export function enabledSceneQuickActions<T extends SceneQuickActionState>(
    actions: readonly T[] | null | undefined,
): T[] {
    return (actions || []).filter((action) => action.enabled !== false);
}

export function findEnabledSceneQuickAction<T extends SceneQuickActionState>(
    actions: readonly T[] | null | undefined,
    actionId: string,
): T | undefined {
    return enabledSceneQuickActions(actions).find((action) => action.id === actionId);
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
