export interface LlmModelListItem {
    id: string;
    provider?: string;
    model?: string;
    label?: string;
    enabled?: boolean;
    supports_vision?: boolean;
}

const modelNameCollator = new Intl.Collator(['zh-CN', 'en'], {
    numeric: true,
    sensitivity: 'base',
});

export function getLlmModelLabel(model: LlmModelListItem): string {
    return model.label?.trim()
        || [model.provider, model.model].filter(Boolean).join(' · ')
        || model.id;
}

export function sortLlmModels<T extends LlmModelListItem>(models: readonly T[]): T[] {
    return models
        .map((model, index) => ({ model, index }))
        .sort((left, right) => {
            const byName = modelNameCollator.compare(
                getLlmModelLabel(left.model),
                getLlmModelLabel(right.model),
            );
            if (byName !== 0) return byName;

            const byProvider = modelNameCollator.compare(
                left.model.provider || '',
                right.model.provider || '',
            );
            if (byProvider !== 0) return byProvider;

            const byModel = modelNameCollator.compare(
                left.model.model || '',
                right.model.model || '',
            );
            const byId = modelNameCollator.compare(left.model.id, right.model.id);
            return byModel || byId || left.index - right.index;
        })
        .map(({ model }) => model);
}
