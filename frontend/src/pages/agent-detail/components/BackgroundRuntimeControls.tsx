import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import DivergenceSlider from '../../../components/DivergenceSlider';
import SelectDropdown from '../../../components/SelectDropdown';
import ReasoningEffortSelect, { type ReasoningEffortValue } from '../../../components/ReasoningEffortSelect';
import { getLlmModelLabel, sortLlmModels } from '../../../utils/llmModels';

type Props = {
    resource: any;
    models: any[];
    disabled?: boolean;
    onSave: (update: Record<string, unknown>) => Promise<unknown>;
    isZh: boolean;
};

export default function BackgroundRuntimeControls({ resource, models, disabled, onSave, isZh }: Props) {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [saving, setSaving] = useState(false);
    const [modelId, setModelId] = useState(resource.model_id || '');
    const [temperature, setTemperature] = useState<number | null>(resource.temperature ?? null);
    const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffortValue>(resource.reasoning_effort || '');
    const [soul, setSoul] = useState(resource.soul !== false);
    const [memory, setMemory] = useState(resource.memory !== false);

    useEffect(() => {
        setModelId(resource.model_id || '');
        setTemperature(resource.temperature ?? null);
        setReasoningEffort(resource.reasoning_effort || '');
        setSoul(resource.soul !== false);
        setMemory(resource.memory !== false);
    }, [resource.model_id, resource.temperature, resource.reasoning_effort, resource.soul, resource.memory]);

    const resetDraft = () => {
        setModelId(resource.model_id || '');
        setTemperature(resource.temperature ?? null);
        setReasoningEffort(resource.reasoning_effort || '');
        setSoul(resource.soul !== false);
        setMemory(resource.memory !== false);
    };
    const dirty = modelId !== (resource.model_id || '')
        || temperature !== (resource.temperature ?? null)
        || reasoningEffort !== (resource.reasoning_effort || '')
        || soul !== (resource.soul !== false)
        || memory !== (resource.memory !== false);
    const modelOptions = [
        { value: '', label: isZh ? '继承数字员工模型' : 'Inherit Digital Employee model' },
        ...sortLlmModels(models.filter((model) => model.enabled || model.id === modelId))
            .map((model) => ({ value: model.id, label: getLlmModelLabel(model) })),
    ];

    return (
        <div onClick={(event) => event.stopPropagation()} style={{ position: 'relative' }}>
            <button className="btn btn-ghost" disabled={disabled} onClick={() => {
                if (!open) resetDraft();
                setOpen((value) => !value);
            }} style={{ padding: '3px 8px', fontSize: 11 }}>
                {isZh ? '运行配置' : 'Runtime'}
            </button>
            {open && (
                <div style={{ position: 'absolute', zIndex: 20, right: 0, top: 'calc(100% + 6px)', width: 300, padding: 14, border: '1px solid var(--border-subtle)', borderRadius: 10, background: 'var(--bg-primary)', boxShadow: '0 12px 32px rgba(0,0,0,.16)' }}>
                    <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>{isZh ? '模型' : 'Model'}</label>
                    <SelectDropdown value={modelId} options={modelOptions} onChange={setModelId} ariaLabel={isZh ? '模型' : 'Model'} style={{ width: '100%', marginBottom: 14 }} />
                    <DivergenceSlider
                        value={temperature}
                        onChange={setTemperature}
                        label={isZh ? '想象力' : 'Imagination'}
                        inheritedLabel={isZh ? '继承数字员工设置' : 'Inherit Agent setting'}
                        lowLabel={isZh ? '稳定' : 'Stable'}
                        middleLabel={isZh ? '均衡' : 'Balanced'}
                        highLabel={isZh ? '丰富' : 'Imaginative'}
                    />
                    <div style={{ marginTop: 12 }}>
                        <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                            {t('reasoning.label')}
                        </label>
                        <ReasoningEffortSelect
                            value={reasoningEffort}
                            onChange={setReasoningEffort}
                            supportedEfforts={models.find((model) => model.id === modelId)?.reasoning_efforts}
                            inheritLabel={t('reasoning.inherit')}
                        />
                    </div>
                    <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
                        <label style={{ fontSize: 12 }}><input type="checkbox" checked={soul} onChange={(event) => setSoul(event.target.checked)} /> {isZh ? '使用 Soul' : 'Use Soul'}</label>
                        <label style={{ fontSize: 12 }}><input type="checkbox" checked={memory} onChange={(event) => setMemory(event.target.checked)} /> {isZh ? '使用记忆' : 'Use memory'}</label>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
                        <button className="btn btn-ghost" onClick={() => { resetDraft(); setOpen(false); }}>{isZh ? '取消' : 'Cancel'}</button>
                        <button className="btn btn-primary" disabled={saving || !dirty} onClick={async () => {
                            setSaving(true);
                            try {
                                await onSave({ model_id: modelId || null, temperature, reasoning_effort: reasoningEffort || null, soul, memory });
                                setOpen(false);
                            } catch {
                                // The shared mutation renders the actionable error toast.
                            } finally {
                                setSaving(false);
                            }
                        }}>{saving ? (isZh ? '保存中…' : 'Saving…') : (isZh ? '保存' : 'Save')}</button>
                    </div>
                </div>
            )}
        </div>
    );
}
