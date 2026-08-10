import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
    IconChevronLeft,
    IconChevronRight,
    IconCopy,
    IconExternalLink,
    IconEye,
    IconHistory,
    IconLock,
    IconSettings,
    IconUsers,
    IconWorld,
    IconX,
} from '@tabler/icons-react';

import OrgMemberAccessPicker, { type AgentAccessUser } from '../components/OrgMemberAccessPicker';
import { useToast } from '../components/Toast/ToastProvider';
import { fetchJson } from '../services/api';
import { copyToClipboard } from '../utils/clipboard';
import './PublishedPages.css';

type AccessMode = 'public' | 'authenticated' | 'restricted';
type AccessUser = {
    id: string;
    display_name: string;
    email?: string;
    status: 'pending' | 'approved' | 'rejected';
    requested_at?: string;
};
type Visitor = {
    id: string;
    display_name: string;
    email?: string;
    visitor_type: 'authenticated' | 'anonymous';
    view_count: number;
    first_viewed_at?: string;
    last_viewed_at?: string;
};
type PublishedPage = {
    id: string;
    short_id: string;
    agent_id: string;
    title: string;
    source_path: string;
    agent_name: string;
    access_mode: AccessMode;
    view_count: number;
    url: string;
    created_at?: string;
    visitor_count: number;
    pending_request_count: number;
    visitors: Visitor[];
};
type PublishedPageDetail = Omit<PublishedPage, 'visitors'> & { access_users: AccessUser[] };
type Paged<T> = { items: T[]; total: number; page: number; page_size: number };

const PAGE_SIZE = 20;
const modeLabels: Record<AccessMode, string> = {
    public: '公开', authenticated: '仅登录', restricted: '指定人员',
};

function formatTime(value?: string) {
    return value ? new Date(value).toLocaleString() : '—';
}

function absolutePageUrl(url: string) {
    try {
        return new URL(url, window.location.origin).toString();
    } catch {
        return url;
    }
}

