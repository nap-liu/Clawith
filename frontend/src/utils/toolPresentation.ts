import type { TFunction } from 'i18next';

export interface ToolPresentationSource {
    key?: string | null;
    internal_name?: string | null;
    name?: string | null;
    tool_name?: string | null;
    display_name?: string | null;
    tool_display_name?: string | null;
    description?: string | null;
    category?: string | null;
    type?: string | null;
    mcp_server_id?: string | null;
    mcp_server_name?: string | null;
    mcp_server_display_name?: string | null;
}

export interface LocalizedToolCategoryPresentation {
    key: string;
    label: string;
    description: string;
}

export interface LocalizedToolPresentation {
    name: string;
    description: string;
    categoryKey: string;
    categoryLabel: string;
    categoryDescription: string;
    groupKey: string;
    groupLabel: string;
    groupDescription: string;
    searchText: string;
}

const text = (value: unknown): string => typeof value === 'string' ? value.trim() : '';

const CATEGORY_TRANSLATION_KEYS: Record<string, string> = {
    agentbay: 'agentbay',
    atlassian: 'atlassian',
    aware: 'aware',
    browser: 'browser',
    business: 'business',
    code: 'code',
    communication: 'communication',
    custom: 'custom',
    database: 'database',
    deploy: 'deploy',
    discovery: 'discovery',
    document: 'document',
    email: 'email',
    feishu: 'feishu',
    file: 'file',
    general: 'general',
    media: 'media',
    okr: 'okr',
    pages: 'pages',
    project_management: 'projectManagement',
    search: 'search',
    social: 'social',
    subagent: 'subagent',
    task: 'task',
};

export function getLocalizedToolCategoryPresentation(
    t: TFunction,
    category: string | null | undefined,
): LocalizedToolCategoryPresentation {
    const key = text(category) || 'general';
    const translationKey = CATEGORY_TRANSLATION_KEYS[key];
    const generalLabel = t('agent.toolCategories.general', 'General');
    return {
        key,
        label: translationKey
            ? t(`agent.toolCategories.${translationKey}`, { defaultValue: generalLabel })
            : generalLabel,
        description: t('agent.toolCategoryDescriptions.' + key, {
            defaultValue: t('agent.tools.categoryFallbackDescription', 'Tools in this category'),
        }),
    };
}

export function getLocalizedToolPresentation(
    t: TFunction,
    tool: ToolPresentationSource,
): LocalizedToolPresentation {
    const identifier = text(tool.key) || text(tool.internal_name) || text(tool.name) || text(tool.tool_name);
    const fallbackName = text(tool.display_name) || text(tool.tool_display_name) || identifier;
    const fallbackDescription = text(tool.description);
    const category = getLocalizedToolCategoryPresentation(t, tool.category);
    const internalServerName = text(tool.mcp_server_name);
    const serverName = text(tool.mcp_server_display_name) || internalServerName;
    const isMcpGroup = tool.type === 'mcp' && Boolean(serverName);
    const groupKey = isMcpGroup
        ? `mcp:${text(tool.mcp_server_id) || serverName.toLocaleLowerCase()}`
        : category.key;
    const legacyName = identifier
        ? t(`enterprise.tools.toolNames.${identifier}`, { defaultValue: fallbackName })
        : fallbackName;
    const legacyDescription = identifier
        ? t(`enterprise.tools.toolDescriptions.${identifier}`, { defaultValue: fallbackDescription })
        : fallbackDescription;
    const name = identifier
        ? t(`agent.toolTranslations.${identifier}.name`, { defaultValue: legacyName })
        : legacyName;
    const description = identifier
        ? t(`agent.toolTranslations.${identifier}.description`, { defaultValue: legacyDescription })
        : legacyDescription;
    const groupLabel = isMcpGroup ? serverName : category.label;
    const groupDescription = isMcpGroup
        ? t('agent.tools.mcpGroupDescription', 'Tools from {{name}}', { name: serverName })
        : category.description;

    return {
        name,
        description,
        categoryKey: category.key,
        categoryLabel: category.label,
        categoryDescription: category.description,
        groupKey,
        groupLabel,
        groupDescription,
        searchText: [
            identifier,
            fallbackName,
            name,
            fallbackDescription,
            description,
            serverName,
            internalServerName,
            category.key,
            category.label,
        ].filter(Boolean).join(' ').toLocaleLowerCase(),
    };
}
