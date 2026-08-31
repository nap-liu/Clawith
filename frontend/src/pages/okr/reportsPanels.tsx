import React, { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { IconAlertTriangle } from '@tabler/icons-react';
import { fetchJson } from '../../services/api';
import { useAuthStore } from '../../stores';
import type {
    CompanyReport,
    MemberDailyReportItem,
    MembersWithoutOKRData,
} from './model';

export function MembersWithoutOKRPanel({
    isChinese,
    periodStart,
    periodEnd,
}: {
    isChinese: boolean;
    periodStart: string;
    periodEnd: string;
}) {
    const queryClient = useQueryClient();
    const [nudging, setNudging] = React.useState(false);
    const [nudgeResult, setNudgeResult] = React.useState<string | null>(null);

    const { data, isLoading } = useQuery<MembersWithoutOKRData>({
        queryKey: ['okr-members-without-okr', periodStart, periodEnd],
        queryFn: () => fetchJson<MembersWithoutOKRData>('/okr/members-without-okr'),
        staleTime: 0,
        refetchOnWindowFocus: true,
    });

    React.useEffect(() => {
        if (data?.last_outreach_error && nudgeResult) {
            setNudgeResult(null);
        }
    }, [data?.last_outreach_error, nudgeResult]);

    if (isLoading || !data || !data.members_without_okr?.length) {
        return null;
    }

    async function handleNudge() {
        setNudging(true);
        setNudgeResult(null);
        try {
            const result = await fetchJson<{ status: string; message: string; members_count: number }>(
                '/okr/trigger-member-outreach',
                { method: 'POST' }
            );
            setNudgeResult(result.message);
            queryClient.invalidateQueries({ queryKey: ['okr-members-without-okr'] });

            setTimeout(() => queryClient.invalidateQueries({ queryKey: ['okr-members-without-okr'] }), 5000);
            setTimeout(() => queryClient.invalidateQueries({ queryKey: ['okr-members-without-okr'] }), 12000);
        } catch (e: any) {
            setNudgeResult(e.message ?? (isChinese ? '催促失败，请重试' : 'Failed to trigger outreach'));
        } finally {
            setNudging(false);
        }
    }

    const { members_without_okr: members, company_okr_exists, okr_agent_id, last_outreach_error, channel_warnings } = data;

    const unreachableNames = new Set<string>();
    if (channel_warnings?.length) {
        for (const w of channel_warnings) {
            for (const name of w.affected_members) {
                unreachableNames.add(name);
            }
        }
    }

    return (
        <section style={{ marginTop: '32px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.8px' }}>
                    {isChinese ? '未设定 OKR 的成员' : 'Members Without OKR'}
                </span>
                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                <span style={{ fontSize: '11px', color: 'var(--text-quaternary)' }}>{members.length}</span>
            </div>

            {channel_warnings && channel_warnings.length > 0 && (
                <div style={{
                    display: 'flex', alignItems: 'flex-start', gap: '8px',
                    padding: '10px 14px',
                    marginBottom: '12px',
                    background: 'rgba(245, 158, 11, 0.06)',
                    border: '1px solid rgba(245, 158, 11, 0.2)',
                    borderRadius: '8px',
                    fontSize: '12px',
                    color: '#92400e',
                    lineHeight: 1.6,
                }}>
                    <IconAlertTriangle size={14} stroke={1.8} style={{ flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                        {channel_warnings.map((w, i) => (
                            <div key={i} style={{ marginBottom: i < channel_warnings.length - 1 ? '4px' : 0 }}>
                                <span style={{ fontWeight: 600 }}>{w.affected_members.join('、')}</span>
                                {' '}
                                {isChinese
                                    ? `为 ${w.channel_display} 用户，但该 Agent 未配置 ${w.channel_display} 机器人，催办消息无法送达。`
                                    : `are on ${w.channel_display} but the Agent has no ${w.channel_display} bot configured. Nudge messages cannot be delivered.`}
                            </div>
                        ))}
                        {okr_agent_id && (
                            <a
                                href={`/agents/${okr_agent_id}#settings`}
                                style={{
                                    fontSize: '11px', color: '#d97706',
                                    textDecoration: 'none', fontWeight: 500,
                                    display: 'inline-block', marginTop: '2px',
                                }}
                            >
                                {isChinese ? '前往 Agent Settings 配置 →' : 'Configure in Agent Settings →'}
                            </a>
                        )}
                    </div>
                </div>
            )}

            <div style={{
                display: 'flex', alignItems: 'center', gap: '16px',
                marginBottom: '12px',
                padding: '10px 14px',
                background: 'var(--bg-secondary)',
                borderRadius: '8px',
                border: '1px solid var(--border-subtle)',
            }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
                        {company_okr_exists
                            ? (isChinese
                                ? 'OKR Agent 将向以上成员发送消息，邀请他们设定个人 OKR。发送后可在 OKR Agent 的会话列表里查看具体对话记录。'
                                : 'OKR Agent will message each member above. You can review the conversations in the OKR Agent\'s chat history.')
                            : (isChinese
                                ? '请先与 OKR Agent 确认公司 OKR，再催促成员。'
                                : 'Please set company OKRs with the OKR Agent before nudging members.')
                        }
                    </div>
                    {nudgeResult && (
                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '6px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span>{nudgeResult}</span>
                            {okr_agent_id && (
                                <a
                                    href={`/agents/${okr_agent_id}`}
                                    style={{ fontSize: '11px', color: 'var(--accent-primary)', textDecoration: 'none', whiteSpace: 'nowrap' }}
                                >
                                    {isChinese ? '查看会话 →' : 'View chat →'}
                                </a>
                            )}
                        </div>
                    )}
                    {!nudgeResult && last_outreach_error && (
                        <div style={{
                            fontSize: '12px',
                            color: '#b91c1c',
                            background: 'rgba(239,68,68,0.08)',
                            border: '1px solid rgba(239,68,68,0.2)',
                            borderRadius: '6px',
                            padding: '8px 10px',
                            marginTop: '8px',
                            lineHeight: 1.5,
                            display: 'flex',
                            alignItems: 'flex-start',
                            gap: '6px',
                        }}>
                            <IconAlertTriangle size={14} stroke={1.8} style={{ flexShrink: 0 }} />
                            <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontWeight: 600, marginBottom: '2px' }}>
                                    {isChinese ? 'OKR Agent 上次执行失败' : 'OKR Agent task failed'}
                                </div>
                                <div style={{ color: '#991b1b', wordBreak: 'break-word' }}>
                                    {last_outreach_error.message}
                                </div>
                                {last_outreach_error.timestamp && (
                                    <div style={{ fontSize: '11px', color: '#b45309', marginTop: '4px' }}>
                                        {new Date(last_outreach_error.timestamp).toLocaleString()}
                                    </div>
                                )}
                                {okr_agent_id && (
                                    <a
                                        href={`/agents/${okr_agent_id}#settings`}
                                        style={{ fontSize: '11px', color: 'var(--accent-primary)', textDecoration: 'none', marginTop: '4px', display: 'inline-block' }}
                                    >
                                        {isChinese ? '检查 Agent 设置 →' : 'Check Agent Settings →'}
                                    </a>
                                )}
                            </div>
                        </div>
                    )}
                </div>
                <button
                    id="okr-nudge-btn"
                    onClick={handleNudge}
                    disabled={nudging || !company_okr_exists}
                    title={company_okr_exists ? undefined : (isChinese ? '请先设定公司 OKR' : 'Set company OKRs first')}
                    style={{
                        display: 'flex', alignItems: 'center', gap: '6px',
                        padding: '5px 12px', borderRadius: '6px',
                        border: 'none', flexShrink: 0,
                        background: company_okr_exists ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                        color: company_okr_exists ? '#fff' : 'var(--text-quaternary)',
                        fontSize: '12px', fontWeight: 500,
                        cursor: nudging || !company_okr_exists ? 'not-allowed' : 'pointer',
                        opacity: nudging ? 0.7 : 1,
                        transition: 'opacity 0.15s, background 0.15s',
                        whiteSpace: 'nowrap',
                    }}
                >
                    {nudging ? (
                        <>{isChinese ? '发送中...' : 'Sending...'}</>
                    ) : (
                        <>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M22 2L11 13"/><path d="M22 2L15 22 11 13 2 9l20-7z"/>
                            </svg>
                            {isChinese ? '催促设定 OKR' : 'Nudge Members'}
                        </>
                    )}
                </button>
            </div>

            <div style={{
                border: '1px solid var(--border-subtle)',
                borderRadius: '10px',
                overflow: 'hidden',
                background: 'var(--bg-primary)',
            }}>
                <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {members.map((member) => (
                        <div key={member.user_id || member.agent_id} style={{
                            display: 'flex', alignItems: 'center', gap: '10px',
                            padding: '8px 10px',
                            background: 'var(--bg-secondary)',
                            borderRadius: '6px',
                            border: '1px solid var(--border-subtle)',
                        }}>
                            <div style={{
                                width: 28, height: 28, borderRadius: '50%', flexShrink: 0,
                                background: member.agent_id ? 'rgba(99,102,241,0.15)' : 'rgba(16,185,129,0.15)',
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                fontSize: '11px', fontWeight: 600,
                                color: member.agent_id ? '#6366f1' : '#10b981',
                            }}>
                                {(member.display_name || '?').charAt(0).toUpperCase()}
                            </div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}>
                                    {member.display_name}
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-quaternary)' }}>
                                    {member.agent_id
                                        ? 'AI Agent'
                                        : (member.source_label
                                            ? (member.source_label === 'Platform User'
                                                ? (isChinese ? '平台成员' : 'Platform member')
                                                : member.source_label)
                                            : (isChinese ? '平台成员' : 'Platform member'))}
                                </div>
                            </div>
                            {unreachableNames.has(member.display_name) && (
                                <span title={isChinese ? '渠道未配置，无法推送' : 'Channel not configured, cannot deliver'}
                                    style={{ cursor: 'help', flexShrink: 0, display: 'inline-flex' }}><IconAlertTriangle size={13} stroke={1.8} /></span>
                            )}
                            <span style={{
                                fontSize: '10px', color: 'var(--text-tertiary)',
                                border: '1px dashed var(--border-subtle)',
                                borderRadius: '4px', padding: '1px 6px',
                            }}>
                                {isChinese ? '未设定 OKR' : 'No OKR'}
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        </section>
    );
}

export function ReportsTab({ isChinese }: { isChinese: boolean }) {
    const qc = useQueryClient();
    const currentUser = useAuthStore((s) => s.user);
    const [view, setView] = useState<'company' | 'member'>('company');
    const [reportType, setReportType] = useState<'daily' | 'weekly' | 'monthly'>('daily');
    const [selectedDate, setSelectedDate] = useState(() => new Date().toISOString().slice(0, 10));
    const [expandedCompanyReportId, setExpandedCompanyReportId] = useState<string | null>(null);
    const [selectedMemberReportId, setSelectedMemberReportId] = useState<string | null>(null);
    const [memberSearch, setMemberSearch] = useState('');
    const isAdmin = currentUser?.role === 'org_admin' || currentUser?.role === 'platform_admin';

    const { data: companyReports = [], isLoading: companyLoading } = useQuery<CompanyReport[]>({
        queryKey: ['company-reports', reportType],
        queryFn: () => fetchJson<CompanyReport[]>(`/okr/company-reports?report_type=${reportType}`),
        enabled: view === 'company',
    });

    const { data: memberReports = [], isLoading: memberLoading } = useQuery<MemberDailyReportItem[]>({
        queryKey: ['member-daily-reports', selectedDate],
        queryFn: () => fetchJson<MemberDailyReportItem[]>(`/okr/member-daily-reports?report_date=${selectedDate}`),
        enabled: view === 'member',
    });

    const regenerateMutation = useMutation({
        mutationFn: (payload: { report_type: string; period_start: string }) =>
            fetchJson<CompanyReport>('/okr/company-reports/regenerate', {
                method: 'POST',
                body: JSON.stringify(payload),
            }),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['company-reports'] });
        },
    });

    useEffect(() => {
        if (!companyReports.length) {
            setExpandedCompanyReportId(null);
            return;
        }
        if (!expandedCompanyReportId || !companyReports.some(report => report.id === expandedCompanyReportId)) {
            setExpandedCompanyReportId(companyReports[0].id);
        }
    }, [companyReports, expandedCompanyReportId]);

    const filteredMemberReports = useMemo(() => {
        const keyword = memberSearch.trim().toLowerCase();
        if (!keyword) return memberReports;
        return memberReports.filter(report =>
            report.display_name.toLowerCase().includes(keyword)
            || report.group_label.toLowerCase().includes(keyword)
        );
    }, [memberReports, memberSearch]);

    useEffect(() => {
        if (!filteredMemberReports.length) {
            setSelectedMemberReportId(null);
            return;
        }
        if (!selectedMemberReportId || !filteredMemberReports.some(report => report.id === selectedMemberReportId)) {
            setSelectedMemberReportId(filteredMemberReports[0].id);
        }
    }, [filteredMemberReports, selectedMemberReportId]);

    const selectedMemberReport = filteredMemberReports.find(report => report.id === selectedMemberReportId) || filteredMemberReports[0] || null;

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '16px',
                flexWrap: 'wrap',
            }}>
                <div style={{
                    display: 'inline-flex',
                    padding: '4px',
                    borderRadius: '10px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                }}>
                    {[
                        { key: 'company', zh: '公司汇总', en: 'Company Reports' },
                        { key: 'member', zh: '成员日报', en: 'Member Daily Reports' },
                    ].map(item => (
                        <button
                            key={item.key}
                            onClick={() => setView(item.key as 'company' | 'member')}
                            style={{
                                border: 'none',
                                borderRadius: '8px',
                                padding: '8px 14px',
                                background: view === item.key ? 'var(--bg-primary)' : 'transparent',
                                color: view === item.key ? 'var(--text-primary)' : 'var(--text-secondary)',
                                fontSize: '13px',
                                fontWeight: 600,
                                cursor: 'pointer',
                                boxShadow: view === item.key ? '0 1px 4px rgba(0,0,0,0.06)' : 'none',
                            }}
                        >
                            {isChinese ? item.zh : item.en}
                        </button>
                    ))}
                </div>

                {view === 'company' ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                        <div style={{ display: 'inline-flex', gap: '6px' }}>
                            {[
                                { key: 'daily', zh: '日报', en: 'Daily' },
                                { key: 'weekly', zh: '周报', en: 'Weekly' },
                                { key: 'monthly', zh: '月报', en: 'Monthly' },
                            ].map(item => (
                                <button
                                    key={item.key}
                                    onClick={() => setReportType(item.key as 'daily' | 'weekly' | 'monthly')}
                                    style={{
                                        border: `1px solid ${reportType === item.key ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                                        borderRadius: '8px',
                                        padding: '7px 12px',
                                        background: reportType === item.key ? 'rgba(99,102,241,0.08)' : 'var(--bg-primary)',
                                        color: reportType === item.key ? 'var(--accent-primary)' : 'var(--text-secondary)',
                                        fontSize: '12px',
                                        fontWeight: 600,
                                        cursor: 'pointer',
                                    }}
                                >
                                    {isChinese ? item.zh : item.en}
                                </button>
                            ))}
                        </div>
                    </div>
                ) : (
                    <input
                        type="date"
                        className="form-input"
                        value={selectedDate}
                        onChange={e => setSelectedDate(e.target.value)}
                        style={{ width: '180px' }}
                    />
                )}
            </div>

            {view === 'company' ? (
                companyLoading ? (
                    <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                        {isChinese ? '加载中...' : 'Loading...'}
                    </div>
                ) : companyReports.length ? (
                    <div style={{
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '16px',
                    }}>
                        {companyReports.map(report => {
                            const expanded = expandedCompanyReportId === report.id;
                            const showMissing = report.missing_count > 0;
                            const showRefresh = report.needs_refresh && isAdmin;
                            return (
                                <div
                                    key={report.id}
                                    style={{
                                        background: 'var(--bg-primary)',
                                        border: `1px solid ${expanded ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                                        borderRadius: '12px',
                                        overflow: 'hidden',
                                    }}
                                >
                                    <button
                                        onClick={() => setExpandedCompanyReportId(report.id)}
                                        style={{
                                            width: '100%',
                                            textAlign: 'left',
                                            background: 'transparent',
                                            border: 'none',
                                            padding: '16px 18px',
                                            cursor: 'pointer',
                                            display: 'flex',
                                            alignItems: 'center',
                                            justifyContent: 'space-between',
                                            gap: '16px',
                                        }}
                                    >
                                        <div style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)' }}>
                                            {report.period_label}
                                        </div>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                                            {showMissing && (
                                                <span style={{
                                                    fontSize: '11px',
                                                    fontWeight: 600,
                                                    color: '#b45309',
                                                    background: 'rgba(245,158,11,0.12)',
                                                    border: '1px solid rgba(245,158,11,0.25)',
                                                    padding: '2px 8px',
                                                    borderRadius: '999px',
                                                }}>
                                                    {isChinese ? `${report.missing_count} 人缺交` : `${report.missing_count} missing`}
                                                </span>
                                            )}
                                            {report.needs_refresh && (
                                                <span style={{
                                                    fontSize: '11px',
                                                    fontWeight: 600,
                                                    color: '#a16207',
                                                    background: 'rgba(245,158,11,0.10)',
                                                    border: '1px solid rgba(245,158,11,0.22)',
                                                    padding: '2px 8px',
                                                    borderRadius: '999px',
                                                }}>
                                                    {isChinese ? '有补交更新' : 'Updated submissions'}
                                                </span>
                                            )}
                                            <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                                {expanded ? '−' : '+'}
                                            </span>
                                        </div>
                                    </button>

                                    {expanded && (
                                        <div style={{
                                            padding: '0 18px 18px',
                                            borderTop: '1px solid var(--border-subtle)',
                                            display: 'flex',
                                            flexDirection: 'column',
                                            gap: '12px',
                                        }}>
                                            {showRefresh && (
                                                <div style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: '12px' }}>
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            regenerateMutation.mutate({
                                                                report_type: report.report_type,
                                                                period_start: report.period_start,
                                                            });
                                                        }}
                                                        disabled={regenerateMutation.isPending}
                                                        className="btn btn-secondary"
                                                        style={{ fontSize: '12px' }}
                                                    >
                                                        {regenerateMutation.isPending
                                                            ? (isChinese ? '重新汇总中...' : 'Regenerating...')
                                                            : (isChinese ? '重新汇总' : 'Regenerate')}
                                                    </button>
                                                </div>
                                            )}

                                            <pre style={{
                                                margin: 0,
                                                whiteSpace: 'pre-wrap',
                                                wordBreak: 'break-word',
                                                fontSize: '13px',
                                                lineHeight: '1.7',
                                                color: 'var(--text-secondary)',
                                                fontFamily: 'inherit',
                                            }}>
                                                {report.content}
                                            </pre>
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                ) : (
                    <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '13px', background: 'var(--bg-secondary)', borderRadius: '12px' }}>
                        {isChinese ? '暂无公司级汇总报告。' : 'No company reports yet.'}
                    </div>
                )
            ) : memberLoading ? (
                <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                    {isChinese ? '加载中...' : 'Loading...'}
                </div>
            ) : (
                <div style={{
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '12px',
                    overflow: 'hidden',
                    background: 'var(--bg-primary)',
                }}>
                    <div style={{
                        display: 'grid',
                        gridTemplateColumns: '220px minmax(0, 1fr)',
                        minHeight: '480px',
                    }}>
                        <div style={{
                            borderRight: '1px solid var(--border-subtle)',
                            background: 'var(--bg-secondary)',
                            padding: '12px',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '8px',
                        }}>
                            <input
                                type="text"
                                className="form-input"
                                value={memberSearch}
                                onChange={e => setMemberSearch(e.target.value)}
                                placeholder={isChinese ? '搜索成员...' : 'Search members...'}
                                style={{ width: '100%' }}
                            />
                            {filteredMemberReports.length ? filteredMemberReports.map(item => (
                                <div
                                    key={item.id}
                                    onClick={() => setSelectedMemberReportId(item.id)}
                                    style={{
                                        padding: '10px 12px',
                                        borderRadius: '8px',
                                        border: `1px solid ${selectedMemberReport?.id === item.id ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                                        background: selectedMemberReport?.id === item.id ? 'rgba(99,102,241,0.06)' : 'var(--bg-primary)',
                                        cursor: 'pointer',
                                    }}
                                >
                                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                                        {item.display_name}
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                                        {item.group_label}
                                    </div>
                                    <div style={{ marginTop: '6px' }}>
                                        <span style={{
                                            fontSize: '10px',
                                            fontWeight: 600,
                                            color: item.status === 'missing' ? '#b45309' : 'var(--accent-primary)',
                                            background: item.status === 'missing' ? 'rgba(245,158,11,0.12)' : 'rgba(99,102,241,0.08)',
                                            borderRadius: '999px',
                                            padding: '2px 6px',
                                        }}>
                                            {item.status === 'missing'
                                                ? (isChinese ? '缺交' : 'Missing')
                                                : item.status === 'late'
                                                    ? (isChinese ? '补交' : 'Late')
                                                    : item.status === 'revised'
                                                        ? (isChinese ? '已修改' : 'Revised')
                                                        : (isChinese ? '已提交' : 'Submitted')}
                                        </span>
                                    </div>
                                </div>
                            )) : (
                                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px 4px' }}>
                                    {isChinese ? '没有匹配的成员。' : 'No matching members.'}
                                </div>
                            )}
                        </div>

                        <div style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px', minWidth: 0 }}>
                            {selectedMemberReport ? (
                                <div
                                    key={`content-${selectedMemberReport.id}`}
                                    style={{
                                        padding: '14px 16px',
                                        border: '1px solid var(--border-subtle)',
                                        borderRadius: '10px',
                                        background: 'var(--bg-secondary)',
                                        minWidth: 0,
                                    }}
                                >
                                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', marginBottom: '8px', flexWrap: 'wrap' }}>
                                        <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                                            {selectedMemberReport.display_name}
                                        </div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {selectedMemberReport.updated_at ? new Date(selectedMemberReport.updated_at).toLocaleString() : ''}
                                        </div>
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                        {selectedMemberReport.group_label}
                                    </div>
                                    <div style={{
                                        fontSize: '13px',
                                        lineHeight: '1.7',
                                        color: selectedMemberReport.content ? 'var(--text-secondary)' : 'var(--text-tertiary)',
                                        whiteSpace: 'pre-wrap',
                                        wordBreak: 'break-word',
                                    }}>
                                        {selectedMemberReport.content || (isChinese ? '当天暂无日报提交。' : 'No daily report submitted for this day.')}
                                    </div>
                                </div>
                            ) : (
                                <div style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>
                                    {isChinese ? '当天暂无成员日报。' : 'No member daily reports for this day.'}
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
