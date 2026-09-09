import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import SelectDropdown from '../../../components/SelectDropdown';
import MultiSelectDropdown from '../../../components/ui/MultiSelectDropdown';
import Button from '../../../components/ui/Button';
import TextInput from '../../../components/ui/TextInput';
import { SettingsDrawer, SettingsField, SettingsSection } from '../../../components/ui/SettingsForm';
import DivergenceSlider from '../../../components/DivergenceSlider';
import ReasoningEffortSelect, { type ReasoningEffortValue } from '../../../components/ReasoningEffortSelect';
import { INPUT_MODALITIES, MODEL_PURPOSES, type LlmModelListItem } from '../../../utils/llmModels';
import { fetchJson } from '../utils/fetchJson';

export interface PoolModel extends LlmModelListItem {
    provider: string;
    model: string;
    label: string;
    base_url?: string;
    max_output_tokens?: number;
    request_timeout?: number;
    temperature?: number;
    reasoning_effort?: string | null;
    reasoning_efforts?: string[];
    context_window: number;
    context_usage_ratio: number;
    keep_recent_turns: number;
}
export interface ProviderSpec {
    provider: string;
    display_name: string;
    protocol: string;
    preferred_protocol?: string;
    default_base_url?: string | null;
    default_max_tokens: number;
}

