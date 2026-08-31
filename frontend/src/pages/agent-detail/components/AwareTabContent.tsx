import React from 'react';
import OrgMemberAccessPicker from '../../../components/OrgMemberAccessPicker';
import ConversationTimeline from '../../../features/conversation/web/ConversationTimeline';
import type { FocusItem, ExecutionUserOption } from '../shared';
import {
    focusItemFromApi,
    focusKeyFromTrigger,
    formatReflectionTitle,
    shortIdentity,
    synthesizeFocusForTrigger,
} from '../shared';
import ExecutionIdentityRail from './ExecutionIdentityRail';

type ExecutionUserPickerTarget = {
    resourceType: 'trigger' | 'task' | 'schedule';
    resourceId: string;
    executionUserId: string;
    expectedExecutionUserId: string | null;
} | null;

type Props = {
    focusRecords: any[];
    awareTriggers: any[];
    activityLogs: any[];
    i18n: any;
    t: any;
    expandedFocusIds: Set<string>;
    toggleExpandedFocus: (id: string) => void;
    showAllFocus: boolean;
    setShowAllFocus: React.Dispatch<React.SetStateAction<boolean>>;
    showCompletedFocus: boolean;
    setShowCompletedFocus: React.Dispatch<React.SetStateAction<boolean>>;
    executionUsers: ExecutionUserOption[];
    canReassignExecutionUser: boolean;
    reassignExecutionUser: any;
    setExecutionUserPickerTarget: React.Dispatch<React.SetStateAction<ExecutionUserPickerTarget>>;
    executionUserPickerTarget: ExecutionUserPickerTarget;
    backgroundTasks: any[];
    schedules: any[];
    id: string;
    agent: any;
    dialog: any;
    canManage: boolean;
    triggerApi: any;
    refetchTriggers: () => void;
    reflectionSessions: any[];
    reflectionPage: number;
    setReflectionPage: React.Dispatch<React.SetStateAction<number>>;
    expandedReflection: string | null;
    setExpandedReflection: React.Dispatch<React.SetStateAction<string | null>>;
    reflectionMessages: Record<string, any[]>;
    loadReflectionMessages: (conversationId: string) => Promise<any>;
    openSubagentSession: (run: any) => void;
    unavailableAttachmentKeys: Set<string>;
    handleAttachmentDownload: (path: string, name: string) => Promise<void>;
    markAttachmentUnavailable: (key: string) => void;
    setChatImagePreview: React.Dispatch<React.SetStateAction<any>>;
    upsertToolCallMessage: (message: any) => void;
};

const REFLECTIONS_PAGE_SIZE = 10;
const SECTION_PAGE_SIZE = 5;

