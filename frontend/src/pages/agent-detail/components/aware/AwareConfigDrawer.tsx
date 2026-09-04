import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { IconTrash, IconX } from '@tabler/icons-react';

import Checkbox from '../../../../components/ui/Checkbox';
import Button from '../../../../components/ui/Button';
import { Drawer } from '../../../../components/Dialog/DialogProvider';
import DivergenceSlider from '../../../../components/DivergenceSlider';
import ReasoningEffortSelect, { type ReasoningEffortValue } from '../../../../components/ReasoningEffortSelect';
import SelectDropdown from '../../../../components/SelectDropdown';
import ToggleSwitch from '../../../../components/ToggleSwitch';
import { getLlmModelLabel, sortLlmModels } from '../../../../utils/llmModels';
import { fromDateTimeInput, resourceCreator, toDateTimeInput } from './awareFormatters';
import type { BackgroundResourceTarget, Translate } from './types';

type DrawerTab = 'business' | 'identity' | 'runtime';

type Props = {
    target: BackgroundResourceTarget;
    models: any[];
    inheritedModelId?: string | null;
    supervisionHumanTargets: any[];
    supervisionAgentTargets: any[];
    canEdit: boolean;
    canDelete: boolean;
    canReassign: boolean;
    currentUserId?: string | null;
    t: Translate;
    onClose: () => void;
    onSave: (type: 'trigger' | 'task' | 'schedule', id: string, update: Record<string, unknown>) => Promise<void>;
    onDelete: (type: 'trigger' | 'schedule', resource: any) => Promise<void>;
    onChooseIdentity: (type: 'trigger' | 'task' | 'schedule', resource: any) => void;
    confirmExecutionAlignment: () => Promise<boolean>;
    confirmWebhookModeChange: () => Promise<boolean>;
    validateTiming: (kind: 'cron' | 'datetime' | 'timezone', value: string, timezone?: string) => Promise<boolean>;
};

const OUTBOUND_CHANNELS = new Set(['feishu', 'dingtalk', 'wecom', 'slack', 'teams', 'wechat']);
const REMINDER_PRESETS = ['', 'daily', 'every_2_days', 'every_3_days', 'weekly'];

function normalizedChannels(item: any) {
    return Array.from(new Set((item?.channels || [])
        .map((value: unknown) => String(value || '').trim().toLowerCase())
        .map((value: string) => value === 'microsoft_teams' ? 'teams' : value)
        .filter((value: string) => OUTBOUND_CHANNELS.has(value))));
}

function reminderConfig(value: string) {
    try {
        const parsed = JSON.parse(value);
        if (parsed && typeof parsed === 'object' && ['daily', 'weekly'].includes(parsed.freq)) {
            return {
                mode: 'custom',
                freq: parsed.freq,
                interval: parsed.interval ?? 1,
                time: parsed.time || '09:00',
                weekdays: Array.isArray(parsed.weekdays) ? parsed.weekdays : [1, 2, 3, 4, 5],
            };
        }
    } catch {
        // Legacy presets are stored as plain strings.
    }
    return { mode: value, freq: 'daily', interval: 1, time: '09:00', weekdays: [1, 2, 3, 4, 5] };
}

function serializedReminder(config: ReturnType<typeof reminderConfig>) {
    return JSON.stringify({
        freq: config.freq,
        interval: Number(config.interval),
        time: config.time,
        ...(config.freq === 'weekly' ? { weekdays: config.weekdays } : {}),
    });
}

const field = (draft: Record<string, any>, key: string) => draft[key] ?? '';

function draftFor(type: string, resource: any) {
    const config = resource.config || {};
    return {
        title: resource.title || '',
        description: resource.description || '',
        status: resource.status || 'pending',
        priority: resource.priority || 'medium',
        dueDate: toDateTimeInput(resource.due_date),
        targetUserId: resource.supervision_target_user_id || '',
        targetAgentId: resource.supervision_target_agent_id || '',
        supervisionChannel: resource.supervision_channel || '',
        remindSchedule: resource.remind_schedule || '',
        name: resource.name || '',
        instruction: resource.instruction || '',
        cronExpr: resource.cron_expr || config.expr || '',
        timezone: config.timezone || '',
        reason: resource.reason || '',
        enabled: resource.is_enabled !== false,
        maxFires: resource.max_fires ?? '',
        cooldownSeconds: resource.cooldown_seconds ?? 0,
        expiresAt: toDateTimeInput(resource.expires_at),
        onceAt: config.at || '',
        intervalMinutes: config.minutes ?? '',
        pollMethod: config.method || 'GET',
        pollUrl: config.url || '',
        pollHeaders: JSON.stringify(config.headers || {}, null, 2),
        jsonPath: config.json_path || '$',
        fireOn: config.fire_on || 'change',
        matchValue: config.match_value ?? '',
        sourceUserId: config.from_user_id || '',
        sourceAgentId: config.from_agent_id || '',
        webhookMode: config.webhook_mode || 'legacy',
        modelId: resource.model_id || '',
        temperature: resource.temperature ?? null,
        reasoningEffort: resource.reasoning_effort || '',
        soul: resource.soul !== false,
        memory: resource.memory !== false,
        resourceType: type,
    };
}

