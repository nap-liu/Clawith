import { useEffect, useMemo, useState } from 'react';
import { IconChevronDown, IconChevronRight, IconSettings } from '@tabler/icons-react';

import Button from '../../../../components/ui/Button';
import { focusItemFromApi, focusKeyFromTrigger, synthesizeFocusForTrigger } from '../../shared';
import { triggerScheduleLabel } from './awareFormatters';
import type { Translate } from './types';

type Props = {
    focusRecords: any[];
    triggers: any[];
    activityLogs: any[];
    canManage: boolean;
    locale?: string;
    t: Translate;
    onConfigureTrigger: (trigger: any) => void;
};

function statusLabel(enabled: boolean, t: Translate) {
    return enabled
        ? t('agent.aware.workspace.status.active')
        : t('agent.aware.workspace.status.disabled');
}

function triggerTypeLabel(type: string, t: Translate) {
    return t(`agent.aware.workspace.triggerTypes.${type}`, { defaultValue: type });
}

function focusSourceLabel(source: string | undefined, t: Translate) {
    if (!source) return '—';
    return t(`agent.aware.workspace.focusSources.${source}`, { defaultValue: source });
}

export default function AwareFocusPane({
    focusRecords,
    triggers,
    activityLogs,
    canManage,
    locale,
    t,
    onConfigureTrigger,
}: Props) {
    const { items, triggersByFocus, logsByFocus } = useMemo(() => {
        const base = focusRecords.map(focusItemFromApi) as any[];
        const names = new Set(base.map((item) => item.name));
        const triggerGroups: Record<string, any[]> = {};
        for (const trigger of triggers) {
            const key = trigger.focus_ref && names.has(trigger.focus_ref)
                ? trigger.focus_ref
                : focusKeyFromTrigger(trigger);
            if (!triggerGroups[key]) triggerGroups[key] = [];
            triggerGroups[key].push(trigger);
            if (!names.has(key)) {
                const synthetic = synthesizeFocusForTrigger(trigger) as any;
                base.push(synthetic);
                names.add(key);
            }
        }
        const nameByTrigger = new Map(triggers.map((trigger) => [
            trigger.name,
            trigger.focus_ref || focusKeyFromTrigger(trigger),
        ]));
        const logGroups: Record<string, any[]> = {};
        for (const log of activityLogs) {
            if (!String(log.action_type || '').startsWith('trigger_') && !String(log.summary || '').includes('trigger')) continue;
            const match = [...nameByTrigger.entries()].find(([name]) => log.summary?.includes(name) || log.detail?.tool === name);
            if (!match) continue;
            if (!logGroups[match[1]]) logGroups[match[1]] = [];
            logGroups[match[1]].push(log);
        }
        return { items: base, triggersByFocus: triggerGroups, logsByFocus: logGroups };
    }, [activityLogs, focusRecords, triggers]);
    const active = items.filter((item) => !item.done && !item.system);
    const system = items.filter((item) => !item.done && item.system);
    const completed = items.filter((item) => item.done);
    const firstVisible = active[0] || system[0] || completed[0];
    const [selectedId, setSelectedId] = useState<string>('');
    const [completedOpen, setCompletedOpen] = useState(false);

    useEffect(() => {
        if (!items.some((item) => item.id === selectedId)) setSelectedId(firstVisible?.id || '');
    }, [firstVisible?.id, items, selectedId]);

    const selected = items.find((item) => item.id === selectedId) || firstVisible;
    const selectedTriggers = selected ? triggersByFocus[selected.name] || [] : [];
    const selectedLogs = selected ? logsByFocus[selected.name] || [] : [];

    const row = (item: any, kind: 'active' | 'system' | 'done') => {
        const itemTriggers = triggersByFocus[item.name] || [];
        return (
            <button
                key={item.id}
                type="button"
                className={`aware-focus-row${selected?.id === item.id ? ' active' : ''}${kind === 'done' ? ' done' : ''}`}
                onClick={() => setSelectedId(item.id)}
            >
                <span className={`aware-status-dot aware-status-dot--${kind}`} aria-hidden="true" />
                <span className="aware-focus-row-copy">
                    <strong>{item.title || item.name}</strong>
                    {item.title && <code>{item.name}</code>}
                    {item.description && <small>{item.description}</small>}
                </span>
                <span className="aware-count-pill">{t('agent.aware.workspace.triggerCount', { count: itemTriggers.length })}</span>
                <IconChevronRight size={15} aria-hidden="true" />
            </button>
        );
    };

    return (
        <div className="aware-split aware-focus-workspace">
            <section className="aware-pane aware-focus-list" aria-label={t('agent.aware.workspace.focusList')}>
                <header className="aware-pane-header">
                    <h3>{t('agent.aware.focus')}</h3>
                    <span className="aware-pane-summary">{t('agent.aware.workspace.focusSummary', { active: active.length + system.length, completed: completed.length })}</span>
                </header>
                <div className="aware-section-label">{t('agent.aware.workspace.groups.active')}</div>
                {active.map((item) => row(item, 'active'))}
                {system.length > 0 && <div className="aware-section-label">{t('agent.aware.workspace.groups.system')}</div>}
                {system.map((item) => row(item, 'system'))}
                {completed.length > 0 && (
                    <>
                        <button className="aware-completed-toggle" type="button" onClick={() => setCompletedOpen((value) => !value)} aria-expanded={completedOpen}>
                            <span>{t('agent.aware.workspace.groups.completed', { count: completed.length })}</span>
                            <IconChevronDown size={15} className={completedOpen ? 'open' : ''} />
                        </button>
                        {completedOpen && completed.map((item) => row(item, 'done'))}
                    </>
                )}
                {items.length === 0 && <div className="aware-empty">{t('agent.aware.focusEmpty')}</div>}
            </section>
            <section className="aware-pane aware-focus-detail">
                {!selected ? <div className="aware-empty">{t('agent.aware.focusEmpty')}</div> : (
                    <>
                        <header className="aware-detail-header">
                            <div>
                                <div className="aware-detail-title-line">
                                    <h3>{selected.title || selected.name}</h3>
                                    <span className={`aware-status-pill${selected.done ? ' success' : ''}`}>{selected.done ? t('agent.aware.completed') : t('agent.aware.inProgress')}</span>
                                </div>
                                {selected.description && <p>{selected.description}</p>}
                                <div className="aware-detail-meta">
                                    <span>{selected.system ? t('agent.aware.workspace.systemFocus') : t('agent.aware.workspace.normalFocus')}</span>
                                    <span>{t('agent.aware.workspace.source', { value: focusSourceLabel(selected.source, t) })}</span>
                                </div>
                            </div>
                        </header>
                        <div className="aware-detail-section">
                            <div className="aware-detail-section-head"><h4>{t('agent.aware.workspace.linkedTriggers')}</h4><span>{selectedTriggers.length}</span></div>
                            <div className="aware-trigger-list">
                                {selectedTriggers.map((trigger) => (
                                    <article className={`aware-trigger-row${trigger.is_enabled ? '' : ' disabled'}`} key={trigger.id}>
                                        <span className={`aware-status-dot aware-status-dot--${trigger.is_system ? 'system' : 'active'}`} />
                                        <div className="aware-trigger-copy">
                                            <div className="aware-trigger-title-line"><strong>{triggerScheduleLabel(trigger, t, locale)}</strong><code>{triggerTypeLabel(trigger.type, t)}</code></div>
                                            {trigger.reason && <p>{trigger.reason}</p>}
                                            <small>{statusLabel(trigger.is_enabled, t)} · {t('agent.aware.fired', { count: trigger.fire_count })}</small>
                                        </div>
                                        {canManage && <Button variant="secondary" onClick={() => onConfigureTrigger(trigger)}><IconSettings size={14} />{t('agent.aware.workspace.configure')}</Button>}
                                    </article>
                                ))}
                                {selectedTriggers.length === 0 && <div className="aware-empty aware-empty--compact">{t('agent.aware.noTriggers')}</div>}
                            </div>
                        </div>
                        <div className="aware-detail-section">
                            <div className="aware-detail-section-head"><h4>{t('agent.aware.workspace.activity')}</h4></div>
                            <div className="aware-activity-list">
                                {selectedLogs.slice(0, 10).map((log) => (
                                    <div className="aware-activity-row" key={log.id}>
                                        <time>{new Date(log.created_at).toLocaleString(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time>
                                        <span><b>{t(`agent.aware.workspace.activityTypes.${log.action_type}`, { defaultValue: log.action_type })}</b>{log.summary ? ` · ${log.summary}` : ''}</span>
                                    </div>
                                ))}
                                {selectedLogs.length === 0 && <div className="aware-empty aware-empty--compact">{t('agent.aware.workspace.noActivity')}</div>}
                            </div>
                        </div>
                    </>
                )}
            </section>
        </div>
    );
}
