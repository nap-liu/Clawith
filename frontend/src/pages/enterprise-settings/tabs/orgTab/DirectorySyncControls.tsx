import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import SelectDropdown from '../../../../components/SelectDropdown';
import ToggleSwitch from '../../../../components/ToggleSwitch';
import Button from '../../../../components/ui/Button';
import TextInput from '../../../../components/ui/TextInput';
import IdentityConflictReviewModal from './IdentityConflictReviewModal';

const SYNC_INTERVAL_UNITS = ['hour', 'day', 'week', 'month'] as const;
type SyncIntervalUnit = typeof SYNC_INTERVAL_UNITS[number];

function normalizeIntervalUnit(unit?: string | null): SyncIntervalUnit {
    return SYNC_INTERVAL_UNITS.includes(unit as SyncIntervalUnit)
        ? unit as SyncIntervalUnit
        : 'day';
}

type DirectorySyncRun = {
    status?: string;
    stage?: string;
    progress_percent?: number | null;
    processed_items?: number;
    total_items?: number | null;
    stats?: Record<string, number>;
    error_summary?: string | null;
    error?: string | null;
};

type DirectoryProvider = {
    id?: string;
    is_active?: boolean;
    sync_enabled?: boolean;
    sync_interval_value?: number | null;
    sync_interval_unit?: string | null;
};

