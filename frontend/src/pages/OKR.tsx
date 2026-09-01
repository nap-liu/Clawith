/**
 * OKR Page — Objectives & Key Results dashboard with full editing support.
 *
 * Features:
 *   - Period selector (computed from OKR settings)
 *   - Company-level and member objectives with progress visualization
 *   - Create Objective (admin: company level; users: own level)
 *   - Add Key Result to an objective
 *   - Inline KR progress editing (current_value update)
 *   - KR status manual override (on_track / at_risk / behind / completed)
 *   - Disabled state: guide panel directing to OKR settings
 */

import React, { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { fetchJson } from '../services/api';
import { useAuthStore } from '../stores';
import { CreateObjectiveForm, ObjectiveCard } from './okr/objectiveComponents';
import { MembersWithoutOKRPanel, ReportsTab } from './okr/reportsPanels';
import type {
    OKRSettings,
    Objective,
    Period,
} from './okr/model';

export default function OKR() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const user = useAuthStore(s => s.user);
    const isChinese = i18n.language?.startsWith('zh');
    const isAdmin = user && ['platform_admin', 'org_admin'].includes(user.role);
    const okrRoleMode = isAdmin ? 'admin' : 'member';
    const queryClient = useQueryClient();

    const [selectedPeriod, setSelectedPeriod] = useState<Period | null>(null);
    const [creating, setCreating] = useState(false);
    const [activeTab, setActiveTab] = useState<'dashboards' | 'reports'>('dashboards');
    const [periodMenuOpen, setPeriodMenuOpen] = useState(false);
    const periodMenuRef = useRef<HTMLDivElement | null>(null);

    const { data: settings, isLoading: settingsLoading } = useQuery<OKRSettings>({
        queryKey: ['okr-settings'],
        queryFn: () => fetchJson<OKRSettings>('/okr/settings'),
        staleTime: 0,
        refetchOnWindowFocus: true,
    });

    const { data: periods = [] } = useQuery<Period[]>({
        queryKey: ['okr-periods'],
        queryFn: () => fetchJson<Period[]>('/okr/periods'),
        enabled: !!settings?.enabled,
    });

    useEffect(() => {
        if (periods.length === 0) return;
        const selectedStillExists = selectedPeriod
            ? periods.find(p => p.start === selectedPeriod.start && p.end === selectedPeriod.end)
            : null;
        if (!selectedPeriod || !selectedStillExists) {
            const current = periods.find(p => p.is_current) ?? periods[periods.length - 1];
            setSelectedPeriod(current);
        } else if (selectedStillExists !== selectedPeriod) {
            setSelectedPeriod(selectedStillExists);
        }
    }, [periods, selectedPeriod]);

    useEffect(() => {
        if (!periodMenuOpen) return;
        function handlePointerDown(event: MouseEvent) {
            if (periodMenuRef.current && !periodMenuRef.current.contains(event.target as Node)) {
                setPeriodMenuOpen(false);
            }
        }
        document.addEventListener('mousedown', handlePointerDown);
        return () => document.removeEventListener('mousedown', handlePointerDown);
    }, [periodMenuOpen]);

    useEffect(() => {
        if (settings && !settings.daily_report_enabled) {
            setActiveTab('dashboards');
        }
    }, [settings?.daily_report_enabled]);

    const { data: objectives = [], isLoading: objLoading } = useQuery<Objective[]>({
        queryKey: ['okr-objectives', selectedPeriod?.start, selectedPeriod?.end],
        queryFn: () => fetchJson<Objective[]>(
            `/okr/objectives?period_start=${selectedPeriod!.start}&period_end=${selectedPeriod!.end}`
        ),
        enabled: !!settings?.enabled && !!selectedPeriod,
        staleTime: 0,
        refetchOnWindowFocus: true,
    });

    function invalidateObjectives() {
        queryClient.invalidateQueries({ queryKey: ['okr-objectives'] });
    }

    if (settingsLoading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh', color: 'var(--text-tertiary)' }}>
                {isChinese ? '加载中...' : 'Loading...'}
            </div>
        );
    }

    if (!settings?.enabled) {
        return (
            <div style={{
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                height: '70vh', gap: '16px', color: 'var(--text-secondary)', textAlign: 'center', padding: '24px',
            }}>
                <div style={{
                    width: 64, height: 64, borderRadius: '50%',
                    background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--text-tertiary)',
                }}>
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <circle cx="12" cy="12" r="10" />
                        <circle cx="12" cy="12" r="6" />
                        <circle cx="12" cy="12" r="2" />
                    </svg>
                </div>
                <div>
                    <div style={{ fontSize: '18px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '8px' }}>
                        {isChinese ? 'OKR 功能尚未开启' : 'OKR is not enabled'}
                    </div>
                    <div style={{ fontSize: '13px', color: 'var(--text-tertiary)', maxWidth: 400 }}>
                        {isChinese
                            ? 'OKR 系统可以帮助团队设定目标、跟踪进度，并通过 OKR Agent 自动收集工作汇报。'
                            : 'The OKR system helps your team set objectives, track progress, and automatically collect work reports via the OKR Agent.'}
                    </div>
                </div>
                {isAdmin && (
                    <button
                        id="okr-enable-btn"
                        className="btn btn-primary"
                        onClick={() => navigate('/enterprise#okr')}
                        style={{ padding: '8px 20px', fontSize: '13px' }}
                    >
                        {isChinese ? '前往公司设置开启 OKR' : 'Enable OKR in Company Settings'}
                    </button>
                )}
                {!isAdmin && (
                    <div style={{ fontSize: '12px', color: 'var(--text-quaternary)' }}>
                        {isChinese ? '请联系管理员开启 OKR 功能' : 'Please ask an admin to enable OKR'}
                    </div>
                )}
            </div>
        );
    }

    const companyObjs = objectives.filter(o => !o.user_id && !o.agent_id);
    const memberObjs = objectives.filter(o => o.user_id || o.agent_id);

    const memberGroups: Record<string, { label: string; objs: Objective[] }> = {};
    for (const obj of memberObjs) {
        const key = obj.user_id ? `user:${obj.user_id}` : `agent:${obj.agent_id ?? ''}`;
        if (!memberGroups[key]) {
            const label = obj.owner_name || '?';
            memberGroups[key] = { label, objs: [] };
        }
        memberGroups[key].objs.push(obj);
    }
    const periodOptions = periods;

    return (
        <div data-okr-role-mode={okrRoleMode} style={{ padding: '24px', maxWidth: 960, margin: '0 auto' }}>
            <div style={{
                display: 'grid',
                gridTemplateColumns: settings?.daily_report_enabled
                    ? 'minmax(0, 1fr) auto minmax(0, 1fr)'
                    : '1fr auto',
                alignItems: 'center',
                marginBottom: '24px',
                gap: '12px',
            }}>
                <div>
                    <h1 style={{ margin: 0, fontSize: '20px', fontWeight: 700, color: 'var(--text-primary)' }}>
                        {t('okr.title', 'OKR')}
                    </h1>
                    <div style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        {isChinese ? '目标与关键成果' : 'Objectives & Key Results'}
                    </div>
                </div>

                {settings?.daily_report_enabled && (
                    <div style={{
                        display: 'flex',
                        alignItems: 'center',
                        height: 38,
                        background: 'var(--bg-secondary)',
                        padding: '2px',
                        borderRadius: '8px',
                        justifySelf: 'center',
                        alignSelf: 'start',
                        marginTop: '2px',
                    }}>
                        <button
                            onClick={() => setActiveTab('dashboards')}
                            style={{
                                padding: '6px 16px', borderRadius: '6px', fontSize: '13px', fontWeight: 500,
                                background: activeTab === 'dashboards' ? 'var(--bg-primary)' : 'transparent',
                                color: activeTab === 'dashboards' ? 'var(--text-primary)' : 'var(--text-secondary)',
                                boxShadow: activeTab === 'dashboards' ? '0 1px 2px rgba(0,0,0,0.05)' : 'none',
                                border: 'none', cursor: 'pointer', transition: 'all 0.15s',
                            }}
                        >
                            {isChinese ? '概览' : 'Dashboard'}
                        </button>
                        <button
                            onClick={() => setActiveTab('reports')}
                            style={{
                                padding: '6px 16px', borderRadius: '6px', fontSize: '13px', fontWeight: 500,
                                background: activeTab === 'reports' ? 'var(--bg-primary)' : 'transparent',
                                color: activeTab === 'reports' ? 'var(--text-primary)' : 'var(--text-secondary)',
                                boxShadow: activeTab === 'reports' ? '0 1px 2px rgba(0,0,0,0.05)' : 'none',
                                border: 'none', cursor: 'pointer', transition: 'all 0.15s',
                            }}
                        >
                            {isChinese ? '工作汇报' : 'Reports'}
                        </button>
                    </div>
                )}

                <div style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'flex-end',
                    gap: '12px',
                    flexWrap: 'wrap',
                    minHeight: 38,
                    alignSelf: 'start',
                }}>
                    {activeTab === 'dashboards' && (
                        <>
                            {periods.length > 0 && (
                                <div ref={periodMenuRef} style={{ display: 'flex', alignItems: 'center', gap: '8px', position: 'relative' }}>
                                    <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
                                        {isChinese ? '周期' : 'Period'}
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() => setPeriodMenuOpen(v => !v)}
                                        style={{
                                            minWidth: 170, height: 34, padding: '5px 10px', borderRadius: '6px',
                                            border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)',
                                            color: 'var(--text-secondary)', fontSize: '12px', display: 'flex',
                                            alignItems: 'center', justifyContent: 'space-between', gap: '10px', cursor: 'pointer',
                                        }}
                                    >
                                        <span>
                                            {selectedPeriod?.label}
                                            {selectedPeriod?.is_current ? (isChinese ? '（当前）' : ' (now)') : ''}
                                        </span>
                                        <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>▾</span>
                                    </button>
                                    {periodMenuOpen && (
                                        <div
                                            style={{
                                                position: 'absolute', top: 'calc(100% + 6px)', right: 0, zIndex: 50,
                                                minWidth: 210, maxHeight: 280, overflowY: 'auto', padding: '6px',
                                                borderRadius: '8px', border: '1px solid var(--border-subtle)',
                                                background: 'var(--bg-primary)', boxShadow: '0 12px 32px rgba(0,0,0,0.16)',
                                            }}
                                        >
                                            {periodOptions.map(p => {
                                                const active = selectedPeriod?.start === p.start && selectedPeriod?.end === p.end;
                                                return (
                                                    <button
                                                        key={p.start}
                                                        type="button"
                                                        onClick={() => {
                                                            setSelectedPeriod(p);
                                                            setPeriodMenuOpen(false);
                                                        }}
                                                        style={{
                                                            width: '100%', padding: '8px 10px', borderRadius: '6px', border: 'none',
                                                            background: active ? 'var(--accent-primary)' : 'transparent',
                                                            color: active ? '#fff' : 'var(--text-secondary)', fontSize: '12px',
                                                            textAlign: 'left', cursor: 'pointer', display: 'flex',
                                                            justifyContent: 'space-between', alignItems: 'center', gap: '12px',
                                                        }}
                                                    >
                                                        <span>{p.label}{p.is_current ? (isChinese ? '（当前）' : ' (now)') : ''}</span>
                                                        {active && <span>✓</span>}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            )}

                            {isAdmin && selectedPeriod && !creating && (
                                <button
                                    id="create-objective-btn"
                                    onClick={() => setCreating(true)}
                                    style={{
                                        display: 'flex', alignItems: 'center', gap: '6px',
                                        padding: '7px 14px', borderRadius: '6px',
                                        border: 'none', background: 'var(--accent-primary)',
                                        color: '#fff', fontSize: '13px', cursor: 'pointer',
                                        fontWeight: 500, transition: 'opacity 0.15s',
                                    }}
                                    onMouseEnter={e => (e.currentTarget as HTMLButtonElement).style.opacity = '0.85'}
                                    onMouseLeave={e => (e.currentTarget as HTMLButtonElement).style.opacity = '1'}
                                >
                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                                    {isChinese ? '新建目标' : 'New Objective'}
                                </button>
                            )}
                        </>
                    )}
                </div>
            </div>

            {activeTab === 'dashboards' && (
                <>
                    {creating && selectedPeriod && (
                        <div style={{ marginBottom: '24px' }}>
                            <CreateObjectiveForm
                                isChinese={isChinese}
                                isAdmin={!!isAdmin}
                                userId={user?.id ?? ''}
                                selectedPeriod={selectedPeriod}
                                onCreated={() => { setCreating(false); invalidateObjectives(); }}
                                onCancel={() => setCreating(false)}
                            />
                        </div>
                    )}

                    {objLoading && (
                        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                            {isChinese ? '加载中...' : 'Loading...'}
                        </div>
                    )}

                    {!objLoading && objectives.length === 0 && (
                        <div style={{
                            textAlign: 'center', padding: '60px 24px',
                            border: '1px dashed var(--border-subtle)', borderRadius: '12px',
                            color: 'var(--text-tertiary)', fontSize: '13px',
                        }}>
                            {isChinese
                                ? (isAdmin
                                    ? '当前周期暂无 OKR。点击右上角「新建目标」或联系 OKR Agent 来设定目标。'
                                    : '当前周期暂无 OKR。请联系 OKR Agent 来设定目标。')
                                : (isAdmin
                                    ? 'No OKRs for this period yet. Click "New Objective" or ask the OKR Agent.'
                                    : 'No OKRs for this period yet. Please ask the OKR Agent to set them up.')}
                        </div>
                    )}

                    {!objLoading && companyObjs.length > 0 && (
                        <section style={{ marginBottom: '32px' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                                <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.8px' }}>
                                    {t('okr.companyObjectives', isChinese ? '公司目标' : 'Company Objectives')}
                                </span>
                                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                                <span style={{ fontSize: '11px', color: 'var(--text-quaternary)' }}>{companyObjs.length}</span>
                            </div>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                {companyObjs.map(obj => (
                                    <ObjectiveCard
                                        key={obj.id}
                                        obj={obj}
                                        isChinese={isChinese}
                                        canEdit={!!isAdmin}
                                        onInvalidate={invalidateObjectives}
                                        onDelete={async (id) => {
                                            await fetchJson(`/okr/objectives/${id}`, { method: 'DELETE' });
                                            invalidateObjectives();
                                        }}
                                    />
                                ))}
                            </div>
                        </section>
                    )}

                    {!objLoading && Object.keys(memberGroups).length > 0 && (
                        <section>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
                                <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.8px' }}>
                                    {t('okr.memberObjectives', isChinese ? '成员目标' : 'Member Objectives')}
                                </span>
                                <div style={{ flex: 1, height: '1px', background: 'var(--border-subtle)' }} />
                                <span style={{ fontSize: '11px', color: 'var(--text-quaternary)' }}>{memberObjs.length}</span>
                            </div>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                                {Object.entries(memberGroups).map(([ownerKey, group]) => (
                                    <div
                                        key={ownerKey}
                                        style={{
                                            border: '1px solid var(--border-subtle)',
                                            borderRadius: '14px',
                                            background: 'var(--bg-primary)',
                                            padding: '14px',
                                        }}
                                    >
                                        <div style={{
                                            display: 'flex',
                                            alignItems: 'center',
                                            justifyContent: 'space-between',
                                            gap: '12px',
                                            marginBottom: '12px',
                                        }}>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                                                <div style={{
                                                    width: '28px',
                                                    height: '28px',
                                                    borderRadius: '999px',
                                                    background: 'var(--bg-secondary)',
                                                    border: '1px solid var(--border-subtle)',
                                                    color: 'var(--text-secondary)',
                                                    display: 'flex',
                                                    alignItems: 'center',
                                                    justifyContent: 'center',
                                                    fontSize: '13px',
                                                    fontWeight: 700,
                                                    flexShrink: 0,
                                                }}>
                                                    {group.label.slice(0, 1).toUpperCase()}
                                                </div>
                                                <div style={{
                                                    fontSize: '14px',
                                                    fontWeight: 700,
                                                    color: 'var(--text-primary)',
                                                    minWidth: 0,
                                                }}>
                                                    {group.label}
                                                </div>
                                                <span style={{
                                                    fontSize: '11px',
                                                    color: 'var(--text-tertiary)',
                                                    border: '1px solid var(--border-subtle)',
                                                    borderRadius: '999px',
                                                    padding: '2px 8px',
                                                    whiteSpace: 'nowrap',
                                                }}>
                                                    {group.objs.length} {isChinese ? '个目标' : 'objectives'}
                                                </span>
                                            </div>
                                        </div>

                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                            {group.objs.map(obj => (
                                                <ObjectiveCard
                                                    key={obj.id}
                                                    obj={obj}
                                                    isChinese={isChinese}
                                                    canEdit={!!isAdmin}
                                                    onInvalidate={invalidateObjectives}
                                                    onDelete={async (id) => {
                                                        await fetchJson(`/okr/objectives/${id}`, { method: 'DELETE' });
                                                        invalidateObjectives();
                                                    }}
                                                />
                                            ))}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </section>
                    )}

                    {isAdmin && selectedPeriod && (
                        <MembersWithoutOKRPanel
                            isChinese={isChinese}
                            periodStart={selectedPeriod.start}
                            periodEnd={selectedPeriod.end}
                        />
                    )}
                </>
            )}

            {settings?.daily_report_enabled && activeTab === 'reports' && (
                <ReportsTab isChinese={isChinese} />
            )}
        </div>
    );
}
