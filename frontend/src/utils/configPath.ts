// Dotted-path helpers for tool config_schema.fields with nested keys.
// Used by both the enterprise-level tool config modal (EnterpriseSettings)
// and the agent-level override modal (AgentDetail/ToolsManager).

export function getByPath<T = any>(obj: any, path: string): T | undefined {
    if (!obj || !path) return undefined;
    return path.split('.').reduce<any>((acc, k) => (acc == null ? acc : acc[k]), obj);
}

export function setByPath(obj: any, path: string, value: any): any {
    // Returns a NEW object tree with the path set (for React state immutability).
    if (!path) return obj;
    const parts = path.split('.');
    const next = { ...(obj || {}) };
    let cur: any = next;
    for (let i = 0; i < parts.length - 1; i++) {
        const k = parts[i];
        cur[k] = { ...(cur[k] || {}) };
        cur = cur[k];
    }
    cur[parts[parts.length - 1]] = value;
    return next;
}