export default function AwareTabContent({
    focusRecords,
    awareTriggers,
    activityLogs,
    i18n,
    t,
    expandedFocusIds,
    toggleExpandedFocus,
    showAllFocus,
    setShowAllFocus,
    showCompletedFocus,
    setShowCompletedFocus,
    executionUsers,
    canReassignExecutionUser,
    reassignExecutionUser,
    setExecutionUserPickerTarget,
    executionUserPickerTarget,
    backgroundTasks,
    schedules,
    id,
    agent,
    dialog,
    canManage,
    triggerApi,
    refetchTriggers,
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
}: Props) {
    const focusItems = focusRecords.map(focusItemFromApi);
    const isZh = i18n.language?.startsWith('zh');

    const triggerToHuman = (trig: any): string => {
        const isZh = i18n.language?.startsWith('zh');
        if (trig.type === 'cron' && trig.config?.expr) {
            const expr = trig.config.expr;
            const parts = expr.split(' ');
            if (parts.length >= 5) {
                const [min, hour, dom, , dow] = parts;
                const timeStr = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
                const dayNames = isZh
                    ? ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
                    : ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
                if (dom !== '*' && dow === '*' && min !== '*' && hour !== '*') {
                    const days = dom.split(',').join(isZh ? '、' : ', ');
                    return isZh ? `每月 ${days} 日 ${timeStr}` : `Every month on day ${days} at ${timeStr}`;
                }
                if (dow === '*' && min !== '*' && hour !== '*') return isZh ? `每天 ${timeStr}` : `Every day at ${timeStr}`;
                if (dow === '1-5' && min !== '*' && hour !== '*') return isZh ? `工作日 ${timeStr}` : `Weekdays at ${timeStr}`;
                if ((dow === '0' || dow === '7') && min !== '*' && hour !== '*') return isZh ? `每周日 ${timeStr}` : `Sundays at ${timeStr}`;
                if (/^[1-6]$/.test(dow) && min !== '*' && hour !== '*') return isZh ? `每${dayNames[Number(dow)]} ${timeStr}` : `${dayNames[Number(dow)]}s at ${timeStr}`;
                if (hour === '*' && min === '0') {
                    if (dow === '1-5') return isZh ? '工作日每小时' : 'Every hour on weekdays';
                    return isZh ? '每小时' : 'Every hour';
                }
                if (hour === '*' && min !== '*') return isZh ? `每小时第 ${min.padStart(2, '0')} 分钟` : `Every hour at :${min.padStart(2, '0')}`;
            }
            return isZh ? `Cron：${expr}` : `Cron: ${expr}`;
        }
        if (trig.type === 'once' && trig.config?.at) {
            try {
                return isZh
                    ? `一次性：${new Date(trig.config.at).toLocaleString()}`
                    : `Once at ${new Date(trig.config.at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
            } catch {
                return isZh ? `一次性：${trig.config.at}` : `Once at ${trig.config.at}`;
            }
        }
        if (trig.type === 'interval' && trig.config?.minutes) {
            const m = trig.config.minutes;
            return isZh ? `每 ${m >= 60 ? `${m / 60} 小时` : `${m} 分钟`}` : (m >= 60 ? `Every ${m / 60}h` : `Every ${m} min`);
        }
        if (trig.type === 'poll') return `${isZh ? '轮询' : 'Poll'}: ${trig.config?.url?.substring(0, 40) || 'URL'}`;
        if (trig.type === 'on_message') {
            const sender = trig.config?.from_agent_id || trig.config?.from_user_id || (isZh ? '未知对象' : 'unknown');
            return isZh ? `收到 ${sender} 的消息时` : `On message from ${sender}`;
        }
        if (trig.type === 'webhook') {
            return `Webhook${trig.config?.token ? ` (${trig.config.token.substring(0, 6)}...)` : ''}`;
        }
        return trig.type;
    };

    const triggerReasonText = (trig: any): string | null => {
        if (!i18n.language?.startsWith('zh')) return trig.reason || null;
        if (trig.name === 'daily_okr_report') return '系统触发器：如果启用了日报，收集成员进展、更新滞后的 KR，并生成日报。';
        if (trig.name === 'weekly_okr_report') return '系统触发器：如果启用了周报，收集成员进展、更新滞后的 KR，并生成周报。';
        if (trig.name === 'biweekly_okr_checkin') return '系统触发器：每月 1 日和 15 日进行 OKR 例行检查。';
        if (trig.name === 'monthly_okr_report') return '系统触发器：每月 1 日生成 OKR 月度进展汇报。';
        return trig.reason || null;
    };

    const triggersByFocus: Record<string, any[]> = {};
    const focusNames = new Set(focusItems.map((item) => item.name));
    for (const trig of awareTriggers) {
        if (trig.focus_ref && focusNames.has(trig.focus_ref)) {
            if (!triggersByFocus[trig.focus_ref]) triggersByFocus[trig.focus_ref] = [];
            triggersByFocus[trig.focus_ref].push(trig);
        } else {
            const synthetic = synthesizeFocusForTrigger(trig);
            if (!triggersByFocus[synthetic.name]) triggersByFocus[synthetic.name] = [];
            triggersByFocus[synthetic.name].push(trig);
        }
    }
    const displayFocusItems = focusItems;

    const triggerLogsByFocus: Record<string, any[]> = {};
    const triggerNameToFocus: Record<string, string> = {};
    for (const trig of awareTriggers) {
        triggerNameToFocus[trig.name] = trig.focus_ref || focusKeyFromTrigger(trig);
    }
    const triggerRelatedLogs = activityLogs.filter((log: any) =>
        log.action_type === 'trigger_fired' || log.action_type === 'trigger_created'
        || log.action_type === 'trigger_updated' || log.action_type === 'trigger_cancelled'
        || log.summary?.includes('trigger'),
    );
    for (const log of triggerRelatedLogs) {
        let matched = false;
        for (const [trigName, focusName] of Object.entries(triggerNameToFocus)) {
            if (log.summary?.includes(trigName) || log.detail?.tool === trigName) {
                if (!triggerLogsByFocus[focusName]) triggerLogsByFocus[focusName] = [];
                triggerLogsByFocus[focusName].push(log);
                matched = true;
                break;
            }
        }
        if (!matched) {
            if (!triggerLogsByFocus.__unmatched__) triggerLogsByFocus.__unmatched__ = [];
            triggerLogsByFocus.__unmatched__.push(log);
        }
    }

    const hasFocusItems = displayFocusItems.length > 0;
    const activeFocusItems = displayFocusItems.filter((f) => !f.done && !f.system);
    const systemFocusItems = displayFocusItems.filter((f) => !f.done && f.system);
    const completedFocusItems = displayFocusItems.filter((f) => f.done);
    const visibleActiveFocus = showAllFocus ? activeFocusItems : activeFocusItems.slice(0, SECTION_PAGE_SIZE);
    const hiddenActiveCount = activeFocusItems.length - visibleActiveFocus.length;
    const renderTriggerDot = (done: boolean, label: string) => (
        <span className={`aware-side-status-dot ${done ? 'done' : 'active'}`} aria-label={label} />
    );

    const renderFocusItem = (item: FocusItem) => {
        const isExpanded = expandedFocusIds.has(item.id);
        const itemTriggers = triggersByFocus[item.name] || [];
        const itemLogs = triggerLogsByFocus[item.name] || [];
        const hasTitle = !!item.title;
        const displayTitle = hasTitle ? item.title : item.name;
        const displaySubtitle = hasTitle ? item.name : null;
        const displayDescription = item.description;

        return (
            <div key={item.id} style={{ borderRadius: '8px', border: '1px solid var(--border-subtle)', overflow: 'hidden', marginBottom: '6px', background: 'var(--bg-primary)', opacity: item.done ? 0.74 : 1 }}>
                <div
                    onClick={() => toggleExpandedFocus(item.id)}
                    style={{ padding: '12px 16px', display: 'flex', alignItems: 'flex-start', gap: '12px', cursor: 'pointer', transition: 'background 0.15s' }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-secondary)')}
                    onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                    <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '13px', fontWeight: 500, lineHeight: '20px', textDecoration: item.done ? 'line-through' : 'none', color: item.done ? 'var(--text-tertiary)' : 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                            <span>{displayTitle}</span>
                            {item.done && <span className="aware-side-focus-badge done">{t('agent.aware.completed')}</span>}
                        </div>
                        {displaySubtitle && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'monospace', marginTop: '2px' }}>{displaySubtitle}</div>}
                        {displayDescription && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', whiteSpace: 'pre-wrap' }}>{displayDescription}</div>}
                    </div>
                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', padding: '2px 8px', borderRadius: '10px', background: 'var(--bg-secondary)', whiteSpace: 'nowrap' }}>
                        {i18n.language?.startsWith('zh') ? `${itemTriggers.length} 个触发器` : `${itemTriggers.length} trigger${itemTriggers.length > 1 ? 's' : ''}`}
                    </span>
                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s', marginTop: '4px' }}>&#9654;</span>
                </div>
                {isExpanded && (
                    <div style={{ padding: '0 16px 12px 36px', borderTop: '1px solid var(--border-subtle)' }}>
                        {itemTriggers.length > 0 && (
                            <div style={{ marginTop: '12px' }}>
                                {itemTriggers.map((trig: any) => (
                                    <div key={trig.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 12px', marginBottom: '4px', borderRadius: '6px', background: 'var(--bg-secondary)', opacity: trig.is_enabled ? 1 : 0.5 }}>
                                        {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                        <div style={{ flex: 1 }}>
                                            <div style={{ fontSize: '12px', fontWeight: 500, color: 'var(--text-primary)' }}>{triggerToHuman(trig)}</div>
                                            {triggerReasonText(trig) && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{triggerReasonText(trig)}</div>}
                                            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '2px', fontFamily: 'monospace' }}>{trig.type === 'cron' ? trig.config?.expr : ''}{' '}</div>
                                        </div>
                                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>{t('agent.aware.fired', { count: trig.fire_count })}</span>
                                        <span style={{ fontSize: '10px', color: trig.is_enabled ? 'var(--accent-primary)' : 'var(--success, #10b981)' }}>{trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed')}</span>
                                        <ExecutionIdentityRail
                                            creatorId={trig.created_by_user_id}
                                            creatorName={trig.creator_display_name}
                                            executionUserId={trig.execution_user_id}
                                            executionUserName={trig.execution_user_display_name}
                                            users={executionUsers}
                                            canReassign={canReassignExecutionUser}
                                            isPending={reassignExecutionUser.isPending}
                                            onChoose={() => setExecutionUserPickerTarget({
                                                resourceType: 'trigger',
                                                resourceId: trig.id,
                                                executionUserId: trig.execution_user_id || trig.created_by_user_id,
                                                expectedExecutionUserId: trig.execution_user_id || null,
                                            })}
                                        />
                                        <div style={{ display: 'flex', gap: '4px' }}>
                                            {canManage && !trig.is_system && (
                                                <button
                                                    className="btn btn-ghost"
                                                    style={{ padding: '2px 6px', fontSize: '11px', color: 'var(--error)' }}
                                                    onClick={async (e) => {
                                                        e.stopPropagation();
                                                        if (!canManage) return;
                                                        const ok = await dialog.confirm(t('agent.aware.deleteTriggerConfirm', { name: trig.name }), { title: '删除触发器', danger: true, confirmLabel: '删除' });
                                                        if (ok) {
                                                            await triggerApi.delete(id, trig.id);
                                                            refetchTriggers();
                                                        }
                                                    }}
                                                >
                                                    {t('common.delete', 'Delete')}
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                        {itemLogs.length > 0 && (
                            <div style={{ marginTop: '12px' }}>
                                <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.aware.reflections')}</div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                    {itemLogs.slice(0, 10).map((log: any) => (
                                        <div key={log.id} style={{ padding: '6px 12px', borderRadius: '6px', background: 'var(--bg-secondary)', borderLeft: '2px solid var(--border-subtle)' }}>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '2px' }}>
                                                <span style={{ fontSize: '10px', padding: '1px 5px', borderRadius: '3px', background: log.action_type === 'trigger_fired' ? 'rgba(var(--accent-primary-rgb, 99,102,241), 0.1)' : 'var(--bg-tertiary, #e5e7eb)', color: log.action_type === 'trigger_fired' ? 'var(--accent-primary)' : 'var(--text-tertiary)', fontWeight: 500 }}>{log.action_type?.replace('trigger_', '')}</span>
                                                <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{new Date(log.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
                                            </div>
                                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{log.summary}</div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                        {itemTriggers.length === 0 && itemLogs.length === 0 && <div style={{ padding: '12px 0', fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.noTriggers')}</div>}
                    </div>
                )}
            </div>
        );
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                    <div>
                        <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.aware.focus')}</h4>
                        <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.focusDesc')}</span>
                    </div>
                    {hasFocusItems && <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{i18n.language?.startsWith('zh') ? `${activeFocusItems.length} 个进行中${systemFocusItems.length > 0 ? ` · ${systemFocusItems.length} 个系统` : ''}${completedFocusItems.length > 0 ? ` · ${completedFocusItems.length} 个已完成` : ''}` : `${activeFocusItems.length} active${systemFocusItems.length > 0 ? ` · ${systemFocusItems.length} system` : ''}${completedFocusItems.length > 0 ? ` · ${completedFocusItems.length} done` : ''}`}</span>}
                </div>
                {visibleActiveFocus.map(renderFocusItem)}
                {hiddenActiveCount > 0 && <button onClick={() => setShowAllFocus(true)} className="btn btn-ghost" style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }}>{t('agent.aware.showMore', { count: hiddenActiveCount })}</button>}
                {showAllFocus && activeFocusItems.length > SECTION_PAGE_SIZE && <button onClick={(e) => { setShowAllFocus(false); e.currentTarget.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }} className="btn btn-ghost" style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }}>{t('agent.aware.showLess')}</button>}
                {systemFocusItems.length > 0 && (
                    <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--border-subtle)' }}>
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 600, marginBottom: '6px' }}>{i18n.language?.startsWith('zh') ? '系统 Focus' : 'System Focus'}</div>
                        {systemFocusItems.map(renderFocusItem)}
                    </div>
                )}
                {completedFocusItems.length > 0 && (
                    <>
                        <button onClick={() => setShowCompletedFocus(!showCompletedFocus)} className="btn btn-ghost" style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '8px', borderTop: '1px solid var(--border-subtle)', borderRadius: 0 }}>
                            {showCompletedFocus ? t('agent.aware.hideCompleted') : t('agent.aware.showCompleted', { count: completedFocusItems.length })}
                        </button>
                        {showCompletedFocus && completedFocusItems.map(renderFocusItem)}
                    </>
                )}
                {!hasFocusItems && <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-tertiary)', border: '1px dashed var(--border-subtle)', borderRadius: '8px' }}>{t('agent.aware.focusEmpty')}</div>}
            </div>

            <div className="card background-resource-card" style={{ marginBottom: '16px', padding: '16px' }}>
                <div className="background-resource-header">
                    <div>
                        <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.aware.executionIdentity.title')}</h4>
                        <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.executionIdentity.description')}</span>
                    </div>
                    <span className="background-resource-count">{backgroundTasks.length + schedules.length}</span>
                </div>
                {backgroundTasks.length === 0 && schedules.length === 0 ? (
                    <div className="background-resource-empty">{t('agent.aware.executionIdentity.empty')}</div>
                ) : (
                    <div className="background-resource-list">
                        {(backgroundTasks as any[]).map((task) => (
                            <div key={`task-${task.id}`} className="background-resource-row">
                                <div className="background-resource-main">
                                    <div className="background-resource-title-row">
                                        <span className="background-resource-kind">{t('agent.aware.executionIdentity.task')}</span>
                                        <span className="background-resource-title">{task.title}</span>
                                    </div>
                                    <div className="background-resource-meta">
                                        <span>{task.status}</span>
                                        <span>{task.priority}</span>
                                        <span>{task.type}</span>
                                    </div>
                                </div>
                                <ExecutionIdentityRail
                                    creatorId={task.created_by_user_id || task.created_by}
                                    creatorName={task.creator_display_name || task.creator_username}
                                    executionUserId={task.execution_user_id}
                                    executionUserName={task.execution_user_display_name}
                                    users={executionUsers}
                                    canReassign={canReassignExecutionUser}
                                    isPending={reassignExecutionUser.isPending}
                                    onChoose={() => setExecutionUserPickerTarget({
                                        resourceType: 'task',
                                        resourceId: task.id,
                                        executionUserId: task.execution_user_id || task.created_by_user_id || task.created_by,
                                        expectedExecutionUserId: task.execution_user_id || null,
                                    })}
                                />
                            </div>
                        ))}
                        {(schedules as any[]).map((schedule) => (
                            <div key={`schedule-${schedule.id}`} className="background-resource-row">
                                <div className="background-resource-main">
                                    <div className="background-resource-title-row">
                                        <span className="background-resource-kind schedule">{t('agent.aware.executionIdentity.schedule')}</span>
                                        <span className="background-resource-title">{schedule.name}</span>
                                    </div>
                                    <div className="background-resource-meta">
                                        <span>{schedule.is_enabled ? t('agent.aware.executionIdentity.enabled') : t('agent.aware.executionIdentity.disabled')}</span>
                                        <span className="background-resource-cron">{schedule.cron_expr}</span>
                                        {schedule.next_run_at && <span>{t('agent.aware.executionIdentity.next')} {new Date(schedule.next_run_at).toLocaleString()}</span>}
                                    </div>
                                </div>
                                <ExecutionIdentityRail
                                    creatorId={schedule.created_by_user_id || schedule.created_by}
                                    creatorName={schedule.creator_display_name || schedule.creator_username}
                                    executionUserId={schedule.execution_user_id}
                                    executionUserName={schedule.execution_user_display_name}
                                    users={executionUsers}
                                    canReassign={canReassignExecutionUser}
                                    isPending={reassignExecutionUser.isPending}
                                    onChoose={() => setExecutionUserPickerTarget({
                                        resourceType: 'schedule',
                                        resourceId: schedule.id,
                                        executionUserId: schedule.execution_user_id || schedule.created_by_user_id || schedule.created_by,
                                        expectedExecutionUserId: schedule.execution_user_id || null,
                                    })}
                                />
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {executionUserPickerTarget && (() => {
                const currentUserOption = executionUsers.find((user) => user.id === executionUserPickerTarget.executionUserId);
                return (
                    <OrgMemberAccessPicker
                        open
                        agentId={id}
                        membersOnly
                        singleSelect
                        users={[{
                            id: executionUserPickerTarget.executionUserId,
                            name: currentUserOption?.display_name || currentUserOption?.username || currentUserOption?.email || shortIdentity(executionUserPickerTarget.executionUserId),
                            email: currentUserOption?.email || undefined,
                            access_level: 'use',
                        }]}
                        departments={[]}
                        onClose={() => setExecutionUserPickerTarget(null)}
                        onSave={async (users) => {
                            const nextUser = users[0];
                            if (!nextUser || nextUser.id === executionUserPickerTarget.executionUserId) return;
                            await reassignExecutionUser.mutateAsync({
                                resourceType: executionUserPickerTarget.resourceType,
                                resourceId: executionUserPickerTarget.resourceId,
                                executionUserId: nextUser.id,
                                expectedExecutionUserId: executionUserPickerTarget.expectedExecutionUserId,
                            });
                        }}
                    />
                );
            })()}

            {reflectionSessions.length > 0 && (() => {
                const totalPages = Math.ceil(reflectionSessions.length / REFLECTIONS_PAGE_SIZE);
                const pageStart = reflectionPage * REFLECTIONS_PAGE_SIZE;
                const visibleSessions = reflectionSessions.slice(pageStart, pageStart + REFLECTIONS_PAGE_SIZE);
                return (
                    <div className="card" style={{ padding: '16px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                            <div>
                                <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{isZh ? '执行记录' : 'Executions'}</h4>
                                <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{isZh ? '所有触发入口的状态与标准会话记录' : 'Status and standard conversations for every trigger entry'}</span>
                            </div>
                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{reflectionSessions.length} session{reflectionSessions.length > 1 ? 's' : ''}</span>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                            {visibleSessions.map((session: any) => {
                                const recordId = session.record_id || session.id;
                                const conversationId = session.conversation_id || (!session.conversation_missing ? session.id : null);
                                const isExpanded = expandedReflection === recordId;
                                const msgs = conversationId ? (reflectionMessages[conversationId] || []) : [];
                                return (
                                    <div key={recordId} style={{ borderRadius: '8px', border: '1px solid var(--border-subtle)', overflow: 'hidden', background: 'var(--bg-primary)' }}>
                                        <div
                                            onClick={async () => {
                                                if (isExpanded) {
                                                    setExpandedReflection(null);
                                                    return;
                                                }
                                                setExpandedReflection(recordId);
                                                if (conversationId) await loadReflectionMessages(conversationId);
                                            }}
                                            style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer', transition: 'background 0.15s' }}
                                            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-secondary)')}
                                            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                                        >
                                            <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent-primary)', flexShrink: 0 }} />
                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                <div style={{ fontSize: '12px', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {session.execution ? `${session.execution.trigger_name} · ${session.execution.source}` : formatReflectionTitle(session.title, !!isZh)}
                                                </div>
                                                <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '1px' }}>
                                                    {new Date(session.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                                    {session.message_count > 0 && ` · ${session.message_count} msg`}
                                                </div>
                                            </div>
                                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.15s' }}>&#9654;</span>
                                        </div>
                                        {isExpanded && (
                                            <div style={{ padding: '0 16px 12px', borderTop: '1px solid var(--border-subtle)' }}>
                                                {!conversationId ? (
                                                    <ConversationTimeline
                                                        agentId={id}
                                                        agentName={agent.name || 'Agent'}
                                                        messages={[]}
                                                        provenance={session.execution}
                                                        onOpenSubagentSession={openSubagentSession}
                                                        viewOf={() => ({ isLeft: true })}
                                                    />
                                                ) : msgs.length === 0 ? (
                                                    <div style={{ padding: '12px 0', fontSize: '12px', color: 'var(--text-tertiary)' }}>Loading...</div>
                                                ) : (
                                                    <div style={{ marginTop: '8px' }}>
                                                        <ConversationTimeline
                                                            agentId={id}
                                                            agentName={agent.name || 'Agent'}
                                                            messages={msgs}
                                                            provenance={session.execution}
                                                            viewOf={(message) => ({
                                                                isLeft: message.role !== 'user',
                                                                senderLabel: message.role === 'user' ? (isZh ? '触发事件' : 'Trigger event') : (agent.name || 'Agent'),
                                                                avatarText: message.role === 'user' ? 'T' : (agent.name || 'A')[0],
                                                                forceSenderLabel: true,
                                                            })}
                                                            unavailableAttachmentKeys={unavailableAttachmentKeys}
                                                            onAttachmentDownload={handleAttachmentDownload}
                                                            onAttachmentUnavailable={markAttachmentUnavailable}
                                                            onPreviewImages={(images, index) => setChatImagePreview({ images, index })}
                                                            onOpenSubagentSession={openSubagentSession}
                                                            onToolResolved={(message, result) => upsertToolCallMessage({ ...message, toolStatus: 'done', toolResult: result } as any)}
                                                        />
                                                    </div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                        {totalPages > 1 && (
                            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', marginTop: '12px', paddingTop: '8px', borderTop: '1px solid var(--border-subtle)' }}>
                                <button onClick={() => { setReflectionPage((p) => Math.max(0, p - 1)); setExpandedReflection(null); }} disabled={reflectionPage === 0} className="btn btn-ghost" style={{ fontSize: '12px', padding: '4px 10px', opacity: reflectionPage === 0 ? 0.3 : 1 }}>{i18n.language?.startsWith('zh') ? '上一页' : 'Prev'}</button>
                                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums' }}>{reflectionPage + 1} / {totalPages}</span>
                                <button onClick={() => { setReflectionPage((p) => Math.min(totalPages - 1, p + 1)); setExpandedReflection(null); }} disabled={reflectionPage >= totalPages - 1} className="btn btn-ghost" style={{ fontSize: '12px', padding: '4px 10px', opacity: reflectionPage >= totalPages - 1 ? 0.3 : 1 }}>{i18n.language?.startsWith('zh') ? '下一页' : 'Next'}</button>
                            </div>
                        )}
                    </div>
                );
            })()}
        </div>
    );
}
