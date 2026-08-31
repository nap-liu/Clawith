export const CATEGORY_CONFIG_SCHEMAS: Record<string, any> = {
    agentbay: {
        title: 'agent.tools.schemas.agentbay.title',
        fields: [
            { key: 'api_key', label: 'agent.tools.schemas.agentbay.apiKey', type: 'password', placeholder: 'agent.tools.schemas.agentbay.apiKeyPlaceholder' },
            { key: 'os_type', label: 'agent.tools.schemas.agentbay.osType', type: 'select', default: 'windows', options: [{ value: 'linux', label: 'agent.tools.schemas.os.linux' }, { value: 'windows', label: 'agent.tools.schemas.os.windows' }] },
        ]
    },
    atlassian: {
        title: 'agent.tools.schemas.atlassian.title',
        fields: [
            { key: 'api_key', label: 'agent.tools.schemas.atlassian.apiKey', type: 'password', placeholder: 'agent.tools.schemas.atlassian.apiKeyPlaceholder' },
            { key: 'cloud_id', label: 'agent.tools.schemas.atlassian.cloudId', type: 'text', placeholder: 'agent.tools.schemas.atlassian.cloudIdPlaceholder' }
        ]
    }
};

const SENSITIVE_KEYS_BASE = new Set(['api_key', 'private_key', 'auth_code', 'password', 'secret']);

export const getSensitiveKeys = (schema: any): Set<string> => {
    const keys = new Set(SENSITIVE_KEYS_BASE);
    if (schema?.fields) {
        for (const field of schema.fields) {
            if (field.type === 'password') keys.add(field.key);
        }
    }
    return keys;
};

export const applyConfigDefaults = (fields: any[] = [], config: Record<string, any> = {}) => {
    const next = { ...config };
    for (const field of fields) {
        if (field.default !== undefined && (next[field.key] === undefined || next[field.key] === null || next[field.key] === '')) {
            next[field.key] = field.default;
        }
    }
    return next;
};