export default function PublishedPages() {
    const queryClient = useQueryClient();
    const toast = useToast();
    const [searchParams, setSearchParams] = useSearchParams();
    const selectedPageId = searchParams.get('page');
    const agentId = searchParams.get('agent_id') || '';
    const pageNo = Math.max(1, Number(searchParams.get('page_no')) || 1);
    const [activeTab, setActiveTab] = useState<'permissions' | 'visitors'>('permissions');
    const [visitorPage, setVisitorPage] = useState(1);
    const [mode, setMode] = useState<AccessMode>('public');
    const [selectedPeople, setSelectedPeople] = useState<AgentAccessUser[]>([]);
    const [showMemberPicker, setShowMemberPicker] = useState(false);
    const [saving, setSaving] = useState(false);

    const listParams = new URLSearchParams({ page: String(pageNo), page_size: String(PAGE_SIZE) });
    if (agentId) listParams.set('agent_id', agentId);
    const { data: pageData, isLoading } = useQuery({
        queryKey: ['published-pages', 'list', pageNo, agentId],
        queryFn: () => fetchJson<Paged<PublishedPage>>(`/pages/mine?${listParams}`),
    });
    const pages = pageData?.items || [];
    const { data: selected } = useQuery({
        queryKey: ['published-pages', 'detail', selectedPageId],
        queryFn: () => fetchJson<PublishedPageDetail>(`/pages/${selectedPageId}/detail`),
        enabled: Boolean(selectedPageId),
    });
    const { data: visitorData, isLoading: visitorsLoading } = useQuery({
        queryKey: ['published-pages', 'visitors', selectedPageId, visitorPage],
        queryFn: () => fetchJson<Paged<Visitor>>(`/pages/${selectedPageId}/visitors?page=${visitorPage}&page_size=${PAGE_SIZE}`),
        enabled: Boolean(selectedPageId) && activeTab === 'visitors',
    });

    useEffect(() => {
        if (!selected) return;
        setMode(selected.access_mode);
        setSelectedPeople(selected.access_users.filter(user => user.status === 'approved').map(user => ({
            id: user.id,
            name: user.display_name,
            email: user.email,
            access_level: 'use',
        })));
        setActiveTab('permissions');
        setVisitorPage(1);
    }, [selectedPageId, selected]);

    const originalApprovedIds = useMemo(
        () => selected?.access_users.filter(user => user.status === 'approved').map(user => user.id).sort() || [],
        [selected],
    );
    const selectedIds = useMemo(() => selectedPeople.map(user => user.id).sort(), [selectedPeople]);
    const dirty = Boolean(selected) && (
        mode !== selected?.access_mode || JSON.stringify(selectedIds) !== JSON.stringify(originalApprovedIds)
    );

    const updateSearch = (updates: Record<string, string | null>) => {
        const next = new URLSearchParams(searchParams);
        Object.entries(updates).forEach(([key, value]) => value === null ? next.delete(key) : next.set(key, value));
        setSearchParams(next);
    };
    const refresh = async () => {
        await queryClient.invalidateQueries({ queryKey: ['published-pages'] });
    };
    const copyPageUrl = async (url: string) => {
        if (await copyToClipboard(absolutePageUrl(url))) toast.success('发布地址已复制');
        else toast.error('复制失败，请手动复制地址');
    };

    const save = async () => {
        if (!selected || !dirty) return;
        setSaving(true);
        try {
            await fetchJson(`/pages/${selected.id}/access`, {
                method: 'PUT',
                body: JSON.stringify({
                    access_mode: mode,
                    allowed_user_ids: mode === 'restricted' ? selectedIds : [],
                }),
            });
            await refresh();
            toast.success('权限设置已保存');
        } catch (error: any) {
            toast.error('权限设置保存失败', { details: error?.message || String(error) });
        } finally {
            setSaving(false);
        }
    };

    const resolveRequest = async (user: AccessUser, status: 'approved' | 'rejected') => {
        if (!selected) return;
        try {
            await fetchJson(`/pages/${selected.id}/requests/${user.id}`, {
                method: 'PUT', body: JSON.stringify({ status }),
            });
            await refresh();
            toast.success(status === 'approved' ? `已允许 ${user.display_name} 访问` : `已拒绝 ${user.display_name} 的访问申请`);
        } catch (error: any) {
            toast.error('申请处理失败', { details: error?.message || String(error) });
        }
    };

    const totalPages = Math.max(1, Math.ceil((pageData?.total || 0) / PAGE_SIZE));
    const visitorPages = Math.max(1, Math.ceil((visitorData?.total || 0) / PAGE_SIZE));
    const pendingUsers = selected?.access_users.filter(user => user.status === 'pending') || [];

    return (
        <div style={{ padding: '28px 32px', maxWidth: 1120, margin: '0 auto' }}>
            <div style={{ marginBottom: 24 }}>
                <h1 style={{ fontSize: 24, margin: 0 }}>发布管理</h1>
                <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>管理 Agent 发布的网页、访问权限和访问记录</p>
            </div>

            {isLoading ? <p>加载中…</p> : pages.length === 0 ? (
                <div style={{ padding: 40, textAlign: 'center', border: '1px solid var(--border-subtle)', borderRadius: 10, color: 'var(--text-tertiary)' }}>
                    暂无已发布内容，可让 Agent 使用 Publish Page 工具发布网页
                </div>
            ) : (
                <>
                    <div style={{ border: '1px solid var(--border-subtle)', borderRadius: 10, overflow: 'hidden' }}>
                        {pages.map((publishedPage, index) => (
                            <div
                                key={publishedPage.id}
                                role="button"
                                tabIndex={0}
                                onClick={() => updateSearch({ page: publishedPage.id })}
                                onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') updateSearch({ page: publishedPage.id }); }}
                                className="published-pages-row"
                                style={{
                                    borderTop: index ? '1px solid var(--border-subtle)' : 0,
                                    background: 'var(--bg-primary)', color: 'inherit', padding: '10px 14px', cursor: 'pointer',
                                    display: 'grid', alignItems: 'center', gap: 14,
                                }}
                            >
                                <div style={{ minWidth: 0 }}>
                                    <div style={{ fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{publishedPage.title || publishedPage.source_path}</div>
                                    <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{publishedPage.agent_name} · {publishedPage.source_path}</div>
                                </div>
                                <div className="published-pages-row__url" style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
                                    <a
                                        href={absolutePageUrl(publishedPage.url)}
                                        target="_blank"
                                        rel="noreferrer"
                                        onClick={event => event.stopPropagation()}
                                        title={absolutePageUrl(publishedPage.url)}
                                        style={{ minWidth: 0, flex: 1, fontSize: 11, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textDecoration: 'none' }}
                                    >{absolutePageUrl(publishedPage.url)}</a>
                                    <button
                                        type="button"
                                        aria-label="复制发布地址"
                                        title="复制发布地址"
                                        onClick={event => { event.stopPropagation(); void copyPageUrl(publishedPage.url); }}
                                        style={{ border: 0, background: 'transparent', color: 'var(--text-tertiary)', padding: 3, cursor: 'pointer', display: 'inline-flex' }}
                                    ><IconCopy size={14} /></button>
                                </div>
                                <span className="published-pages-row__mode" style={{ fontSize: 12 }}>{modeLabels[publishedPage.access_mode]}</span>
                                <span className="published-pages-row__metrics" style={{ fontSize: 12, color: 'var(--text-secondary)', display: 'flex', gap: 12, alignItems: 'center' }}>
                                    <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}><IconEye size={14} /> {publishedPage.view_count}</span>
                                    <span>{publishedPage.visitor_count} 人</span>
                                    {publishedPage.pending_request_count > 0 && (
                                        <span style={{ color: 'var(--warning)' }}>待处理 {publishedPage.pending_request_count}</span>
                                    )}
                                </span>
                            </div>
                        ))}
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 14, color: 'var(--text-secondary)', fontSize: 12 }}>
                        <span>共 {pageData?.total || 0} 条</span>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <button className="btn btn-secondary btn-sm" disabled={pageNo <= 1} onClick={() => updateSearch({ page_no: String(pageNo - 1), page: null })}><IconChevronLeft size={14} /> 上一页</button>
                            <span>{pageNo} / {totalPages}</span>
                            <button className="btn btn-secondary btn-sm" disabled={pageNo >= totalPages} onClick={() => updateSearch({ page_no: String(pageNo + 1), page: null })}>下一页 <IconChevronRight size={14} /></button>
                        </div>
                    </div>
                </>
            )}

            {selected && <div style={{ position: 'fixed', inset: 0, zIndex: 2000, background: 'rgba(0,0,0,.28)' }} onClick={() => updateSearch({ page: null })}>
                <aside style={{ position: 'absolute', right: 0, top: 0, bottom: 0, width: 'min(560px, 94vw)', background: 'var(--bg-primary)', padding: 24, overflowY: 'auto', boxShadow: '-12px 0 40px rgba(0,0,0,.16)' }} onClick={event => event.stopPropagation()}>
                    <button aria-label="关闭" onClick={() => updateSearch({ page: null })} style={{ float: 'right', border: 0, background: 'none', color: 'inherit', cursor: 'pointer' }}><IconX size={20} /></button>
                    <h2 style={{ fontSize: 19, margin: '0 0 10px' }}>{selected.title || selected.source_path}</h2>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px', border: '1px solid var(--border-subtle)', borderRadius: 7, background: 'var(--bg-secondary)' }}>
                        <a
                            href={absolutePageUrl(selected.url)}
                            target="_blank"
                            rel="noreferrer"
                            title={absolutePageUrl(selected.url)}
                            style={{ minWidth: 0, flex: 1, fontSize: 12, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textDecoration: 'none' }}
                        >{absolutePageUrl(selected.url)}</a>
                        <button type="button" aria-label="复制发布地址" title="复制发布地址" onClick={() => void copyPageUrl(selected.url)} style={{ border: 0, background: 'transparent', color: 'var(--text-secondary)', padding: 3, cursor: 'pointer', display: 'inline-flex' }}><IconCopy size={15} /></button>
                        <a href={absolutePageUrl(selected.url)} target="_blank" rel="noreferrer" aria-label="打开发布页面" title="打开发布页面" style={{ color: 'var(--text-secondary)', display: 'inline-flex' }}><IconExternalLink size={15} /></a>
                    </div>

                    <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--border-subtle)', marginTop: 24 }}>
                        {([['permissions', '权限设置', IconSettings], ['visitors', `访问记录 ${selected.visitor_count}`, IconHistory]] as const).map(([value, label, Icon]) => (
                            <button key={value} type="button" onClick={() => setActiveTab(value)} style={{
                                border: 0, borderBottom: activeTab === value ? '2px solid var(--accent-primary)' : '2px solid transparent',
                                background: 'none', color: activeTab === value ? 'var(--text-primary)' : 'var(--text-secondary)',
                                padding: '10px 12px', cursor: 'pointer', display: 'inline-flex', gap: 6, alignItems: 'center', fontWeight: 600,
                            }}><Icon size={15} /> {label}</button>
                        ))}
                    </div>

                    {activeTab === 'permissions' ? <>
                        <div style={{ display: 'grid', gap: 8, marginTop: 18 }}>
                            {([
                                ['public', '公开', '任何人无需登录即可访问', IconWorld],
                                ['authenticated', '仅登录', '公司内已登录用户可访问', IconLock],
                                ['restricted', '指定人员', '仅发布者和指定人员可访问', IconUsers],
                            ] as const).map(([value, title, description, Icon]) => (
                                <label key={value} style={{ padding: 12, border: `1px solid ${mode === value ? 'var(--accent-primary)' : 'var(--border-subtle)'}`, borderRadius: 8, display: 'flex', gap: 10, cursor: 'pointer' }}>
                                    <input type="radio" checked={mode === value} onChange={() => setMode(value)} /><Icon size={18} />
                                    <span><strong style={{ display: 'block', fontSize: 13 }}>{title}</strong><span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{description}</span></span>
                                </label>
                            ))}
                        </div>

                        {mode === 'restricted' && <div style={{ marginTop: 16, padding: 14, border: '1px solid var(--border-subtle)', borderRadius: 8, display: 'flex', alignItems: 'center', gap: 12 }}>
                            <div style={{ flex: 1 }}><strong style={{ fontSize: 13 }}>已选择 {selectedPeople.length} 人</strong><div style={{ color: 'var(--text-tertiary)', fontSize: 11, marginTop: 3 }}>{selectedPeople.slice(0, 3).map(user => user.name).join('、') || '尚未选择人员'}{selectedPeople.length > 3 ? ` 等 ${selectedPeople.length} 人` : ''}</div></div>
                            <button className="btn btn-secondary btn-sm" onClick={() => setShowMemberPicker(true)}>选择可访问人员</button>
                        </div>}

                        {pendingUsers.length > 0 && <section style={{ marginTop: 24 }}>
                            <h3 style={{ fontSize: 14 }}>待处理申请</h3>
                            {pendingUsers.map(user => <div key={user.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 0', borderBottom: '1px solid var(--border-subtle)' }}>
                                <span style={{ flex: 1, fontSize: 13 }}>{user.display_name}{user.email ? ` · ${user.email}` : ''}</span>
                                <button className="btn" onClick={() => void resolveRequest(user, 'rejected')}>拒绝</button>
                                <button className="btn btn-primary" onClick={() => void resolveRequest(user, 'approved')}>允许</button>
                            </div>)}
                        </section>}

                        <div style={{ position: 'sticky', bottom: -24, background: 'var(--bg-primary)', padding: '18px 0 24px', marginTop: 20, textAlign: 'right' }}>
                            <button className="btn btn-primary" disabled={saving || !dirty} onClick={() => void save()}>{saving ? '保存中…' : dirty ? '保存权限' : '已保存'}</button>
                        </div>
                    </> : <section style={{ marginTop: 18 }}>
                        {visitorsLoading ? <p>加载中…</p> : !visitorData?.items.length ? <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>暂无访问记录</p> : visitorData.items.map(visitor => (
                            <div key={visitor.id} style={{ padding: '10px 0', borderBottom: '1px solid var(--border-subtle)', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 10 }}>
                                <div style={{ minWidth: 0 }}>
                                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, minWidth: 0 }}>
                                        <strong title={visitor.display_name} style={{ minWidth: 0, maxWidth: visitor.email ? '42%' : '75%', fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{visitor.display_name}</strong>
                                        {visitor.email && <span title={visitor.email} style={{ minWidth: 0, color: 'var(--text-tertiary)', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{visitor.email}</span>}
                                        {visitor.visitor_type === 'anonymous' && <span style={{ color: 'var(--text-tertiary)', fontSize: 10, flexShrink: 0 }}>未登录</span>}
                                    </div>
                                    <div style={{ color: 'var(--text-tertiary)', fontSize: 11, marginTop: 4 }}>首次：{formatTime(visitor.first_viewed_at)} · 最近：{formatTime(visitor.last_viewed_at)}</div>
                                </div>
                                <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{visitor.view_count} 次</span>
                            </div>
                        ))}
                        {(visitorData?.total || 0) > PAGE_SIZE && <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 8, marginTop: 14, fontSize: 12 }}>
                            <button className="btn btn-secondary btn-sm" disabled={visitorPage <= 1} onClick={() => setVisitorPage(page => page - 1)}>上一页</button>
                            <span>{visitorPage} / {visitorPages}</span>
                            <button className="btn btn-secondary btn-sm" disabled={visitorPage >= visitorPages} onClick={() => setVisitorPage(page => page + 1)}>下一页</button>
                        </div>}
                    </section>}

                    <OrgMemberAccessPicker
                        open={showMemberPicker}
                        agentId={selected.agent_id}
                        directoryBaseUrl={`/pages/${selected.id}/directory`}
                        membersOnly
                        users={selectedPeople}
                        departments={[]}
                        onClose={() => setShowMemberPicker(false)}
                        onSave={async users => setSelectedPeople(users)}
                    />
                </aside>
            </div>}
        </div>
    );
}
