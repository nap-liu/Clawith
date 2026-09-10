import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../services/api';
import SelectDropdown from './SelectDropdown';
import {
    getLlmModelLabel,
    supportsModelPurpose,
    type ModelPurpose,
    sortLlmModels,
    type LlmModelListItem,
} from '../utils/llmModels';

type Props = {
    value: string;
    onChange: (value: string) => void;
    tenantId?: string | null;
    supportsVision?: boolean;
    purpose?: ModelPurpose;
    placeholder?: string;
    disabled?: boolean;
    required?: boolean;
};

export default function LlmModelSelect({
    value,
    onChange,
    tenantId,
    supportsVision = false,
    purpose = "conversation",
    placeholder: customPlaceholder,
    disabled = false,
    required = false,
}: Props) {
    const { t } = useTranslation();
    const { data: models = [], isLoading, isError } = useQuery({
        queryKey: ['llm-models', tenantId || '', purpose],
        queryFn: () => tenantId
            ? enterpriseApi.llmModelsForTenant(tenantId, purpose)
            : enterpriseApi.llmModelsByPurpose(purpose),
    });
    const allModels = models as LlmModelListItem[];
    const currentModel = allModels.find((model) => model.id === value);
    const eligibleModels = sortLlmModels(allModels.filter((model) => (
        model.enabled !== false
        && supportsModelPurpose(model, purpose)
        && (!supportsVision || model.supports_vision === true)
    )));
    const currentIsEligible = eligibleModels.some((model) => model.id === value);
    const options = currentModel && !currentIsEligible
        ? sortLlmModels([currentModel, ...eligibleModels])
        : eligibleModels;
    const hasUnavailableValue = Boolean(value && !currentModel);
    const placeholder = isLoading
        ? t('common.loading', '加载中…')
        : isError
            ? t('common.modelLoadFailed', '模型加载失败')
            : customPlaceholder || (supportsVision
                ? t('common.selectVisionModel', '请选择视觉模型')
                : t('common.selectModel', '请选择模型'));
    const pickerOptions = [
        ...(required ? [] : [{ value: '', label: placeholder }]),
        ...(hasUnavailableValue
            ? [{ value, label: t('common.unavailableConfiguredModel', '当前配置的模型不可用') }]
            : []),
        ...options.map((model) => {
            const unavailable = model.enabled === false
                || (supportsVision && model.supports_vision !== true);
            return {
                value: model.id,
                label: `${getLlmModelLabel(model, t)}${
                    unavailable ? ` ${t('common.unavailableSuffix', '（不可用）')}` : ''
                }`,
            };
        }),
    ];

    return (
        <SelectDropdown
            value={value}
            options={pickerOptions}
            onChange={onChange}
            ariaLabel={supportsVision
                ? t('common.selectVisionModel', '请选择视觉模型')
                : t('common.selectModel', '请选择模型')}
            disabled={disabled || isLoading || isError}
            placeholder={placeholder}
            style={{ width: '100%' }}
        />
    );
}
