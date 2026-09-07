import { useState } from 'react';
import { IconActivity, IconListCheck, IconTargetArrow } from '@tabler/icons-react';

import OrgMemberAccessPicker from '../../../components/OrgMemberAccessPicker';
import { scheduleApi, taskApi, triggerApi } from '../../../services/api';
import { focusKeyFromTrigger, shortIdentity } from '../shared';
import AwareConfigDrawer from './aware/AwareConfigDrawer';
import AwareExecutionsPane from './aware/AwareExecutionsPane';
import AwareFocusPane from './aware/AwareFocusPane';
import AwareResourcesPane from './aware/AwareResourcesPane';
import { resourceCreator } from './aware/awareFormatters';
import type { BackgroundResourceTarget, BackgroundResourceType, ExecutionUserPickerTarget } from './aware/types';
import './aware/AwareWorkspace.css';

export type AwareTabContentProps = {
    embedded?: boolean;
    focusRecords: any[];
    awareTriggers: any[];
    activityLogs: any[];
    executionUsers: any[];
    supervisionHumanTargets: any[];
    supervisionAgentTargets: any[];
    canReassignExecutionUser: boolean;
    reassignExecutionUser: any;
    setExecutionUserPickerTarget: (target: ExecutionUserPickerTarget) => void;
    executionUserPickerTarget: ExecutionUserPickerTarget;
    backgroundTasks: any[];
    schedules: any[];
    llmModels: any[];
    myTenant: any;
    id: string;
    agent: any;
    currentUser: any;
    dialog: any;
    toast: any;
    queryClient: any;
    canManage: boolean;
    reflectionSessions: any[];
    reflectionPage: number;
    setReflectionPage: (value: number | ((page: number) => number)) => void;
    expandedReflection: string | null;
    setExpandedReflection: (id: string | null) => void;
    reflectionMessages: Record<string, any[]>;
    loadReflectionMessages: (conversationId: string, revision?: string) => Promise<any>;
    openSubagentSession: (run: any) => void;
    unavailableAttachmentKeys: Set<string>;
    handleAttachmentDownload: (path: string, name: string) => Promise<void>;
    markAttachmentUnavailable: (key: string) => void;
    setChatImagePreview: (value: any) => void;
    upsertToolCallMessage: (message: any) => void;
    i18n: any;
    t: any;
};

type WorkspaceTab = 'focus' | 'resources' | 'executions';

