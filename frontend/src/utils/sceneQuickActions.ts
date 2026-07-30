type SceneQuickActionState = {
    id: string;
    enabled?: boolean;
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
