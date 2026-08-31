import React from 'react';
import {
    IconAlertTriangle,
    IconBolt,
    IconCheck,
    IconClock,
    IconFileText,
    IconHeartbeat,
    IconMailForward,
    IconMessageCircle,
    IconRobot,
    IconSend,
    IconSettings,
    IconUser,
    IconWorld,
} from '@tabler/icons-react';

export default function ActivityLogTabContent({
    agent,
    activityLogs,
    logFilter,
    setLogFilter,
    expandedLogId,
    setExpandedLogId,
    t,
}: any) {
    const userActionTypes = ['chat_reply', 'tool_call', 'task_created', 'task_updated', 'file_written', 'error'];
    const heartbeatTypes = ['heartbeat'];
    const scheduleTypes = ['schedule_run'];
    const messageTypes = ['feishu_msg_sent', 'agent_msg_sent', 'web_msg_sent'];

    let filteredLogs = activityLogs;
    if (logFilter === 'user') {
        filteredLogs = activityLogs.filter((l: any) => userActionTypes.includes(l.action_type));
    } else if (logFilter === 'backend') {
        filteredLogs = activityLogs.filter((l: any) => !userActionTypes.includes(l.action_type));
    } else if (logFilter === 'heartbeat') {
        filteredLogs = activityLogs.filter((l: any) => heartbeatTypes.includes(l.action_type));
    } else if (logFilter === 'schedule') {
        filteredLogs = activityLogs.filter((l: any) => scheduleTypes.includes(l.action_type));
    } else if (logFilter === 'messages') {
        filteredLogs = activityLogs.filter((l: any) => messageTypes.includes(l.action_type));
    }

    const filterBtn = (key: string, label: React.ReactNode, indent = false) => (
        <button
            key={key}
            onClick={() => setLogFilter(key)}
            style={{
                padding: indent ? '4px 10px 4px 20px' : '6px 14px',
                fontSize: indent ? '11px' : '12px',
                fontWeight: logFilter === key ? 600 : 400,
                color: logFilter === key ? 'var(--accent-primary)' : 'var(--text-secondary)',
                background: logFilter === key ? 'rgba(99,102,241,0.1)' : 'transparent',
                border: logFilter === key ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                borderRadius: '6px',
                cursor: 'pointer',
                transition: 'all 0.15s',
                whiteSpace: 'nowrap' as const,
                display: 'inline-flex',
                alignItems: 'center',
                gap: '5px',
            }}
        >
            {label}
        </button>
    );

    return (
        <div>
            <h3 style={{ marginBottom: '12px' }}>{t('agent.activityLog.title')}</h3>
            <div style={{ display: 'flex', gap: '6px', marginBottom: '16px', flexWrap: 'wrap', alignItems: 'center' }}>
                {filterBtn('user', <><IconUser size={13} stroke={1.8} /> {t('agent.activityLog.userActions', 'User Actions')}</>)}
                {(agent as any)?.agent_type !== 'openclaw' && (<>
                    {filterBtn('backend', <><IconSettings size={13} stroke={1.8} /> {t('agent.activityLog.backendServices', 'Backend Services')}</>)}
                    {(logFilter === 'backend' || logFilter === 'heartbeat' || logFilter === 'schedule' || logFilter === 'messages') && (
                        <>
                            <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>│</span>
                            {filterBtn('heartbeat', <><IconHeartbeat size={13} stroke={1.8} /> {t('agent.mind.heartbeatTitle')}</>)}
                            {filterBtn('schedule', <><IconClock size={13} stroke={1.8} /> {t('agent.activityLog.scheduleCron')}</>, true)}
                            {filterBtn('messages', <><IconMailForward size={13} stroke={1.8} /> {t('agent.activityLog.messages')}</>, true)}
                        </>
                    )}
                </>)}
            </div>

            {filteredLogs.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {filteredLogs.map((log: any) => {
                        const icons: Record<string, React.ReactNode> = {
                            chat_reply: <IconMessageCircle size={16} stroke={1.8} />,
                            tool_call: <IconBolt size={16} stroke={1.8} />,
                            feishu_msg_sent: <IconSend size={16} stroke={1.8} />,
                            agent_msg_sent: <IconRobot size={16} stroke={1.8} />,
                            web_msg_sent: <IconWorld size={16} stroke={1.8} />,
                            task_created: <IconFileText size={16} stroke={1.8} />,
                            task_updated: <IconCheck size={16} stroke={1.8} />,
                            file_written: <IconFileText size={16} stroke={1.8} />,
                            error: <IconAlertTriangle size={16} stroke={1.8} />,
                            schedule_run: <IconClock size={16} stroke={1.8} />,
                            heartbeat: <IconHeartbeat size={16} stroke={1.8} />,
                        };
                        const time = log.created_at ? new Date(log.created_at).toLocaleString('zh-CN', {
                            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
                        }) : '';
                        const isExpanded = expandedLogId === log.id;
                        return (
                            <div
                                key={log.id}
                                onClick={() => setExpandedLogId(isExpanded ? null : log.id)}
                                style={{
                                    padding: '10px 14px', borderRadius: '8px', cursor: 'pointer',
                                    background: isExpanded ? 'var(--bg-elevated)' : 'var(--bg-secondary)', fontSize: '13px',
                                    border: isExpanded ? '1px solid var(--accent-primary)' : '1px solid transparent',
                                    transition: 'all 0.15s ease',
                                }}
                            >
                                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                                    <span style={{ width: '18px', height: '18px', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: '1px', color: 'var(--text-tertiary)' }}>
                                        {icons[log.action_type] || '·'}
                                    </span>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div style={{ fontWeight: 500, marginBottom: '2px' }}>{log.summary}</div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {time} · {log.action_type}
                                            {log.detail && !isExpanded && <span style={{ marginLeft: '8px', color: 'var(--accent-primary)' }}>▸ Details</span>}
                                        </div>
                                    </div>
                                </div>
                                {isExpanded && log.detail && (
                                    <div style={{ marginTop: '8px', padding: '10px', borderRadius: '6px', background: 'var(--bg-primary)', fontSize: '12px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-all', lineHeight: '1.6', color: 'var(--text-secondary)', maxHeight: '300px', overflowY: 'auto' }}>
                                        {Object.entries(log.detail).map(([k, v]: [string, any]) => (
                                            <div key={k} style={{ marginBottom: '6px' }}>
                                                <span style={{ color: 'var(--accent-primary)', fontWeight: 600 }}>{k}:</span>{' '}
                                                <span>{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            ) : (
                <div className="card" style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
                    {t('agent.activityLog.noRecords')}
                </div>
            )}
        </div>
    );
}