export default function AwareTabContent({
    embedded = false,
    focusRecords,
    awareTriggers,
    activityLogs,
    executionUsers,
    supervisionHumanTargets,
    supervisionAgentTargets,
    canReassignExecutionUser,
    reassignExecutionUser,
    setExecutionUserPickerTarget,
    executionUserPickerTarget,
    backgroundTasks,
    schedules,
    llmModels,
    myTenant,
    id,
    agent,
    currentUser,
    dialog,
    toast,
    queryClient,
    canManage,
    reflectionSessions,
    reflectionPage,
    setReflectionPage,
    expandedReflection,
    setExpandedReflection,
    reflectionMessages,
    loadReflectionMessages,
    openSubagentSession,
    unavailableAttachmentKeys,
    handleAttachmentDownload,
    markAttachmentUnavailable,
    setChatImagePreview,
    upsertToolCallMessage,
    i18n,
    t,
}: AwareTabContentProps) {
    const [tab, setTab] = useState<WorkspaceTab>('focus');
    const [configTarget, setConfigTarget] = useState<BackgroundResourceTarget>(null);
    const locale = i18n.language;
    const isAgentCreator = String(agent.creator_id || '') === String(currentUser?.id || '');
    const canEditTask = agent.access_level === 'use' || canManage;

    const canEditTarget = (target: BackgroundResourceTarget) => {
        if (!target) return false;
        if (target.type === 'trigger') return canManage;
        if (target.type === 'schedule') return isAgentCreator;
        return canEditTask;
    };

    const updateResource = async (type: BackgroundResourceType, resourceId: string, update: Record<string, unknown>) => {
        if (type === 'trigger') await triggerApi.update(id, resourceId, update);
        else if (type === 'task') await taskApi.update(id, resourceId, update);
        else await scheduleApi.update(id, resourceId, update);
        const key = type === 'trigger' ? 'triggers' : `${type}s`;
        await queryClient.invalidateQueries({ queryKey: [key, id] });
        toast.success(t('agent.aware.workspace.config.saved'));
    };

    const deleteResource = async (type: 'trigger' | 'schedule', resource: any) => {
        const confirmed = await dialog.confirm(
            t(`agent.aware.workspace.config.deleteConfirm.${type}`, { name: resource.name }),
            {
                title: t(`agent.aware.workspace.config.deleteTitle.${type}`),
                danger: true,
                confirmLabel: t('common.delete'),
            },
        );
        if (!confirmed) return;
        try {
            if (type === 'trigger') await triggerApi.delete(id, resource.id);
            else await scheduleApi.delete(id, resource.id);
            await queryClient.invalidateQueries({ queryKey: [type === 'trigger' ? 'triggers' : 'schedules', id] });
            setConfigTarget(null);
            toast.success(t(`agent.aware.workspace.config.deleted.${type}`));
        } catch {
            toast.error(t(`agent.aware.workspace.config.deleteFailed.${type}`));
        }
    };

    const chooseIdentity = (type: BackgroundResourceType, resource: any) => {
        const creator = resourceCreator(resource);
        setConfigTarget(null);
        setExecutionUserPickerTarget({
            resourceType: type,
            resourceId: resource.id,
            executionUserId: String(resource.execution_user_id || creator.id),
            expectedExecutionUserId: resource.execution_user_id ? String(resource.execution_user_id) : null,
        });
    };

    const focusCount = new Set([
        ...focusRecords.map((item) => item.key),
        ...awareTriggers.map((trigger) => trigger.focus_ref || focusKeyFromTrigger(trigger)),
    ]).size;
    const tabs = [
        { id: 'focus' as const, icon: IconTargetArrow, label: t('agent.aware.focus'), count: focusCount },
        { id: 'resources' as const, icon: IconListCheck, label: t('agent.aware.workspace.background'), count: backgroundTasks.length + schedules.length },
        { id: 'executions' as const, icon: IconActivity, label: t('agent.aware.workspace.executions'), count: reflectionSessions.length, hidden: !canManage },
    ];

    return (
        <div className={`aware-workspace${embedded ? ' aware-workspace--embedded' : ''}`}>
            {!embedded && (
                <header className="aware-workspace-heading">
                    <h2>{t('agent.aware.workspace.title')}</h2>
                    <span>{canManage ? t('agent.aware.workspace.manageView') : t('agent.aware.workspace.memberView')}</span>
                </header>
            )}
            <section className="aware-workspace-card">
                <nav className="aware-workspace-tabs" role="tablist" aria-label={t('agent.aware.workspace.navigation')}>
                    {tabs.filter((item) => !item.hidden).map((item) => {
                        const Icon = item.icon;
                        return (
                            <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>
                                <Icon size={15} stroke={1.8} />
                                <span>{item.label}</span>
                                <b>{item.count}</b>
                            </button>
                        );
                    })}
                </nav>
                <div className="aware-workspace-content">
                    {tab === 'focus' && (
                        <AwareFocusPane
                            focusRecords={focusRecords}
                            triggers={awareTriggers}
                            activityLogs={activityLogs}
                            canManage={canManage}
                            locale={locale}
                            t={t}
                            onConfigureTrigger={(resource) => setConfigTarget({ type: 'trigger', resource })}
                        />
                    )}
                    {tab === 'resources' && (
                        <AwareResourcesPane
                            tasks={backgroundTasks}
                            schedules={schedules}
                            executionUsers={executionUsers}
                            canReassign={canReassignExecutionUser}
                            reassignPending={reassignExecutionUser.isPending}
                            canEditTask={canEditTask}
                            canEditSchedule={isAgentCreator}
                            locale={locale}
                            t={t}
                            onConfigure={(type, resource) => setConfigTarget({ type, resource })}
                            setIdentityTarget={setExecutionUserPickerTarget}
                        />
                    )}
                    {tab === 'executions' && canManage && (
                        <AwareExecutionsPane
                            sessions={reflectionSessions}
                            page={reflectionPage}
                            setPage={setReflectionPage}
                            selectedId={expandedReflection}
                            setSelectedId={setExpandedReflection}
                            messages={reflectionMessages}
                            loadMessages={loadReflectionMessages}
                            agentId={id}
                            agentName={agent.name || t('agent.aware.workspace.agentFallback')}
                            locale={locale}
                            t={t}
                            openSubagentSession={openSubagentSession}
                            unavailableAttachmentKeys={unavailableAttachmentKeys}
                            handleAttachmentDownload={handleAttachmentDownload}
                            markAttachmentUnavailable={markAttachmentUnavailable}
                            setChatImagePreview={setChatImagePreview}
                            upsertToolCallMessage={upsertToolCallMessage}
                        />
                    )}
                </div>
            </section>

            <AwareConfigDrawer
                target={configTarget}
                models={llmModels}
                inheritedModelId={agent.primary_model_id || myTenant?.default_model_id || llmModels.find((model: any) => model.enabled)?.id || null}
                supervisionHumanTargets={supervisionHumanTargets}
                supervisionAgentTargets={supervisionAgentTargets}
                canEdit={canEditTarget(configTarget)}
                canDelete={configTarget?.type === 'trigger' ? canManage : isAgentCreator}
                canReassign={canReassignExecutionUser}
                currentUserId={currentUser?.id}
                t={t}
                onClose={() => setConfigTarget(null)}
                onSave={updateResource}
                onDelete={deleteResource}
                onChooseIdentity={chooseIdentity}
                confirmExecutionAlignment={() => dialog.confirm(
                    t('agent.aware.workspace.config.executionAlignmentConfirm'),
                    { title: t('agent.aware.workspace.config.executionAlignmentTitle'), confirmLabel: t('common.save') },
                )}
                confirmWebhookModeChange={() => dialog.confirm(
                    t('agent.aware.workspace.config.webhookModeConfirm'),
                    { title: t('agent.aware.workspace.config.webhookModeTitle'), confirmLabel: t('common.save') },
                )}
                validateTiming={async (kind, value, timezone) => (
                    await triggerApi.validateSchedule(id, { kind, value, timezone })
                ).valid}
            />

            {executionUserPickerTarget && (() => {
                const current = executionUsers.find((user: any) => user.id === executionUserPickerTarget.executionUserId);
                return (
                    <OrgMemberAccessPicker
                        open
                        agentId={id}
                        membersOnly
                        singleSelect
                        users={[{
                            id: executionUserPickerTarget.executionUserId,
                            name: current?.display_name || current?.username || current?.email || shortIdentity(executionUserPickerTarget.executionUserId),
                            email: current?.email || undefined,
                            access_level: 'use',
                        }]}
                        departments={[]}
                        onClose={() => setExecutionUserPickerTarget(null)}
                        onSave={async (users) => {
                            const next = users[0];
                            if (!next || next.id === executionUserPickerTarget.executionUserId) return;
                            await reassignExecutionUser.mutateAsync({
                                resourceType: executionUserPickerTarget.resourceType,
                                resourceId: executionUserPickerTarget.resourceId,
                                executionUserId: next.id,
                                expectedExecutionUserId: executionUserPickerTarget.expectedExecutionUserId,
                            });
                        }}
                    />
                );
            })()}
        </div>
    );
}