function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: ReactNode }) {
    return (
        <label className="aware-config-field">
            <span className="aware-config-label">{label}</span>
            {children}
            {hint && <span className="aware-config-hint">{hint}</span>}
            {error && <span className="aware-config-error">{error}</span>}
        </label>
    );
}

export default function AwareConfigDrawer({
    target,
    models,
    inheritedModelId,
    supervisionHumanTargets,
    supervisionAgentTargets,
    canEdit,
    canDelete,
    canReassign,
    currentUserId,
    t,
    onClose,
    onSave,
    onDelete,
    onChooseIdentity,
    confirmExecutionAlignment,
    confirmWebhookModeChange,
    validateTiming,
}: Props) {
    const [tab, setTab] = useState<DrawerTab>('business');
    const [draft, setDraft] = useState<Record<string, any>>({});
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
    const resource = target?.resource;
    const type = target?.type;
    const initialDraft = useMemo<Record<string, any>>(
        () => type && resource ? draftFor(type, resource) : {},
        [type, resource],
    );

    useEffect(() => {
        setDraft(initialDraft);
        setTab('business');
        setError('');
        setFieldErrors({});
    }, [initialDraft]);

    if (!target || !resource || !type) return null;
    const set = (key: string, value: unknown) => {
        setDraft((current) => ({ ...current, [key]: value }));
        setFieldErrors((current) => {
            if (!current[key]) return current;
            const next = { ...current };
            delete next[key];
            return next;
        });
    };
    const creator = resourceCreator(resource);
    const executionName = resource.execution_user_display_name || resource.creator_display_name || resource.creator_username || '—';
    const executionChanged = !!currentUserId && String(resource.execution_user_id || creator.id) !== String(currentUserId);
    const changed = (key: string) => JSON.stringify(draft[key]) !== JSON.stringify(initialDraft[key]);
    const runtimeDirty = ['modelId', 'temperature', 'reasoningEffort', 'soul', 'memory'].some(changed);
    const businessDirty = Object.keys(draft)
        .filter((key) => !['modelId', 'temperature', 'reasoningEffort', 'soul', 'memory', 'resourceType'].includes(key))
        .some(changed);
    const dirty = tab === 'runtime' ? runtimeDirty : tab === 'business' && businessDirty;
    const isSystemTrigger = type === 'trigger' && resource.is_system;
    const modelOptions = [
        { value: '', label: t('agent.aware.workspace.config.inheritModel') },
        ...sortLlmModels(models.filter((model) => model.enabled || model.id === draft.modelId))
            .map((model) => ({ value: String(model.id), label: getLlmModelLabel(model) })),
    ];
    const selectedModel = models.find((model) => String(model.id) === String(draft.modelId || inheritedModelId));
    const currentTargetValue = draft.targetUserId ? `user:${draft.targetUserId}` : draft.targetAgentId ? `agent:${draft.targetAgentId}` : '';
    const targetOptions = [
        ...supervisionHumanTargets
            .filter((item) => item.access_allowed !== false && item.member && normalizedChannels(item).length > 0)
            .map((item) => ({ value: `user:${item.user_id}`, label: `${item.member.name} · ${t('agent.aware.workspace.config.targetKinds.user')}` })),
        ...supervisionAgentTargets
            .filter((item) => item.access_allowed !== false && item.target_agent)
            .map((item) => ({ value: `agent:${item.agent_id}`, label: `${item.target_agent.name} · ${t('agent.aware.workspace.config.targetKinds.agent')}` })),
    ];
    if (currentTargetValue && !targetOptions.some((option) => option.value === currentTargetValue)) {
        targetOptions.unshift({ value: currentTargetValue, label: resource.supervision_target_name || t('agent.aware.workspace.config.targetUnavailable') });
    }
    const selectedHumanTarget = supervisionHumanTargets.find((item) => String(item.user_id) === String(draft.targetUserId));
    const selectedChannels = normalizedChannels(selectedHumanTarget);
    const messageSourceName = draft.sourceUserId
        ? supervisionHumanTargets.find((item) => String(item.user_id) === String(draft.sourceUserId))?.member?.name
        : supervisionAgentTargets.find((item) => String(item.agent_id) === String(draft.sourceAgentId))?.target_agent?.name;
    const channelOptions = [
        ...(selectedChannels.length <= 1 ? [{ value: '', label: t('agent.aware.workspace.config.channelAuto') }] : []),
        ...selectedChannels.map((value) => ({ value, label: t(`agent.aware.workspace.config.channels.${value}`) })),
    ];
    const currentReminder = reminderConfig(String(draft.remindSchedule || ''));
    const reminderModes = [
        ...REMINDER_PRESETS.map((value) => ({ value, label: t(`agent.aware.workspace.config.reminders.${value || 'none'}`) })),
        { value: 'custom', label: t('agent.aware.workspace.config.reminders.custom') },
    ];
    if (currentReminder.mode && !reminderModes.some((option) => option.value === currentReminder.mode)) {
        reminderModes.unshift({ value: currentReminder.mode, label: t('agent.aware.workspace.config.reminders.current') });
    }
    const setCustomReminder = (update: Partial<ReturnType<typeof reminderConfig>>) => {
        set('remindSchedule', serializedReminder({ ...currentReminder, ...update, mode: 'custom' }));
    };

    const validateBusiness = (): Record<string, string> => {
        if (type === 'schedule' && !String(draft.name).trim()) {
            return { name: t('agent.aware.workspace.config.requiredFields') };
        }
        if (type === 'schedule' && !String(draft.cronExpr).trim()) return { cronExpr: t('agent.aware.workspace.config.requiredFields') };
        if (type === 'task' && !String(draft.title).trim()) {
            return { title: t('agent.aware.workspace.config.requiredFields') };
        }
        if (type === 'task' && resource.type === 'supervision') {
            const targetContractChanged = ['targetUserId', 'targetAgentId', 'supervisionChannel'].some(changed)
                || (changed('status') && ['pending', 'doing'].includes(draft.status));
            if (targetContractChanged) {
                const targets = [draft.targetUserId, draft.targetAgentId].filter(Boolean).length;
                if (targets !== 1) return { target: t('agent.aware.workspace.config.oneSupervisionTarget') };
                if (draft.targetUserId && selectedChannels.length === 0) return { target: t('agent.aware.workspace.config.targetRouteRequired') };
                if (draft.targetUserId && selectedChannels.length > 1 && !draft.supervisionChannel) {
                    return { supervisionChannel: t('agent.aware.workspace.config.channelRequired') };
                }
                if (draft.targetUserId && draft.supervisionChannel && !selectedChannels.includes(draft.supervisionChannel)) {
                    return { supervisionChannel: t('agent.aware.workspace.config.channelUnavailable') };
                }
            }
            if (changed('remindSchedule') && currentReminder.mode === 'custom') {
                if (!Number.isInteger(Number(currentReminder.interval)) || Number(currentReminder.interval) < 1) {
                    return { reminderInterval: t('agent.aware.workspace.config.reminderIntervalInvalid') };
                }
                if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(currentReminder.time))) {
                    return { reminderTime: t('agent.aware.workspace.config.reminderTimeInvalid') };
                }
                if (currentReminder.freq === 'weekly' && currentReminder.weekdays.length === 0) {
                    return { reminderWeekdays: t('agent.aware.workspace.config.reminderWeekdaysRequired') };
                }
            }
        }
        if (type !== 'trigger' || isSystemTrigger) return {};
        if (draft.cooldownSeconds === '' || !Number.isInteger(Number(draft.cooldownSeconds)) || Number(draft.cooldownSeconds) < 0) {
            return { cooldownSeconds: t('agent.aware.workspace.config.cooldownRequired') };
        }
        if (resource.max_fires != null && draft.maxFires === '') {
            return { maxFires: t('agent.aware.workspace.config.cannotClearLimit') };
        }
        if (draft.maxFires !== '' && (!Number.isInteger(Number(draft.maxFires)) || Number(draft.maxFires) < 1)) {
            return { maxFires: t('agent.aware.workspace.config.maxFiresInvalid') };
        }
        if (resource.expires_at && !draft.expiresAt) {
            return { expiresAt: t('agent.aware.workspace.config.cannotClearExpiry') };
        }
        if (resource.type === 'cron' && !String(draft.cronExpr).trim()) return { cronExpr: t('agent.aware.workspace.config.requiredFields') };
        if (resource.type === 'once' && !String(draft.onceAt).trim()) return { onceAt: t('agent.aware.workspace.config.requiredFields') };
        if (resource.type === 'interval' && Number(draft.intervalMinutes) < 1) {
            return { intervalMinutes: t('agent.aware.workspace.config.intervalRequired') };
        }
        if (resource.type === 'poll') {
            if (!String(draft.pollMethod).trim()) return { pollMethod: t('agent.aware.workspace.config.requiredFields') };
            if (!String(draft.pollUrl).trim()) return { pollUrl: t('agent.aware.workspace.config.requiredFields') };
            if (Number(draft.intervalMinutes) < 1) return { intervalMinutes: t('agent.aware.workspace.config.intervalRequired') };
            try {
                const headers = JSON.parse(draft.pollHeaders || '{}');
                if (!headers || Array.isArray(headers) || typeof headers !== 'object'
                    || Object.values(headers).some((value) => typeof value !== 'string')) {
                    return { pollHeaders: t('agent.aware.workspace.config.headersInvalid') };
                }
            } catch {
                return { pollHeaders: t('agent.aware.workspace.config.headersInvalid') };
            }
            if (draft.fireOn === 'match' && draft.matchValue === '') {
                return { matchValue: t('agent.aware.workspace.config.matchRequired') };
            }
        }
        return {};
    };

    const businessUpdate = (): Record<string, unknown> => {
        if (type === 'task') {
            const update: Record<string, unknown> = {};
            if (changed('title')) update.title = String(draft.title).trim();
            if (changed('description')) update.description = draft.description || null;
            if (changed('status')) update.status = draft.status;
            if (changed('priority')) update.priority = draft.priority;
            if (changed('dueDate')) update.due_date = fromDateTimeInput(draft.dueDate);
            if (resource.type === 'supervision') {
                if (changed('targetUserId') || changed('targetAgentId')) {
                    update.supervision_target_user_id = draft.targetUserId || null;
                    update.supervision_target_agent_id = draft.targetAgentId || null;
                }
                if (changed('supervisionChannel')) update.supervision_channel = draft.supervisionChannel || null;
                if (changed('remindSchedule')) update.remind_schedule = draft.remindSchedule || null;
            }
            return update;
        }
        if (type === 'schedule') {
            const update: Record<string, unknown> = {};
            if (changed('name')) update.name = String(draft.name).trim();
            if (changed('instruction')) update.instruction = draft.instruction;
            if (changed('cronExpr')) update.cron_expr = String(draft.cronExpr).trim();
            if (changed('enabled')) update.is_enabled = draft.enabled;
            return update;
        }
        if (isSystemTrigger) return changed('enabled') ? { is_enabled: draft.enabled } : {};
        const config = { ...(resource.config || {}) };
        if (resource.type === 'cron') config.expr = String(draft.cronExpr).trim();
        if (resource.type === 'once') config.at = String(draft.onceAt).trim();
        if (resource.type === 'cron' || resource.type === 'once') {
            if (String(draft.timezone).trim()) config.timezone = String(draft.timezone).trim();
            else delete config.timezone;
        }
        if (resource.type === 'interval') config.minutes = Number(draft.intervalMinutes);
        if (resource.type === 'poll') {
            Object.assign(config, {
                method: String(draft.pollMethod).trim().toUpperCase(),
                url: String(draft.pollUrl).trim(),
                headers: JSON.parse(draft.pollHeaders || '{}'),
                json_path: draft.jsonPath || '$',
                fire_on: draft.fireOn,
                interval_min: Number(draft.intervalMinutes),
            });
            if (draft.fireOn === 'match') config.match_value = draft.matchValue;
            else delete config.match_value;
        }
        if (resource.type === 'webhook') config.webhook_mode = draft.webhookMode;
        const configKeys: Record<string, string[]> = {
            cron: ['cronExpr', 'timezone'],
            once: ['onceAt', 'timezone'],
            interval: ['intervalMinutes'],
            poll: ['pollMethod', 'pollUrl', 'pollHeaders', 'jsonPath', 'fireOn', 'matchValue', 'intervalMinutes'],
            webhook: ['webhookMode'],
            on_message: [],
        };
        const update: Record<string, unknown> = {};
        if ((configKeys[resource.type] || []).some(changed)) update.config = config;
        if (changed('reason')) update.reason = draft.reason;
        if (changed('enabled')) update.is_enabled = draft.enabled;
        if (changed('cooldownSeconds')) update.cooldown_seconds = Number(draft.cooldownSeconds);
        if (changed('maxFires') && draft.maxFires !== '') update.max_fires = Number(draft.maxFires);
        if (changed('expiresAt') && draft.expiresAt) update.expires_at = fromDateTimeInput(draft.expiresAt);
        return update;
    };

    const save = async () => {
        const validationErrors = tab === 'business' ? validateBusiness() : {};
        if (Object.keys(validationErrors).length > 0) {
            setFieldErrors(validationErrors);
            return;
        }
        setSaving(true);
        setError('');
        setFieldErrors({});
        try {
            if (tab === 'business') {
                const timezone = String(draft.timezone || '').trim() || undefined;
                if (timezone && (type === 'trigger' && ['cron', 'once'].includes(resource.type))
                    && !(await validateTiming('timezone', timezone, timezone))) {
                    setFieldErrors({ timezone: t('agent.aware.workspace.config.timezoneInvalid') });
                    return;
                }
                const timingKind = type === 'schedule' || (type === 'trigger' && resource.type === 'cron')
                    ? 'cron'
                    : type === 'trigger' && resource.type === 'once' ? 'datetime' : null;
                const timingValue = timingKind === 'cron' ? String(draft.cronExpr) : String(draft.onceAt);
                const timingChanged = type === 'schedule'
                    ? changed('cronExpr')
                    : changed(timingKind === 'cron' ? 'cronExpr' : 'onceAt') || changed('timezone');
                if (timingKind && timingChanged && !(await validateTiming(timingKind, timingValue, timezone))) {
                    setFieldErrors(timingKind === 'cron'
                        ? { cronExpr: t('agent.aware.workspace.config.cronInvalid') }
                        : { onceAt: t('agent.aware.workspace.config.onceInvalid') });
                    return;
                }
            }
            if (tab === 'business' && resource.type === 'webhook' && changed('webhookMode')
                && !(await confirmWebhookModeChange())) return;
            if (executionChanged && !(await confirmExecutionAlignment())) return;
            const update: Record<string, unknown> = tab === 'runtime' ? {} : businessUpdate();
            if (tab === 'runtime') {
                if (changed('modelId')) update.model_id = draft.modelId || null;
                if (changed('temperature')) update.temperature = draft.temperature;
                if (changed('reasoningEffort')) update.reasoning_effort = draft.reasoningEffort || null;
                if (changed('soul')) update.soul = draft.soul;
                if (changed('memory')) update.memory = draft.memory;
            }
            await onSave(type, resource.id, update);
            onClose();
        } catch {
            setError(t('agent.aware.workspace.config.saveFailed'));
        } finally {
            setSaving(false);
        }
    };

    const textInput = (key: string, inputType = 'text', extra: Record<string, unknown> = {}) => (
        <input
            className="form-input"
            type={inputType}
            value={field(draft, key)}
            disabled={!canEdit}
            onChange={(event) => set(key, event.target.value)}
            {...extra}
        />
    );

    const renderTriggerFields = () => {
        if (isSystemTrigger) {
            return <div className="aware-config-note">{t('agent.aware.workspace.config.systemLocked')}</div>;
        }
        return (
            <>
                <Field label={t('agent.aware.workspace.config.reason')}>
                    <textarea className="form-input aware-config-textarea" value={draft.reason} disabled={!canEdit} onChange={(event) => set('reason', event.target.value)} />
                </Field>
                {resource.type === 'cron' && <Field label={t('agent.aware.workspace.config.cron')} hint={t('agent.aware.workspace.config.cronHint')} error={fieldErrors.cronExpr}>{textInput('cronExpr')}</Field>}
                {resource.type === 'once' && <Field label={t('agent.aware.workspace.config.onceAt')} hint={t('agent.aware.workspace.config.onceHint')} error={fieldErrors.onceAt}>{textInput('onceAt')}</Field>}
                {(resource.type === 'cron' || resource.type === 'once') && (
                    <Field label={t('agent.aware.workspace.config.timezone')} hint={t('agent.aware.workspace.config.timezoneHint')} error={fieldErrors.timezone}>{textInput('timezone')}</Field>
                )}
                {resource.type === 'interval' && <Field label={t('agent.aware.workspace.config.intervalMinutes')} error={fieldErrors.intervalMinutes}>{textInput('intervalMinutes', 'number', { min: 1 })}</Field>}
                {resource.type === 'poll' && (
                    <>
                        <div className="aware-config-grid">
                            <Field label={t('agent.aware.workspace.config.method')} error={fieldErrors.pollMethod}>{textInput('pollMethod')}</Field>
                            <Field label={t('agent.aware.workspace.config.url')} error={fieldErrors.pollUrl}>{textInput('pollUrl', 'url')}</Field>
                        </div>
                        <Field label={t('agent.aware.workspace.config.headers')} hint={t('agent.aware.workspace.config.headersHint')} error={fieldErrors.pollHeaders}>
                            <textarea className="form-input aware-config-textarea aware-config-code" value={draft.pollHeaders} disabled={!canEdit} onChange={(event) => set('pollHeaders', event.target.value)} />
                        </Field>
                        <div className="aware-config-grid">
                            <Field label={t('agent.aware.workspace.config.jsonPath')}>{textInput('jsonPath')}</Field>
                            <Field label={t('agent.aware.workspace.config.fireOn')}>
                                <SelectDropdown value={draft.fireOn} options={[{ value: 'change', label: t('agent.aware.workspace.config.fireOnChange') }, { value: 'match', label: t('agent.aware.workspace.config.fireOnMatch') }]} onChange={(value) => set('fireOn', value)} ariaLabel={t('agent.aware.workspace.config.fireOn')} disabled={!canEdit} />
                            </Field>
                        </div>
                        {draft.fireOn === 'match' && <Field label={t('agent.aware.workspace.config.matchValue')} error={fieldErrors.matchValue}>{textInput('matchValue')}</Field>}
                        <Field label={t('agent.aware.workspace.config.pollMinutes')} error={fieldErrors.intervalMinutes}>{textInput('intervalMinutes', 'number', { min: 1 })}</Field>
                    </>
                )}
                {resource.type === 'on_message' && (
                    <Field label={t(draft.sourceUserId ? 'agent.aware.workspace.config.fromUser' : 'agent.aware.workspace.config.fromAgent')}>
                        <div className="aware-config-value">{messageSourceName || t('agent.aware.workspace.config.sourceUnavailable')}</div>
                    </Field>
                )}
                {resource.type === 'webhook' && (
                    <Field label={t('agent.aware.workspace.config.webhookMode')} hint={t('agent.aware.workspace.config.webhookPrivate')}>
                        <SelectDropdown value={draft.webhookMode} options={['legacy', 'queue', 'merge'].map((value) => ({ value, label: t(`agent.aware.workspace.config.webhookModes.${value}`) }))} onChange={(value) => set('webhookMode', value)} ariaLabel={t('agent.aware.workspace.config.webhookMode')} disabled={!canEdit} />
                    </Field>
                )}
                <div className="aware-config-grid">
                    <Field label={t('agent.aware.workspace.config.maxFires')} error={fieldErrors.maxFires}>
                        {textInput('maxFires', 'number', { min: 1 })}
                    </Field>
                    <Field label={t('agent.aware.workspace.config.cooldown')} error={fieldErrors.cooldownSeconds}>{textInput('cooldownSeconds', 'number', { min: 0, required: true })}</Field>
                </div>
                <Field label={t('agent.aware.workspace.config.expiresAt')} error={fieldErrors.expiresAt}>
                    {textInput('expiresAt', 'datetime-local', { step: 0.001 })}
                </Field>
            </>
        );
    };

    const renderBusiness = () => (
        <div className="aware-config-form">
            {(type === 'trigger' || type === 'schedule') && (
                <div className="aware-config-toggle-field">
                    <span className="aware-config-label">{t(type === 'trigger' ? 'agent.aware.workspace.config.triggerEnabled' : 'agent.aware.workspace.config.scheduleEnabled')}</span>
                    <ToggleSwitch checked={draft.enabled} onChange={(value) => set('enabled', value)} disabled={!canEdit} ariaLabel={t('agent.aware.workspace.config.enabledAria')} />
                </div>
            )}
            {type === 'trigger' && renderTriggerFields()}
            {type === 'task' && (
                <>
                    <Field label={t('agent.aware.workspace.config.title')} error={fieldErrors.title}>{textInput('title')}</Field>
                    <Field label={t('agent.aware.workspace.config.description')}>
                        <textarea className="form-input aware-config-textarea" value={draft.description} disabled={!canEdit} onChange={(event) => set('description', event.target.value)} />
                    </Field>
                    <div className="aware-config-grid">
                        <Field label={t('agent.aware.workspace.config.status')}>
                            <SelectDropdown value={draft.status} options={['pending', 'doing', 'done'].map((value) => ({ value, label: t(`agent.aware.workspace.status.${value}`) }))} onChange={(value) => set('status', value)} ariaLabel={t('agent.aware.workspace.config.status')} disabled={!canEdit} />
                        </Field>
                        <Field label={t('agent.aware.workspace.config.priority')}>
                            <SelectDropdown value={draft.priority} options={['low', 'medium', 'high', 'urgent'].map((value) => ({ value, label: t(`agent.aware.workspace.priority.${value}`) }))} onChange={(value) => set('priority', value)} ariaLabel={t('agent.aware.workspace.config.priority')} disabled={!canEdit} />
                        </Field>
                    </div>
                    <Field label={t('agent.aware.workspace.config.dueDate')}>{textInput('dueDate', 'datetime-local', { step: 0.001 })}</Field>
                    {resource.type === 'supervision' && (
                        <>
                            <Field label={t('agent.aware.workspace.config.supervisionTarget')} error={fieldErrors.target}>
                                <SelectDropdown
                                    value={currentTargetValue}
                                    options={targetOptions}
                                    onChange={(value) => {
                                        const [kind, targetId] = value.split(':');
                                        setDraft((current) => ({
                                            ...current,
                                            targetUserId: kind === 'user' ? targetId : '',
                                            targetAgentId: kind === 'agent' ? targetId : '',
                                            supervisionChannel: '',
                                        }));
                                        setFieldErrors((current) => {
                                            const next = { ...current };
                                            delete next.target;
                                            return next;
                                        });
                                    }}
                                    ariaLabel={t('agent.aware.workspace.config.supervisionTarget')}
                                    disabled={!canEdit}
                                />
                            </Field>
                            <div className="aware-config-grid">
                                <Field label={t('agent.aware.workspace.config.channel')} error={fieldErrors.supervisionChannel}>
                                    <SelectDropdown value={draft.supervisionChannel} options={channelOptions} onChange={(value) => set('supervisionChannel', value)} ariaLabel={t('agent.aware.workspace.config.channel')} disabled={!canEdit || !draft.targetUserId} />
                                </Field>
                                <Field label={t('agent.aware.workspace.config.remindSchedule')}>
                                    <SelectDropdown
                                        value={currentReminder.mode}
                                        options={reminderModes}
                                        onChange={(value) => set('remindSchedule', value === 'custom'
                                            ? serializedReminder({ mode: 'custom', freq: 'daily', interval: 1, time: '09:00', weekdays: [1, 2, 3, 4, 5] })
                                            : value)}
                                        ariaLabel={t('agent.aware.workspace.config.remindSchedule')}
                                        disabled={!canEdit}
                                    />
                                </Field>
                            </div>
                            {currentReminder.mode === 'custom' && (
                                <div className="aware-reminder-config">
                                    <div className="aware-config-grid">
                                        <Field label={t('agent.aware.workspace.config.reminderFrequency')}>
                                            <SelectDropdown
                                                value={currentReminder.freq}
                                                options={['daily', 'weekly'].map((value) => ({ value, label: t(`agent.aware.workspace.config.reminderFrequencies.${value}`) }))}
                                                onChange={(value) => setCustomReminder({ freq: value })}
                                                ariaLabel={t('agent.aware.workspace.config.reminderFrequency')}
                                                disabled={!canEdit}
                                            />
                                        </Field>
                                        <Field label={t('agent.aware.workspace.config.reminderInterval')} error={fieldErrors.reminderInterval}>
                                            <input className="form-input" type="number" min={1} value={currentReminder.interval} disabled={!canEdit} onChange={(event) => setCustomReminder({ interval: event.target.value })} />
                                        </Field>
                                        <Field label={t('agent.aware.workspace.config.reminderTime')} error={fieldErrors.reminderTime}>
                                            <input className="form-input" type="time" value={currentReminder.time} disabled={!canEdit} onChange={(event) => setCustomReminder({ time: event.target.value })} />
                                        </Field>
                                    </div>
                                    {currentReminder.freq === 'weekly' && (
                                        <div className="aware-config-field" role="group" aria-label={t('agent.aware.workspace.config.reminderWeekdays')}>
                                            <span className="aware-config-label">{t('agent.aware.workspace.config.reminderWeekdays')}</span>
                                            <div className="aware-weekdays">
                                                {[0, 1, 2, 3, 4, 5, 6].map((day) => (
                                                    <label key={day}>
                                                        <Checkbox
                                                            checked={currentReminder.weekdays.includes(day)}
                                                            disabled={!canEdit}
                                                            onChange={(event) => setCustomReminder({
                                                                weekdays: event.target.checked
                                                                    ? [...currentReminder.weekdays, day].sort()
                                                                    : currentReminder.weekdays.filter((value: number) => value !== day),
                                                            })}
                                                        />
                                                        {t(`agent.aware.workspace.config.weekdays.${day}`)}
                                                    </label>
                                                ))}
                                            </div>
                                            {fieldErrors.reminderWeekdays && <span className="aware-config-error">{fieldErrors.reminderWeekdays}</span>}
                                        </div>
                                    )}
                                </div>
                            )}
                        </>
                    )}
                </>
            )}
            {type === 'schedule' && (
                <>
                    <Field label={t('agent.aware.workspace.config.name')} error={fieldErrors.name}>{textInput('name')}</Field>
                    <Field label={t('agent.aware.workspace.config.instruction')}>
                        <textarea className="form-input aware-config-textarea" value={draft.instruction} disabled={!canEdit} onChange={(event) => set('instruction', event.target.value)} />
                    </Field>
                    <Field label={t('agent.aware.workspace.config.cron')} hint={t('agent.aware.workspace.config.cronHint')} error={fieldErrors.cronExpr}>{textInput('cronExpr')}</Field>
                </>
            )}
        </div>
    );

    const renderRuntime = () => (
        <div className="aware-config-form">
            <Field label={t('agent.aware.workspace.config.model')}>
                <SelectDropdown value={draft.modelId} options={modelOptions} onChange={(value) => set('modelId', value)} ariaLabel={t('agent.aware.workspace.config.model')} disabled={!canEdit} />
            </Field>
            <div className="aware-config-imagination">
                <DivergenceSlider
                    value={draft.temperature}
                    onChange={(value) => set('temperature', value)}
                    label={t('agent.aware.workspace.config.imagination')}
                    inheritedLabel={t('agent.aware.workspace.config.inheritAgent')}
                    lowLabel={t('agent.aware.workspace.config.stable')}
                    middleLabel={t('agent.aware.workspace.config.balanced')}
                    highLabel={t('agent.aware.workspace.config.imaginative')}
                    disabled={!canEdit}
                />
            </div>
            <Field label={t('reasoning.label')}>
                <ReasoningEffortSelect value={draft.reasoningEffort as ReasoningEffortValue} onChange={(value) => set('reasoningEffort', value)} supportedEfforts={selectedModel?.reasoning_efforts} inheritLabel={t('reasoning.inherit')} disabled={!canEdit} />
            </Field>
            <div className="aware-config-capabilities">
                <div className="aware-config-capability-row">
                    <span>{t('agent.aware.workspace.config.useSoul')}</span>
                    <ToggleSwitch checked={draft.soul} disabled={!canEdit} onChange={(value) => set('soul', value)} ariaLabel={t('agent.aware.workspace.config.useSoul')} />
                </div>
                <div className="aware-config-capability-row">
                    <span>{t('agent.aware.workspace.config.useMemory')}</span>
                    <ToggleSwitch checked={draft.memory} disabled={!canEdit} onChange={(value) => set('memory', value)} ariaLabel={t('agent.aware.workspace.config.useMemory')} />
                </div>
            </div>
        </div>
    );

    const resourceTypeLabel = type === 'trigger'
        ? t(`agent.aware.workspace.triggerTypes.${resource.type}`, { defaultValue: t('agent.aware.workspace.config.unknownType') })
        : type === 'task'
            ? t(`agent.aware.workspace.taskTypes.${resource.type}`, { defaultValue: t('agent.aware.workspace.config.unknownType') })
            : t('agent.aware.workspace.schedules');
    const resourceDisplayLabel = type === 'trigger'
        ? resourceTypeLabel
        : resource.title || resource.name || resourceTypeLabel;
    const drawerTabs = (['business', ...(canReassign ? ['identity'] : []), 'runtime']) as DrawerTab[];
    const tabLabel = (item: DrawerTab) => item === 'business'
        ? t(`agent.aware.workspace.config.tabs.business.${type}`)
        : t(`agent.aware.workspace.config.tabs.${item}`);

    return (
        <Drawer open onClose={onClose} ariaLabelledBy="aware-config-title" className="aware-config-drawer">
            <header className="aware-config-header">
                <div>
                    <h2 id="aware-config-title">{t(`agent.aware.workspace.config.titles.${type}`)}</h2>
                    <p>{resourceDisplayLabel}</p>
                </div>
                <Button variant="ghost" onClick={onClose} aria-label={t('common.close')}><IconX size={18} /></Button>
            </header>
            <nav className="aware-config-tabs" aria-label={t('agent.aware.workspace.config.sections')}>
                {drawerTabs.map((item) => (
                    <button key={item} type="button" className={tab === item ? 'active' : ''} onClick={() => { setTab(item); setError(''); }}>
                        {tabLabel(item)}
                    </button>
                ))}
            </nav>
            <div className="aware-config-body">
                {tab === 'business' && renderBusiness()}
                {tab === 'identity' && (
                    <div className="aware-config-form">
                        <div className="aware-config-field">
                            <span className="aware-config-label">{t('agent.aware.executionIdentity.runsAs')}</span>
                            <div className="aware-config-assignee">
                                <strong>{executionName}</strong>
                                <Button variant="secondary" disabled={!canReassign} onClick={() => onChooseIdentity(type, resource)}>{t('agent.aware.workspace.config.changeExecutor')}</Button>
                            </div>
                        </div>
                    </div>
                )}
                {tab === 'runtime' && renderRuntime()}
                {!canEdit && tab !== 'identity' && <div className="aware-config-note">{t('agent.aware.workspace.config.readOnly')}</div>}
                {error && <div className="aware-config-alert" role="alert">{error}</div>}
            </div>
            <footer className="aware-config-footer">
                <span>
                    {canDelete && (type === 'schedule' || (type === 'trigger' && !isSystemTrigger)) && (
                        <Button variant="ghost" className="aware-config-delete" onClick={() => onDelete(type as 'trigger' | 'schedule', resource)}><IconTrash size={15} />{t('common.delete')}</Button>
                    )}
                </span>
                <span className="aware-config-footer-actions">
                    <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
                    {tab !== 'identity' && <Button variant="primary" disabled={!canEdit || !dirty || saving} onClick={save}>{saving ? t('common.saving') : t('common.save')}</Button>}
                </span>
            </footer>
        </Drawer>
    );
}
