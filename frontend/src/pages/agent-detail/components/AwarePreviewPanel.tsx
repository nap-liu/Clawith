import ConversationTimeline from '../../../features/conversation/web/ConversationTimeline';
import ExecutionIdentityRail from './ExecutionIdentityRail';
import {
    focusItemFromApi,
    formatReflectionTitle,
    synthesizeFocusForTrigger,
    type FocusItem,
} from '../shared';

export default function AwarePreviewPanel({
    focusRecords,
    awareTriggers,
    i18n,
    expandedFocusIds,
    toggleExpandedFocus,
    t,
    awareCalendarDate,
    awareCalendarMode,
    setAwareCalendarDate,
    setAwareCalendarMode,
    awareView,
    setAwareView,
    showAllSideActive,
    setShowAllSideActive,
    showAllSideSystem,
    setShowAllSideSystem,
    showCompletedFocus,
    setShowCompletedFocus,
    showAllSideCompleted,
    setShowAllSideCompleted,
    reflectionSessions,
    expandedReflection,
    setExpandedReflection,
    reflectionMessages,
    loadReflectionMessages,
    id,
    agent,
    openSubagentSession,
    unavailableAttachmentKeys,
    handleAttachmentDownload,
    markAttachmentUnavailable,
    setChatImagePreview,
    upsertToolCallMessage,
}: any) {
    const focusItems = focusRecords.map(focusItemFromApi);
    const isZh = i18n.language?.startsWith('zh');
    const formatTrigger = (trig: any) => {
        if (trig.type === 'cron' && trig.config?.expr) return `Cron ${trig.config.expr}`;
        if (trig.type === 'interval' && trig.config?.minutes) return isZh ? `每 ${trig.config.minutes} 分钟` : `Every ${trig.config.minutes} min`;
        if (trig.type === 'once' && trig.config?.at) return new Date(trig.config.at).toLocaleString();
        return trig.name || trig.type;
    };
    const triggerTitle = (trig: any) => String(trig.reason || trig.name || trig.type || '').trim();
    const triggerMeta = (trig: any) => {
        const schedule = formatTrigger(trig);
        if (!trig.reason || schedule === trig.reason) return schedule;
        return schedule;
    };
    const triggerTooltip = (trig: any) => {
        const title = triggerTitle(trig);
        const meta = triggerMeta(trig);
        const parts = [title, meta];
        if (trig.reason && trig.reason !== title) parts.push(String(trig.reason));
        if (trig.name && trig.name !== title) parts.push(String(trig.name));
        return Array.from(new Set(parts.filter(Boolean))).join('\n');
    };
    const triggersByFocus: Record<string, any[]> = {};
    const focusNames = new Set(focusItems.map((item: any) => item.name));
    for (const trig of awareTriggers as any[]) {
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
    const activeFocusItems = displayFocusItems.filter((item: any) => !item.done && !item.system);
    const systemFocusItems = displayFocusItems.filter((item: any) => !item.done && item.system);
    const completedFocusItems = displayFocusItems.filter((item: any) => item.done);
    const renderTriggerDot = (done: boolean, label: string) => (
        <span className={`aware-side-status-dot ${done ? 'done' : 'active'}`} aria-label={label} />
    );
    const renderFocusItem = (item: FocusItem) => {
        const isExpanded = expandedFocusIds.has(item.id);
        const itemTriggers = triggersByFocus[item.name] || [];
        const hasTitle = !!item.title;
        const displayTitle = hasTitle ? item.title : item.name;
        const displaySubtitle = hasTitle ? item.name : null;
        const displayDescription = item.description;

        return (
            <div key={item.id} className={`aware-side-focus ${item.done ? 'done' : ''}`}>
                <button className="aware-side-focus-head" type="button" onClick={() => toggleExpandedFocus(item.id)}>
                    <div className="aware-side-trigger-main">
                        <div className="aware-side-item-title" style={{ fontWeight: 500 }}>
                            <span>{displayTitle}</span>
                            {item.done && (
                                <span className="aware-side-focus-badge done">
                                    {t('agent.aware.completed')}
                                </span>
                            )}
                        </div>
                        {displaySubtitle && (
                            <div className="aware-side-item-meta" style={{ fontFamily: 'monospace' }}>
                                {displaySubtitle}
                            </div>
                        )}
                        {displayDescription && (
                            <div className="aware-side-item-desc">
                                {displayDescription}
                            </div>
                        )}
                    </div>
                    <span className="aware-side-count">
                        {isZh ? `${itemTriggers.length} 个` : itemTriggers.length}
                    </span>
                    <span className={`aware-side-chevron ${isExpanded ? 'open' : ''}`}>▶</span>
                </button>
                {isExpanded && (
                    <div className="aware-side-nested">
                        {itemTriggers.length === 0 ? (
                            <div className="aware-side-empty compact">{t('agent.aware.noTriggers')}</div>
                        ) : itemTriggers.map((trig: any) => (
                            <div key={trig.id} className={`aware-side-trigger ${trig.is_enabled ? '' : 'done'}`}>
                                {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                <div className="aware-side-trigger-main">
                                    <div className="aware-side-item-title">{triggerTitle(trig)}</div>
                                    <div className="aware-side-item-meta">{triggerMeta(trig)}</div>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        );
    };
    const SIDE_FOCUS_LIMIT = 12;
    const renderFocusGroup = (
        title: string,
        items: FocusItem[],
        showAll: boolean,
        setShowAll: (val: boolean) => void,
    ) => {
        if (items.length === 0) return null;
        const hasMore = items.length > SIDE_FOCUS_LIMIT;
        const visibleItems = showAll ? items : items.slice(0, SIDE_FOCUS_LIMIT);
        return (
            <div className="aware-side-focus-group">
                <div className="aware-side-subtitle" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span>{title}</span>
                    {hasMore && (
                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                            {showAll ? '' : `${SIDE_FOCUS_LIMIT}/${items.length}`}
                        </span>
                    )}
                </div>
                {visibleItems.map(renderFocusItem)}
                {hasMore && (
                    <button
                        type="button"
                        className="aware-side-collapse"
                        onClick={() => setShowAll(!showAll)}
                        style={{ marginTop: '4px', width: '100%', textAlign: 'center', borderTop: '1px dashed var(--border-subtle)', paddingTop: '6px' }}
                    >
                        <span>
                            {showAll
                                ? (isZh ? '收起' : 'Show less')
                                : (isZh ? `显示更多 (+${items.length - SIDE_FOCUS_LIMIT})` : `Show more (+${items.length - SIDE_FOCUS_LIMIT})`)
                            }
                        </span>
                    </button>
                )}
            </div>
        );
    };
    const parseTriggerTime = (trig: any): Date | null => {
        if (trig.type === 'once' && trig.config?.at) {
            const date = new Date(trig.config.at);
            return Number.isNaN(date.getTime()) ? null : date;
        }
        return null;
    };
    const startOfDay = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const today = startOfDay(new Date());
    const calendarAnchor = startOfDay(awareCalendarDate);
    const calendarDays = (() => {
        if (awareCalendarMode === 'day') return [calendarAnchor];
        if (awareCalendarMode === 'month') {
            const first = new Date(calendarAnchor.getFullYear(), calendarAnchor.getMonth(), 1);
            return Array.from({ length: 31 }, (_, idx) => new Date(first.getFullYear(), first.getMonth(), first.getDate() + idx))
                .filter((date) => date.getMonth() === first.getMonth());
        }
        const weekStart = new Date(calendarAnchor);
        weekStart.setDate(calendarAnchor.getDate() - ((calendarAnchor.getDay() + 6) % 7));
        return Array.from({ length: 7 }, (_, idx) => new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + idx));
    })();
    const calendarRangeLabel = (() => {
        if (awareCalendarMode === 'day') {
            return calendarAnchor.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', weekday: 'short' });
        }
        if (awareCalendarMode === 'month') {
            return calendarAnchor.toLocaleDateString(undefined, { year: 'numeric', month: 'long' });
        }
        const first = calendarDays[0];
        const last = calendarDays[calendarDays.length - 1];
        return `${first.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} - ${last.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}`;
    })();
    const shiftCalendar = (direction: -1 | 1) => {
        setAwareCalendarDate((prev: Date) => {
            const next = new Date(prev);
            if (awareCalendarMode === 'day') next.setDate(next.getDate() + direction);
            else if (awareCalendarMode === 'week') next.setDate(next.getDate() + direction * 7);
            else next.setMonth(next.getMonth() + direction);
            return next;
        });
    };
    const timedTriggers = (awareTriggers as any[]).filter((trig) => ['once', 'cron', 'interval'].includes(trig.type));
    const recurringTriggers = timedTriggers.filter((trig) => !parseTriggerTime(trig));
    const triggersForDay = (day: Date) => timedTriggers.filter((trig) => {
        const when = parseTriggerTime(trig);
        return !!when && startOfDay(when).getTime() === day.getTime();
    });
    const renderCalendar = () => (
        <div className="aware-calendar">
            <div className="aware-calendar-header">
                <div className="aware-calendar-toolbar">
                    {(['day', 'week', 'month'] as const).map(mode => (
                        <button
                            key={mode}
                            type="button"
                            className={`aware-view-button ${awareCalendarMode === mode ? 'active' : ''}`}
                            onClick={() => setAwareCalendarMode(mode)}
                        >
                            {isZh ? ({ day: '日', week: '周', month: '月' } as const)[mode] : mode}
                        </button>
                    ))}
                </div>
                <div className="aware-calendar-nav">
                    <button type="button" className="aware-calendar-nav-button" onClick={() => shiftCalendar(-1)} aria-label={isZh ? '上一段时间' : 'Previous'}>
                        ‹
                    </button>
                    <button type="button" className="aware-calendar-range" onClick={() => setAwareCalendarDate(new Date())}>
                        {calendarRangeLabel}
                    </button>
                    <button type="button" className="aware-calendar-nav-button" onClick={() => shiftCalendar(1)} aria-label={isZh ? '下一段时间' : 'Next'}>
                        ›
                    </button>
                </div>
            </div>
            <div className={`aware-calendar-grid mode-${awareCalendarMode}`}>
                {calendarDays.map((day) => {
                    const items = triggersForDay(day);
                    const isToday = day.getTime() === today.getTime();
                    return (
                        <div key={day.toISOString()} className={`aware-calendar-day ${isToday ? 'is-today' : ''}`}>
                            <div className="aware-calendar-day-label">
                                {day.toLocaleDateString(undefined, awareCalendarMode === 'month' ? { day: 'numeric' } : { weekday: 'short', month: 'numeric', day: 'numeric' })}
                                {isToday && <span className="aware-calendar-today-pill">{isZh ? '今天' : 'Today'}</span>}
                            </div>
                            {items.length === 0 ? (
                                <div className="aware-calendar-empty">-</div>
                            ) : items.slice(0, 3).map((trig: any) => (
                                <div key={trig.id} className="aware-calendar-event" data-tooltip={triggerTooltip(trig)} aria-label={triggerTooltip(trig)}>
                                    {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                                    <span className="aware-calendar-event-body">
                                        <span className="aware-calendar-event-title">{triggerTitle(trig)}</span>
                                        <span className="aware-calendar-event-meta">{triggerMeta(trig)}</span>
                                    </span>
                                </div>
                            ))}
                            {items.length > 3 && <div className="aware-calendar-more">+{items.length - 3}</div>}
                        </div>
                    );
                })}
            </div>
            {recurringTriggers.length > 0 && (
                <div className="aware-calendar-recurring">
                    <div className="aware-side-subtitle">{isZh ? '重复计划' : 'Recurring'}</div>
                    {recurringTriggers.slice(0, 6).map((trig: any) => (
                        <div key={trig.id} className="aware-calendar-event recurring" data-tooltip={triggerTooltip(trig)} aria-label={triggerTooltip(trig)}>
                            {renderTriggerDot(!trig.is_enabled, trig.is_enabled ? t('agent.aware.inProgress') : t('agent.aware.completed'))}
                            <span className="aware-calendar-event-body">
                                <span className="aware-calendar-event-title">{triggerTitle(trig)}</span>
                                <span className="aware-calendar-event-meta">{triggerMeta(trig)}</span>
                            </span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );

    return (
        <div className="aware-side-preview">
            <div className="aware-side-section">
                <div className="aware-side-title-row">
                    <div className="aware-side-section-title">{t('agent.aware.focus')}</div>
                    <div className="aware-view-switch">
                        <button
                            type="button"
                            className={`aware-view-button ${awareView === 'list' ? 'active' : ''}`}
                            onClick={() => setAwareView('list')}
                        >
                            {isZh ? '列表' : 'List'}
                        </button>
                        <button
                            type="button"
                            className={`aware-view-button ${awareView === 'calendar' ? 'active' : ''}`}
                            onClick={() => setAwareView('calendar')}
                        >
                            {isZh ? '日历' : 'Calendar'}
                        </button>
                    </div>
                </div>
                {awareView === 'calendar' ? renderCalendar() : (
                    displayFocusItems.length === 0 ? (
                        <div className="aware-side-empty">{t('agent.aware.focusEmpty')}</div>
                    ) : (
                        <>
                            {renderFocusGroup(isZh ? '进行中' : 'In progress', activeFocusItems, showAllSideActive, setShowAllSideActive)}
                            {renderFocusGroup(isZh ? '系统 Focus' : 'System Focus', systemFocusItems, showAllSideSystem, setShowAllSideSystem)}
                            {completedFocusItems.length > 0 && (
                                <div className="aware-side-focus-group">
                                    <button
                                        type="button"
                                        className="aware-side-collapse"
                                        onClick={() => { setShowCompletedFocus(!showCompletedFocus); setShowAllSideCompleted(false); }}
                                    >
                                        <span>{showCompletedFocus ? (isZh ? '收起已完成' : 'Hide completed') : (isZh ? `已完成 (${completedFocusItems.length})` : `Completed (${completedFocusItems.length})`)}</span>
                                        <span className={`aware-side-chevron ${showCompletedFocus ? 'open' : ''}`}>▶</span>
                                    </button>
                                    {showCompletedFocus && (
                                        <>
                                            {(showAllSideCompleted ? completedFocusItems : completedFocusItems.slice(0, SIDE_FOCUS_LIMIT)).map(renderFocusItem)}
                                            {completedFocusItems.length > SIDE_FOCUS_LIMIT && (
                                                <button
                                                    type="button"
                                                    className="aware-side-collapse"
                                                    onClick={() => setShowAllSideCompleted(!showAllSideCompleted)}
                                                    style={{ marginTop: '4px', width: '100%', textAlign: 'center', borderTop: '1px dashed var(--border-subtle)', paddingTop: '6px' }}
                                                >
                                                    <span>
                                                        {showAllSideCompleted
                                                            ? (isZh ? '收起' : 'Show less')
                                                            : (isZh ? `显示更多 (+${completedFocusItems.length - SIDE_FOCUS_LIMIT})` : `Show more (+${completedFocusItems.length - SIDE_FOCUS_LIMIT})`)
                                                        }
                                                    </span>
                                                </button>
                                            )}
                                        </>
                                    )}
                                </div>
                            )}
                        </>
                    )
                )}
            </div>
            <div className="aware-side-section">
                <div className="aware-side-section-title">{isZh ? '执行记录' : 'Executions'}</div>
                {(reflectionSessions as any[]).length === 0 ? (
                    <div className="aware-side-empty">{isZh ? '暂无执行记录' : 'No executions yet'}</div>
                ) : (reflectionSessions as any[]).slice(0, 10).map((session: any) => {
                    const recordId = session.record_id || session.id;
                    const conversationId = session.conversation_id || (!session.conversation_missing ? session.id : null);
                    const isExpanded = expandedReflection === recordId;
                    const msgs = conversationId ? (reflectionMessages[conversationId] || []) : [];
                    return (
                        <div key={recordId} className="aware-side-reflection">
                            <button
                                type="button"
                                className="aware-side-reflection-head"
                                onClick={async () => {
                                    if (isExpanded) {
                                        setExpandedReflection(null);
                                        return;
                                    }
                                    setExpandedReflection(recordId);
                                    if (conversationId) await loadReflectionMessages(conversationId);
                                }}
                            >
                                <span className="aware-side-dot active" />
                                <div className="aware-side-trigger-main">
                                    <div className="aware-side-item-title">
                                        {session.execution
                                            ? `${session.execution.trigger_name} · ${session.execution.source}`
                                            : formatReflectionTitle(session.title, !!isZh)}
                                    </div>
                                    <div className="aware-side-item-meta">
                                        {new Date(session.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                        {session.message_count > 0 ? ` · ${session.message_count}` : ''}
                                    </div>
                                </div>
                                <span className={`aware-side-chevron ${isExpanded ? 'open' : ''}`}>▶</span>
                            </button>
                            {isExpanded && (
                                <div className="aware-side-reflection-detail">
                                    {!conversationId ? (
                                        <ConversationTimeline
                                            agentId={id!}
                                            agentName={agent.name || 'Agent'}
                                            messages={[]}
                                            provenance={session.execution}
                                            onOpenSubagentSession={openSubagentSession}
                                            viewOf={() => ({ isLeft: true })}
                                        />
                                    ) : msgs.length === 0 ? (
                                        <div className="aware-side-empty compact">{isZh ? '正在加载...' : 'Loading...'}</div>
                                    ) : (
                                        <ConversationTimeline
                                            agentId={id!}
                                            agentName={agent.name || 'Agent'}
                                            messages={msgs}
                                            provenance={session.execution}
                                            viewOf={(message) => ({
                                                isLeft: message.role !== 'user',
                                                senderLabel: message.role === 'user'
                                                    ? (isZh ? '触发事件' : 'Trigger event')
                                                    : (agent.name || 'Agent'),
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
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