export default function DirectorySyncControls({
    provider,
    syncing,
    result,
    onTrigger,
    onUpdateSchedule,
}: {
    provider: DirectoryProvider;
    syncing: boolean;
    result: DirectorySyncRun | null;
    onTrigger: () => void;
    onUpdateSchedule: (value: number, unit: string, enabled: boolean) => Promise<void>;
}) {
    const { t } = useTranslation();
    const [value, setValue] = useState(provider.sync_interval_value || 1);
    const [unit, setUnit] = useState<SyncIntervalUnit>(normalizeIntervalUnit(provider.sync_interval_unit));
    const [saving, setSaving] = useState(false);
    const [scheduleError, setScheduleError] = useState(false);
    const [conflictsOpen, setConflictsOpen] = useState(false);

    useEffect(() => {
        setValue(provider.sync_interval_value || 1);
        setUnit(normalizeIntervalUnit(provider.sync_interval_unit));
    }, [provider.sync_interval_value, provider.sync_interval_unit]);

    const update = async (enabled: boolean, nextValue = value, nextUnit = unit) => {
        setSaving(true);
        setScheduleError(false);
        try {
            await onUpdateSchedule(nextValue, nextUnit, enabled);
        } catch {
            setScheduleError(true);
        } finally {
            setSaving(false);
        }
    };

    const active = result && ['pending', 'running'].includes(result.status || '');
    const stats = result?.stats || {};
    const failed = result && ['failed', 'partial_failed'].includes(result.status || '');
    const warning = result && ['needs_review', 'cancelled'].includes(result.status || '');
    const succeeded = result?.status === 'succeeded';
    const intervalOptions = SYNC_INTERVAL_UNITS.map((intervalUnit) => ({
        value: intervalUnit,
        label: t(`enterprise.org.directorySync.units.${intervalUnit}`),
    }));
    const stageLabels: Record<string, string> = {
        queued: t('enterprise.org.directorySync.stages.queued'),
        fetching: t('enterprise.org.directorySync.stages.fetching'),
        fetching_accounts: t('enterprise.org.directorySync.stages.fetchingAccounts'),
        validating: t('enterprise.org.directorySync.stages.validating'),
        applying_groups: t('enterprise.org.directorySync.stages.applyingGroups'),
        applying_accounts: t('enterprise.org.directorySync.stages.applyingAccounts'),
        reconciling: t('enterprise.org.directorySync.stages.reconciling'),
    };
    const stageLabel = stageLabels[result?.stage || '']
        || t('enterprise.org.directorySync.stages.processing');
    const resultBackground = active
        ? 'rgba(59,130,246,0.1)'
        : failed || result?.error
            ? 'rgba(255,100,0,0.1)'
            : warning
                ? 'rgba(245,158,11,0.1)'
                : succeeded
                    ? 'rgba(0,200,0,0.1)'
                    : 'var(--bg-secondary)';
    return (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '10px', maxWidth: '100%' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', flexWrap: 'wrap', gap: '8px' }}>
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
                    <ToggleSwitch
                        checked={!!provider.sync_enabled}
                        disabled={saving || provider.is_active === false}
                        onChange={(checked) => void update(checked)}
                        ariaLabel={t('enterprise.org.directorySync.autoSync')}
                    />
                    <span>{t('enterprise.org.directorySync.autoSync')}</span>
                </div>
                <TextInput
                    type="number"
                    min={1}
                    max={365}
                    value={value}
                    disabled={saving || provider.is_active === false}
                    onChange={(event) => setValue(Number(event.target.value) || 1)}
                    onBlur={() => void update(!!provider.sync_enabled)}
                    aria-label={t('enterprise.org.directorySync.intervalValueAria')}
                    style={{ width: '72px' }}
                />
                <SelectDropdown
                    value={unit}
                    options={intervalOptions}
                    disabled={saving || provider.is_active === false}
                    onChange={(nextUnit) => {
                        setUnit(nextUnit);
                        void update(!!provider.sync_enabled, value, nextUnit);
                    }}
                    ariaLabel={t('enterprise.org.directorySync.intervalUnitAria')}
                    style={{ minWidth: '112px' }}
                />
                <Button
                    type="button"
                    variant="secondary"
                    className="btn-sm"
                    onClick={onTrigger}
                    disabled={syncing || provider.is_active === false}
                >
                    {syncing
                        ? t('enterprise.org.syncing')
                        : provider.is_active === false
                            ? t('enterprise.org.directorySync.providerInactive')
                            : t('enterprise.org.syncNow')}
                </Button>
            </div>
            {scheduleError && (
                <div style={{ color: 'var(--error)', fontSize: '11px' }}>
                    {t('enterprise.org.directorySync.scheduleSaveFailed')}
                </div>
            )}
            {result && (
                <div style={{ padding: '6px 10px', borderRadius: '4px', fontSize: '11px', background: resultBackground, textAlign: 'left' }}>
                    {active
                        ? t('enterprise.org.directorySync.progress', {
                            stage: stageLabel,
                            percent: result.progress_percent ?? '?',
                            processed: result.processed_items || 0,
                            total: result.total_items ?? '?',
                        })
                        : result.status === 'needs_review'
                            ? t('enterprise.org.directorySync.needsReview', {
                                reason: Number(stats.identity_conflicts) > 0
                                    ? t('enterprise.org.directorySync.identityMatchingConflicts', {
                                        count: Number(stats.identity_conflicts),
                                    })
                                    : t('enterprise.org.directorySync.identityMatchingConflict'),
                            })
                            : result.status === 'cancelled'
                                ? t('enterprise.org.directorySync.cancelled')
                                : failed || result.error_summary || result.error
                                    ? t(
                                        result.status === 'partial_failed'
                                            ? 'enterprise.org.directorySync.completedWithErrors'
                                            : 'enterprise.org.directorySync.syncFailed',
                                        {
                                            reason: result.error_summary
                                                || result.error
                                                || t('enterprise.org.directorySync.unknownError'),
                                        },
                                    )
                                    : result.status === 'succeeded'
                                        ? t('enterprise.org.syncComplete', {
                                            departments: stats.departments || 0,
                                            members: stats.members || 0,
                                        })
                                        : t('enterprise.org.directorySync.unknownStatus')}
                </div>
            )}
            {provider.id && Number(stats.identity_conflicts) > 0 && (
                <Button
                    type="button"
                    variant="ghost"
                    className="btn-sm"
                    onClick={() => setConflictsOpen(true)}
                >
                    {t('enterprise.org.identityConflicts.viewDetails')}
                </Button>
            )}
            {provider.id && (
                <IdentityConflictReviewModal
                    open={conflictsOpen}
                    providerId={provider.id}
                    onClose={() => setConflictsOpen(false)}
                />
            )}
        </div>
    );
}
