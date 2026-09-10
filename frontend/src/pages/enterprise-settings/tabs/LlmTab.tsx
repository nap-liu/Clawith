import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconPlus, IconSettings } from '@tabler/icons-react';
import { useDialog } from '../../../components/Dialog/DialogProvider';
import { useToast } from '../../../components/Toast/ToastProvider';
import ToggleSwitch from '../../../components/ToggleSwitch';
import SelectDropdown from '../../../components/SelectDropdown';
import Button from '../../../components/ui/Button';
import TextInput from '../../../components/ui/TextInput';
import { SettingsDrawer, SettingsField, SettingsSection } from '../../../components/ui/SettingsForm';
import { useAuthStore } from '../../../stores';
import { getLlmModelLabel, getLlmModelName, getLlmModelPlatform, getLlmModelPlatformLabel, sortLlmModels, supportsModelPurpose, type ModelPurpose } from '../../../utils/llmModels';
import { fetchJson } from '../utils/fetchJson';
import LlmModelForm, { type PoolModel, type ProviderSpec } from './LlmModelForm';
import LlmMediaTest from './LlmMediaTest';
import './LlmTab.css';

const MEDIA_DEFAULTS: [string, ModelPurpose][] = [
    ['understanding_model_id', 'media_understanding'],
    ['image_model_id', 'image_generation'],
    ['audio_model_id', 'audio_generation'],
    ['video_model_id', 'video_generation'],
    ['speech_model_id', 'speech_recognition'],
];