export default function LlmModelForm({ model, providers, onSave, onCancel, saving }: {
    model?: PoolModel;
    providers: ProviderSpec[];
    onSave: (data: Record<string, unknown>) => Promise<unknown>;
    onCancel: () => void;
    saving: boolean;
}) {
    const { t, i18n } = useTranslation();
    const formId = useId();
    const defaultSpec = providers.find(p => p.provider === 'openai') || providers[0];
    const [form, setForm] = useState({
        provider: model?.provider || defaultSpec?.provider || 'openai',
        api_protocol: model?.api_protocol || (model ? '' : defaultSpec?.preferred_protocol || 'openai_responses'),
        purposes: model?.purposes || ['conversation'],
        input_modalities: model?.input_modalities || (model?.supports_vision ? ['text', 'image'] : ['text']),
        model: model?.model || '',
        label: model?.label || '',
        api_key: '',
        base_url: model?.base_url || (model ? '' : defaultSpec?.default_base_url || ''),
        max_output_tokens: String(model?.max_output_tokens ?? defaultSpec?.default_max_tokens ?? 4096),
        request_timeout: String(model?.request_timeout ?? ''),
        temperature: model?.temperature ?? null as number | null,
        reasoning_effort: (model?.reasoning_effort || '') as ReasoningEffortValue,
        context_window: String(model?.context_window ?? 32000),
        context_usage_percent: String(Math.round((model?.context_usage_ratio ?? .7) * 100)),
        keep_recent_turns: String(model?.keep_recent_turns ?? 3),
    });
    const [testing, setTesting] = useState(false);
    const [testResult, setTestResult] = useState('');
    const [error, setError] = useState('');
    const busy = saving || testing;
    const update = (patch: Partial<typeof form>) => {
        setForm(current => ({ ...current, ...patch }));
        setTestResult('');
        setError('');
    };
    const canChat = form.purposes.includes('conversation') || form.purposes.includes('media_understanding');
    const unchangedModel = model && form.provider === model.provider && form.model === model.model
        && form.base_url === (model.base_url || '') && form.api_protocol === (model.api_protocol || '');
    const invalid = !form.model.trim() || (!model && !form.api_key.trim())
        || !form.purposes.length || !form.input_modalities.length;
    const payload = () => ({
        ...form,
        api_protocol: form.api_protocol || null,
        max_output_tokens: form.max_output_tokens ? Number(form.max_output_tokens) : null,
        request_timeout: form.request_timeout ? Number(form.request_timeout) : null,
        reasoning_effort: form.reasoning_effort || null,
        context_window: Number(form.context_window),
        context_usage_ratio: Number(form.context_usage_percent) / 100,
        keep_recent_turns: Number(form.keep_recent_turns),
    });
    const submit = async () => {
        if (busy || invalid) return;
        setError('');
        try {
            await onSave(payload());
        } catch (failure) {
            setError(String(failure));
        }
    };
    const test = async () => {
        setTesting(true);
        setTestResult('');
        setError('');
        try {
            const result = await fetchJson<{ success: boolean; latency_ms: number; error?: string }>('/enterprise/llm-test', {
                method: 'POST', body: JSON.stringify({ ...payload(), model_id: model?.id }),
            });
            if (result.success) setTestResult(t('enterprise.llm.testSuccess', { latency: result.latency_ms }));
            else setError(result.error || t('enterprise.llm.testFailedShort'));
        } catch (failure) {
            setError(String(failure));
        } finally {
            setTesting(false);
        }
    };
    type InputKey = 'model' | 'label' | 'base_url' | 'api_key' | 'max_output_tokens'
        | 'request_timeout' | 'context_window' | 'context_usage_percent' | 'keep_recent_turns';
    const input = (key: InputKey, label: string, type = 'text', hint?: string) => (
        <SettingsField key={key} htmlFor={`${formId}-${key}`} label={t(label)} hint={hint}>
            <TextInput id={`${formId}-${key}`} type={type} value={form[key]} disabled={busy}
                placeholder={key === 'api_key' && model ? t('enterprise.llm.keepApiKey') : undefined}
                onChange={event => update({ [key]: event.target.value })} />
        </SettingsField>
    );
    const multi = (key: 'purposes' | 'input_modalities', options: readonly string[], label: string, prefix: string) => (
        <SettingsField htmlFor={`${formId}-${key}`} label={t(label)}>
            <MultiSelectDropdown values={form[key]} disabled={busy}
                options={options.map(value => ({ value, label: t(`${prefix}.${value}`) }))}
                onChange={values => update({ [key]: values,
                    ...(key === 'purposes' && values.includes('speech_recognition') && !form.input_modalities.includes('audio')
                        ? { input_modalities: [...form.input_modalities, 'audio'] } : {}),
                })} emptyLabel={t('common.select')}
                selectedLabel={count => t('enterprise.llm.selectedCount', { count })}
                searchPlaceholder={t('common.search')} noOptionsLabel={t('common.noData')}
                noMatchesLabel={t('common.noData')} ariaLabel={t(label)} clearLabel={t('common.clear')} />
        </SettingsField>
    );
    const providerLabel = (provider: ProviderSpec) => {
        const key = `enterprise.llm.providers.${provider.provider}`;
        return i18n.exists(key) ? t(key) : provider.display_name;
    };
    return <SettingsDrawer title={t(model ? 'enterprise.llm.editModel' : 'enterprise.llm.addModel')}
        busy={busy} onClose={onCancel} footer={<>
            <Button variant="secondary" disabled={busy} onClick={onCancel}>{t('common.cancel')}</Button>
            {form.purposes.includes('conversation') && <Button variant="secondary"
                disabled={busy || invalid} onClick={() => void test()}>
                {t(testing ? 'enterprise.llm.testing' : 'enterprise.llm.test')}
            </Button>}
            <Button variant="primary" type="submit" form={formId} disabled={busy || invalid}>{t('common.save')}</Button>
        </>}>
        <form id={formId} onSubmit={event => { event.preventDefault(); void submit(); }} aria-busy={busy}>
            {error && <p className="model-pool__error" role="alert">{error}</p>}
            {testResult && <p className="model-pool__status" role="status">{testResult}</p>}
            <SettingsSection title={t('enterprise.llm.connectionSettings')}>
                <SettingsField htmlFor={`${formId}-provider`} label={t('enterprise.llm.provider')}>
                    <SelectDropdown value={form.provider} disabled={busy} style={{ width: '100%' }}
                        options={[
                            ...providers.map(provider => ({ value: provider.provider, label: providerLabel(provider) })),
                            ...(providers.some(p => p.provider === form.provider) ? [] : [{ value: form.provider, label: form.provider }]),
                        ]} onChange={provider => {
                            const spec = providers.find(p => p.provider === provider);
                            update({ provider, base_url: spec?.default_base_url || '',
                                api_protocol: spec?.preferred_protocol || spec?.protocol || 'openai_compatible' });
                        }} ariaLabel={t('enterprise.llm.provider')} />
                </SettingsField>
                <div className="settings-field-grid">
                    {input('model', 'enterprise.llm.model')}
                    {input('label', 'enterprise.llm.label')}
                </div>
                {input('api_key', 'enterprise.llm.apiKey', 'password')}
                {input('base_url', 'enterprise.llm.baseUrl')}
                {canChat && <SettingsField htmlFor={`${formId}-protocol`} label={t('enterprise.llm.protocol')}>
                    <SelectDropdown value={form.api_protocol} disabled={busy} style={{ width: '100%' }}
                        options={['', 'openai_responses', 'openai_compatible', 'anthropic', 'gemini'].map(value => ({ value, label: t(`enterprise.llm.protocols.${value || 'inherit'}`) }))}
                        onChange={api_protocol => update({ api_protocol })} ariaLabel={t('enterprise.llm.protocol')} />
                </SettingsField>}
            </SettingsSection>
            <SettingsSection title={t('enterprise.llm.capabilitySettings')}>
                {multi('purposes', MODEL_PURPOSES, 'enterprise.llm.purposesLabel', 'enterprise.llm.purposes')}
                {multi('input_modalities', INPUT_MODALITIES, 'enterprise.llm.modalitiesLabel', 'enterprise.llm.modalities')}
            </SettingsSection>
            {canChat && <SettingsSection title={t('enterprise.llm.responseSettings')}>
                <div className="settings-field-grid">
                    <DivergenceSlider value={form.temperature} onChange={temperature => update({ temperature })}
                        disabled={busy} label={t('enterprise.llm.temperature')}
                        inheritedLabel={t('enterprise.llm.providerDefault')}
                        lowLabel={t('enterprise.llm.imaginationLow')} middleLabel={t('enterprise.llm.imaginationMiddle')}
                        highLabel={t('enterprise.llm.imaginationHigh')} />
                    <SettingsField htmlFor={`${formId}-reasoning`} label={t('reasoning.label')}>
                        <ReasoningEffortSelect value={form.reasoning_effort} onChange={reasoning_effort => update({ reasoning_effort })}
                            supportedEfforts={unchangedModel ? model.reasoning_efforts : undefined}
                            inheritLabel={t('reasoning.inherit')} disabled={busy} style={{ width: '100%' }} />
                    </SettingsField>
                </div>
                <div className="settings-field-grid">
                    {input('max_output_tokens', 'enterprise.llm.maxOutputTokens', 'number')}
                    {input('context_window', 'enterprise.llm.contextWindow', 'number')}
                    {input('context_usage_percent', 'enterprise.llm.contextUsagePercent', 'number')}
                    {input('keep_recent_turns', 'enterprise.llm.keepRecentTurns', 'number')}
                </div>
            </SettingsSection>}
            <SettingsSection title={t('enterprise.llm.requestSettings')}>
                {input('request_timeout', 'enterprise.llm.requestTimeout', 'number', t('enterprise.llm.requestTimeoutDesc'))}
            </SettingsSection>
        </form>
    </SettingsDrawer>;
}
