import { useState } from 'react';
import { IconSettings } from '@tabler/icons-react';

import Button from '../../../../components/ui/Button';
import ExecutionIdentityRail from '../ExecutionIdentityRail';
import { resourceCreator } from './awareFormatters';
import type { BackgroundResourceType, ExecutionUserPickerTarget, Translate } from './types';

type Props = {
    tasks: any[];
    schedules: any[];
    executionUsers: any[];
    canReassign: boolean;
    reassignPending: boolean;
    canEditTask: boolean;
    canEditSchedule: boolean;
    locale?: string;
    t: Translate;
    onConfigure: (type: BackgroundResourceType, resource: any) => void;
    setIdentityTarget: (target: ExecutionUserPickerTarget) => void;
};

export default function AwareResourcesPane({
    tasks,
    schedules,
    executionUsers,
    canReassign,
    reassignPending,
    canEditTask,
    canEditSchedule,
    locale,
    t,
    onConfigure,
    setIdentityTarget,
}: Props) {
    const [mode, setMode] = useState<'task' | 'schedule'>('task');
    const resources = mode === 'task' ? tasks : schedules;
    const editable = mode === 'task' ? canEditTask : canEditSchedule;

    const chooseIdentity = (resourceType: 'task' | 'schedule', resource: any) => {
        const creator = resourceCreator(resource);
        setIdentityTarget({
            resourceType,
            resourceId: resource.id,
            executionUserId: String(resource.execution_user_id || creator.id),
            expectedExecutionUserId: resource.execution_user_id ? String(resource.execution_user_id) : null,
        });
    };

    return (
        <section className="aware-pane aware-resources-pane">
            <header className="aware-pane-header">
                <h3>{t('agent.aware.workspace.background')}</h3>
                <div className="aware-segment" role="tablist" aria-label={t('agent.aware.workspace.resourceTypes')}>
                    <button type="button" className={mode === 'task' ? 'active' : ''} onClick={() => setMode('task')}>{t('agent.aware.workspace.tasks')} <span>{tasks.length}</span></button>
                    <button type="button" className={mode === 'schedule' ? 'active' : ''} onClick={() => setMode('schedule')}>{t('agent.aware.workspace.schedules')} <span>{schedules.length}</span></button>
                </div>
            </header>
            {resources.length === 0 ? <div className="aware-empty">{t(mode === 'task' ? 'agent.aware.workspace.noTasks' : 'agent.aware.workspace.noSchedules')}</div> : (
                <div className="aware-resource-table">
                    <div className="aware-resource-table-head">
                        <span>{t('agent.aware.workspace.columns.resource')}</span>
                        <span>{t('agent.aware.workspace.columns.status')}</span>
                        <span>{t('agent.aware.workspace.columns.time')}</span>
                        <span>{t('agent.aware.workspace.columns.identity')}</span>
                        <span>{t('agent.aware.workspace.columns.actions')}</span>
                    </div>
                    {resources.map((resource: any) => {
                        const creator = resourceCreator(resource);
                        const isTask = mode === 'task';
                        const status = isTask ? resource.status : (resource.is_enabled ? 'enabled' : 'disabled');
                        const time = isTask
                            ? (resource.due_date ? t('agent.aware.workspace.due', { value: new Date(resource.due_date).toLocaleString(locale) }) : '—')
                            : (resource.next_run_at ? t('agent.aware.workspace.nextRun', { value: new Date(resource.next_run_at).toLocaleString(locale) }) : '—');
                        return (
                            <article className="aware-resource-row" key={resource.id}>
                                <div className="aware-resource-name">
                                    <strong>{isTask ? resource.title : resource.name}</strong>
                                    <small>{isTask ? t(`agent.aware.workspace.taskTypes.${resource.type}`) : resource.cron_expr}</small>
                                </div>
                                <span className={`aware-status-pill aware-status-pill--${status}`}>{t(`agent.aware.workspace.status.${status}`, { defaultValue: status })}</span>
                                <span className="aware-resource-time">{time}</span>
                                <ExecutionIdentityRail
                                    creatorId={creator.id}
                                    creatorName={creator.name}
                                    executionUserId={resource.execution_user_id}
                                    executionUserName={resource.execution_user_display_name}
                                    users={executionUsers}
                                    canReassign={canReassign}
                                    isPending={reassignPending}
                                    onChoose={() => chooseIdentity(mode, resource)}
                                />
                                <Button variant="secondary" disabled={!editable} title={!editable ? t('agent.aware.workspace.config.readOnly') : undefined} onClick={() => onConfigure(mode, resource)}>
                                    <IconSettings size={14} />{t('agent.aware.workspace.configure')}
                                </Button>
                            </article>
                        );
                    })}
                </div>
            )}
        </section>
    );
}