export default function LlmTab({ selectedTenantId }: { selectedTenantId: string }) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const qc = useQueryClient();
    const currentUser = useAuthStore(s => s.user);
    const [editing, setEditing] = useState<PoolModel | 'new' | null>(null);
    const [testing, setTesting] = useState<PoolModel | null>(null);
    const [showDefaults, setShowDefaults] = useState(false);
    const [search, setSearch] = useState('');
    const [platform, setPlatform] = useState('');
    const tenantId = selectedTenantId || currentUser?.tenant_id || '';
    const suffix = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : '';
    useEffect(() => {
        setEditing(null);
        setTesting(null);
        setShowDefaults(false);
        setSearch('');
        setPlatform('');
    }, [tenantId]);
    const modelQuery = useQuery({
        queryKey: ['enterprise-model-pool', tenantId],
        queryFn: () => fetchJson<PoolModel[]>(`/enterprise/llm-models${suffix}`),
        enabled: Boolean(tenantId),
    });
    const models = sortLlmModels(modelQuery.data || []);
    const platforms = [...new Map(models.map(model => [getLlmModelPlatform(model), model])).values()];
    const query = search.trim().toLocaleLowerCase();
    const visibleModels = models.filter(model => (!platform || getLlmModelPlatform(model) === platform)
        && (!query || [getLlmModelLabel(model, t), model.model].join(' ').toLocaleLowerCase().includes(query)));
    const { data: providers = [] } = useQuery({
        queryKey: ['llm-provider-specs'],
        queryFn: () => fetchJson<ProviderSpec[]>('/enterprise/llm-providers'),
    });
    const { data: tenant } = useQuery({
        queryKey: ['tenant-default-model', tenantId], enabled: Boolean(tenantId),
        queryFn: () => fetchJson<{ default_model_id: string | null }>(
            tenantId === currentUser?.tenant_id ? '/tenants/me' : `/tenants/${tenantId}`),
    });
    const defaultQuery = useQuery({
        queryKey: ['media-model-defaults', tenantId], enabled: Boolean(tenantId),
        queryFn: () => fetchJson<Record<string, string | null>>(`/enterprise/media-model-defaults${suffix}`),
    });
    const defaults = defaultQuery.data || {};
    const invalidate = () => {
        for (const key of ['enterprise-model-pool', 'llm-models', 'media-model-defaults', 'tenant-default-model', 'tenant', 'agents', 'agent']) {
            void qc.invalidateQueries({ queryKey: [key] });
        }
    };
    const reportError = (error: unknown) => toast.error(t('common.error'), { details: String(error) });
    const save = useMutation({
        mutationFn: (data: Record<string, unknown>) => fetchJson(
            editing && editing !== 'new' ? `/enterprise/llm-models/${editing.id}` : `/enterprise/llm-models${suffix}`, {
                method: editing && editing !== 'new' ? 'PUT' : 'POST', body: JSON.stringify(data),
            }),
        onSuccess: () => {
            invalidate();
            setEditing(null);
        },
    });
    const update = useMutation({
        mutationFn: ({ model, enabled }: { model: PoolModel; enabled: boolean }) => fetchJson(
            `/enterprise/llm-models/${model.id}`, { method: 'PUT', body: JSON.stringify({ enabled }) }),
        onSuccess: invalidate, onError: reportError,
    });
    const setDefault = useMutation({
        mutationFn: (id: string) => fetchJson(`/enterprise/llm-models/${id}/set-default`, { method: 'POST' }),
        onSuccess: invalidate, onError: reportError,
    });
    const saveDefaults = useMutation({
        mutationFn: (patch: Record<string, string | null>) => fetchJson(`/enterprise/media-model-defaults${suffix}`, {
            method: 'PUT', body: JSON.stringify(patch),
        }),
        onSuccess: invalidate, onError: reportError,
    });
    const remove = async (model: PoolModel) => {
        if (!await dialog.confirm(t('enterprise.llm.deleteConfirm', { name: getLlmModelLabel(model, t) }), {
            title: t('common.dialog.deleteModel'), danger: true,
        })) return;
        try {
            await fetchJson(`/enterprise/llm-models/${model.id}`, { method: 'DELETE' });
            invalidate();
        } catch (error) {
            if ((error as { status?: number }).status !== 409) {
                reportError(error);
                return;
            }
            if (!await dialog.confirm(t('enterprise.llm.forceDeleteConfirm'), {
                title: t('common.dialog.deleteModel'), danger: true,
            })) return;
            try {
                await fetchJson(`/enterprise/llm-models/${model.id}?force=true`, { method: 'DELETE' });
                invalidate();
            } catch (failure) {
                reportError(failure);
            }
        }
    };
    const defaultOptions = (field: string, purpose: ModelPurpose) => {
        const eligible = models.filter(model => model.enabled !== false && supportsModelPurpose(model, purpose));
        const selected = field === 'conversation' ? tenant?.default_model_id : defaults[field];
        return [
            ...(field === 'conversation' ? [] : [{ value: '', label: t('enterprise.llm.noDefault') }]),
            ...(selected && !eligible.some(model => model.id === selected)
                ? [{ value: selected, label: t('common.unavailableConfiguredModel') }] : []),
            ...eligible.map(model => ({ value: model.id, label: getLlmModelLabel(model, t) })),
        ];
    };
    return <section className="model-pool">
        <header className="model-pool__header">
            <h2>{t('enterprise.llm.poolTitle')}</h2>
            <div className="model-pool__actions">
                <Button variant="secondary" disabled={!tenantId} onClick={() => setShowDefaults(true)}>
                    <IconSettings size={16} />{t('enterprise.llm.mediaDefaults')}
                </Button>
                <Button variant="primary" disabled={!tenantId || modelQuery.isPending || modelQuery.isError}
                    onClick={() => setEditing('new')}>
                    <IconPlus size={16} />{t('enterprise.llm.addModel')}
                </Button>
            </div>
        </header>
        <div className="model-pool__filters">
            <TextInput type="search" value={search} onChange={event => setSearch(event.target.value)}
                placeholder={t('enterprise.llm.searchModels')} aria-label={t('enterprise.llm.searchModels')} />
            <SelectDropdown value={platform} onChange={setPlatform} style={{ width: '100%' }}
                ariaLabel={t('enterprise.llm.servicePlatform')} options={[
                    { value: '', label: t('enterprise.llm.allPlatforms') },
                    ...platforms.map(model => ({ value: getLlmModelPlatform(model), label: getLlmModelPlatformLabel(model, t) })),
                ]} />
        </div>
        {modelQuery.isPending ? <p className="model-pool__status" role="status">{t('common.loading')}</p>
            : modelQuery.isError ? <p className="model-pool__error" role="alert">{t('common.modelLoadFailed')}
                <Button variant="ghost" onClick={() => void modelQuery.refetch()}>{t('common.retry')}</Button>
            </p> : <div className="model-pool__list">
                {visibleModels.map(model => <article className="model-pool__row" key={model.id}>
                    <div className="model-pool__identity">
                        <div className="model-pool__name">
                            <h3>{getLlmModelName(model)}</h3>
                            <span className="badge">{getLlmModelPlatformLabel(model, t)}</span>
                            {(tenant?.default_model_id === model.id || MEDIA_DEFAULTS.some(([field]) => defaults[field] === model.id)) &&
                                <span className="badge">{t('enterprise.llm.defaultBadge')}</span>}
                        </div>
                        <p>{model.model}</p>
                        {(supportsModelPurpose(model) || supportsModelPurpose(model, 'media_understanding')) &&
                            <p>{t(`enterprise.llm.protocols.${model.effective_api_protocol || 'openai_compatible'}`)}</p>}
                    </div>
                    <div className="model-pool__capabilities">
                        <span>{(model.purposes || ['conversation']).map(p => t(`enterprise.llm.purposes.${p}`)).join(' · ')}</span>
                        <small>{t('enterprise.llm.modalitiesLabel')}: {(model.input_modalities || ['text']).map(m => t(`enterprise.llm.modalities.${m}`)).join(' · ')}</small>
                    </div>
                    <div className="model-pool__actions">
                        <label className="model-pool__enabled">
                            <ToggleSwitch checked={model.enabled !== false} disabled={update.isPending}
                                onChange={enabled => update.mutate({ model, enabled })}
                                ariaLabel={t(model.enabled !== false ? 'enterprise.llm.clickToDisable' : 'enterprise.llm.clickToEnable')} />
                            <span>{t(model.enabled !== false ? 'common.enabled' : 'common.disabled')}</span>
                        </label>
                        {(model.purposes || []).some(p => p !== 'conversation') &&
                            <Button variant="ghost" className="model-pool__test-action" onClick={() => setTesting(model)}>{t('enterprise.llm.mediaTest')}</Button>}
                        <Button variant="ghost" className="model-pool__edit-action" onClick={() => setEditing(model)}>{t('enterprise.llm.edit')}</Button>
                        <Button variant="ghost" className="model-pool__delete" onClick={() => void remove(model)}>{t('common.delete')}</Button>
                    </div>
                </article>)}
                {!visibleModels.length && <p className="model-pool__status">{t(models.length
                    ? 'enterprise.llm.noMatchingModels' : 'enterprise.llm.noModels')}</p>}
            </div>}
        {editing && <LlmModelForm key={`${tenantId}-${editing === 'new' ? 'new' : editing.id}`}
            model={editing === 'new' ? undefined : editing} providers={providers}
            onSave={data => save.mutateAsync(data)} onCancel={() => setEditing(null)} saving={save.isPending} />}
        {testing && <LlmMediaTest key={`${tenantId}-${testing.id}`} model={testing} tenantId={tenantId} onClose={() => setTesting(null)} />}
        {showDefaults && <SettingsDrawer title={t('enterprise.llm.mediaDefaults')} busy={saveDefaults.isPending || setDefault.isPending}
            onClose={() => setShowDefaults(false)} footer={<Button variant="secondary" disabled={saveDefaults.isPending || setDefault.isPending}
                onClick={() => setShowDefaults(false)}>{t('common.close')}</Button>}>
            {defaultQuery.isError && <p className="model-pool__error" role="alert">{t('common.modelLoadFailed')}</p>}
            <SettingsSection title={t('enterprise.llm.purposesLabel')}>
                <SettingsField htmlFor="default-conversation" label={t('enterprise.llm.purposes.conversation')}>
                    <SelectDropdown value={tenant?.default_model_id || ''} style={{ width: '100%' }}
                        disabled={setDefault.isPending || !tenant} options={defaultOptions('conversation', 'conversation')}
                        onChange={value => setDefault.mutate(value)} ariaLabel={t('enterprise.llm.purposes.conversation')} />
                </SettingsField>
                {MEDIA_DEFAULTS.map(([field, purpose]) => <SettingsField key={field}
                    htmlFor={`media-default-${field}`} label={t(`enterprise.llm.purposes.${purpose}`)}>
                    <SelectDropdown value={defaults[field] || ''} style={{ width: '100%' }}
                        disabled={saveDefaults.isPending || defaultQuery.isPending || defaultQuery.isError}
                        options={defaultOptions(field, purpose)} onChange={value => saveDefaults.mutate({ [field]: value || null })}
                        ariaLabel={t(`enterprise.llm.purposes.${purpose}`)} />
                </SettingsField>)}
            </SettingsSection>
        </SettingsDrawer>}
    </section>;
}
