export type ModelPurpose = "conversation" | "media_understanding" | "image_generation" | "audio_generation" | "video_generation" | "speech_recognition";
export type InputModality = "text" | "image" | "audio" | "video";
export const MODEL_PURPOSES: ModelPurpose[] = ["conversation", "media_understanding", "image_generation", "audio_generation", "video_generation", "speech_recognition"];
export const INPUT_MODALITIES: InputModality[] = ["text", "image", "audio", "video"];

export interface LlmModelListItem {
    id: string;
    provider?: string;
    service_platform?: string;
    model?: string;
    label?: string;
    enabled?: boolean;
    supports_vision?: boolean;
    api_protocol?: string | null;
    effective_api_protocol?: string;
    purposes?: ModelPurpose[];
    input_modalities?: InputModality[];
}

const modelNameCollator = new Intl.Collator(['zh-CN', 'en'], {
    numeric: true,
    sensitivity: 'base',
});

type ModelLabelTranslate = (key: string, options: { defaultValue: string }) => string;

export function getLlmModelName(model: LlmModelListItem): string {
    return model.label?.trim()
        || model.model
        || model.id;
}

export function getLlmModelPlatform(model: LlmModelListItem): string {
    return model.service_platform || model.provider || '';
}

export function getLlmModelPlatformLabel(model: LlmModelListItem, t: ModelLabelTranslate): string {
    const platform = getLlmModelPlatform(model);
    return platform ? t(`enterprise.llm.providers.${platform}`, { defaultValue: platform }) : '';
}

export function getLlmModelLabel(model: LlmModelListItem, t: ModelLabelTranslate): string {
    return [getLlmModelName(model), getLlmModelPlatformLabel(model, t)].filter(Boolean).join(' · ');
}

export function sortLlmModels<T extends LlmModelListItem>(models: readonly T[]): T[] {
    return models
        .map((model, index) => ({ model, index }))
        .sort((left, right) => {
            const byName = modelNameCollator.compare(
                getLlmModelName(left.model),
                getLlmModelName(right.model),
            );
            if (byName !== 0) return byName;

            const byProvider = modelNameCollator.compare(
                getLlmModelPlatform(left.model),
                getLlmModelPlatform(right.model),
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

export function supportsModelPurpose(model: LlmModelListItem, purpose: ModelPurpose = "conversation"): boolean {
    return (model.purposes ?? ["conversation"]).includes(purpose);
}
