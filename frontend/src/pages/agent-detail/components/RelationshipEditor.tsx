import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { fetchAuth } from '../utils/fetchAuth';
import { getAgentRelationOptions, getRelationOptions } from '../shared';
import OrgMemberIdentitySummary from '../../../components/OrgMemberIdentitySummary';

export default function RelationshipEditor({ agentId, readOnly = false }: { agentId: string; readOnly?: boolean }) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const humanSearchRef = useRef<HTMLDivElement>(null);
    const agentSearchRef = useRef<HTMLDivElement>(null);
    const getHumanMemberSourceLabel = useCallback((member: any) => {
        const providerName = (member?.provider_name || '').trim();
        const providerType = (member?.provider_type || '').trim().toLowerCase();
        if (!providerName || providerType === 'platform' || providerType === 'web' || providerName.toLowerCase() === 'web') {
            return isChinese ? '平台用户' : 'Platform User';
        }
        return providerName;
    }, [isChinese]);

    const renderHumanMemberSourceBadge = useCallback((member: any) => {
        const providerName = (member?.provider_name || '').trim();
        const providerType = (member?.provider_type || '').trim().toLowerCase();
        const isPlatformUser = !providerName || providerType === 'platform' || providerType === 'web' || providerName.toLowerCase() === 'web';
        const showPlatformBadge = Boolean(member?.is_platform_user) && !isPlatformUser;
        const badgeStyle = (platform: boolean): React.CSSProperties => ({
            display: 'inline-flex',
            alignItems: 'center',
            padding: '1px 6px',
            borderRadius: '999px',
            fontSize: '10px',
            fontWeight: 600,
            marginRight: '6px',
            background: platform ? 'rgba(99,102,241,0.10)' : 'rgba(16,185,129,0.10)',
            color: platform ? 'rgb(79,70,229)' : 'rgb(16,185,129)',
            border: platform ? '1px solid rgba(99,102,241,0.18)' : '1px solid rgba(16,185,129,0.18)',
        });
        return (
            <>
                <span style={badgeStyle(isPlatformUser)}>
                    {getHumanMemberSourceLabel(member)}
                </span>
                {showPlatformBadge && (
                    <span style={badgeStyle(true)}>
                        {isChinese ? '平台用户' : 'Platform User'}
                    </span>
                )}
            </>
        );
    }, [getHumanMemberSourceLabel, isChinese]);

    const getRestrictedTitle = useCallback((reason?: string | null) => {
        const reasonText = reason ? ` (${reason})` : '';
        return isChinese
            ? `当关系目标不存在、停用/过期，或当前访问权限不再允许这个 Agent 与该用户/Agent 互动时，会显示为 restricted。关系记录会保留，但运行时不会使用。${reasonText}`
            : `Restricted means the target is missing, inactive/expired, or current access permissions no longer allow this agent to interact with that user/agent. The record is kept, but runtime use is blocked.${reasonText}`;
    }, [isChinese]);

    const [restrictedTooltip, setRestrictedTooltip] = useState<{ text: string; x: number; y: number } | null>(null);
    const showRestrictedTooltip = useCallback((event: React.SyntheticEvent<HTMLElement>, reason?: string | null) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const tooltipWidth = Math.min(320, Math.max(220, window.innerWidth - 32));
        const x = Math.min(
            Math.max(rect.left + rect.width / 2, 16 + tooltipWidth / 2),
            window.innerWidth - 16 - tooltipWidth / 2,
        );
        setRestrictedTooltip({
            text: getRestrictedTitle(reason),
            x,
            y: rect.top - 8,
        });
    }, [getRestrictedTitle]);
    const hideRestrictedTooltip = useCallback(() => setRestrictedTooltip(null), []);

    const [search, setSearch] = useState('');
    const [showHumanForm, setShowHumanForm] = useState(false);
    const [searchResults, setSearchResults] = useState<any[]>([]);
    const [showMemberDropdown, setShowMemberDropdown] = useState(false);
    const [selectedMembers, setSelectedMembers] = useState<any[]>([]);
    const [relation, setRelation] = useState('collaborator');
    const [description, setDescription] = useState('');
    const [agentSearch, setAgentSearch] = useState('');
    const [showAgentForm, setShowAgentForm] = useState(false);
    const [agentSearchResults, setAgentSearchResults] = useState<any[]>([]);
    const [showAgentDropdown, setShowAgentDropdown] = useState(false);
    const [selectedAgents, setSelectedAgents] = useState<any[]>([]);
    const [agentRelation, setAgentRelation] = useState('collaborator');
    const [agentDescription, setAgentDescription] = useState('');
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editRelation, setEditRelation] = useState('');
    const [editDescription, setEditDescription] = useState('');
    const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
    const [editAgentRelation, setEditAgentRelation] = useState('');
    const [editAgentDescription, setEditAgentDescription] = useState('');
    const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

    const { data: relationships = [], refetch } = useQuery({
        queryKey: ['relationships', agentId],
        queryFn: () => fetchAuth<any[]>(`/agents/${agentId}/relationships/`),
    });
    const { data: agentRelationships = [], refetch: refetchAgentRels } = useQuery({
        queryKey: ['agent-relationships', agentId],
        queryFn: () => fetchAuth<any[]>(`/agents/${agentId}/relationships/agents`),
    });

    const relatedMemberIds = useMemo(() => new Set(relationships.map((r: any) => r.user_id)), [relationships]);
    const relatedAgentIds = useMemo(() => new Set(agentRelationships.map((r: any) => r.agent_id)), [agentRelationships]);
    const selectedMemberIds = useMemo(() => new Set(selectedMembers.map((m: any) => m.user_id)), [selectedMembers]);
    const selectedAgentIds = useMemo(() => new Set(selectedAgents.map((a: any) => a.agent_id)), [selectedAgents]);
    const relatedMemberById = useMemo(() => {
        const map = new Map<string, any>();
        relationships.forEach((r: any) => {
            if (r.user_id) map.set(r.user_id, r);
        });
        return map;
    }, [relationships]);

    const visibleMemberResults = useMemo(
        () => searchResults,
        [searchResults],
    );
    const visibleAgentResults = useMemo(
        () => agentSearchResults.filter((a: any) => !relatedAgentIds.has(a.agent_id)),
        [agentSearchResults, relatedAgentIds],
    );

    const loadOrgMembers = async (keyword = '') => {
        const query = keyword.trim() ? `?search=${encodeURIComponent(keyword.trim())}` : '';
        const results = await fetchAuth<any[]>(`/agents/${agentId}/relationships/member-candidates${query}`);
        setSearchResults(results);
    };

    const loadAgentCandidates = async (keyword = '') => {
        const query = keyword.trim() ? `?search=${encodeURIComponent(keyword.trim())}` : '';
        const results = await fetchAuth<any[]>(`/agents/${agentId}/relationships/agent-candidates${query}`);
        setAgentSearchResults(results);
    };

    useEffect(() => {
        if (!search || search.length < 1) { setSearchResults([]); return; }
        const timer = setTimeout(() => {
            loadOrgMembers(search);
        }, 300);
        return () => clearTimeout(timer);
    }, [search]);

    useEffect(() => {
        if (!agentSearch || agentSearch.length < 1) { setAgentSearchResults([]); return; }
        const timer = setTimeout(() => {
            loadAgentCandidates(agentSearch);
        }, 300);
        return () => clearTimeout(timer);
    }, [agentId, agentSearch]);

    useEffect(() => {
        const handleClickOutside = (e: MouseEvent) => {
            const target = e.target as Node;
            if (showMemberDropdown && humanSearchRef.current && !humanSearchRef.current.contains(target)) {
                setShowMemberDropdown(false);
            }
            if (showAgentDropdown && agentSearchRef.current && !agentSearchRef.current.contains(target)) {
                setShowAgentDropdown(false);
            }
        };
        if (showMemberDropdown || showAgentDropdown) {
            document.addEventListener('mousedown', handleClickOutside);
        }
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [showMemberDropdown, showAgentDropdown]);

    const resetHumanDraft = () => {
        setShowHumanForm(false);
        setSearch('');
        setSearchResults([]);
        setShowMemberDropdown(false);
        setSelectedMembers([]);
        setRelation('collaborator');
        setDescription('');
    };

    const resetAgentDraft = () => {
        setShowAgentForm(false);
        setAgentSearch('');
        setAgentSearchResults([]);
        setShowAgentDropdown(false);
        setSelectedAgents([]);
        setAgentRelation('collaborator');
        setAgentDescription('');
    };

    const toggleMemberSelection = (member: any) => {
        setSelectedMembers(prev =>
            prev.some((item: any) => item.user_id === member.user_id)
                ? prev.filter((item: any) => item.user_id !== member.user_id)
                : [...prev, member]
        );
    };

    const toggleAgentSelection = (agent: any) => {
        setSelectedAgents(prev =>
            prev.some((item: any) => item.agent_id === agent.agent_id)
                ? prev.filter((item: any) => item.agent_id !== agent.agent_id)
                : [...prev, agent]
        );
    };

    const addRelationship = async () => {
        if (!selectedMembers.length) return;
        const existing = new Map(
            relationships.map((r: any) => [r.user_id, { user_id: r.user_id, relation: r.relation, description: r.description }])
        );
        selectedMembers.forEach((member: any) => {
            existing.set(member.user_id, { user_id: member.user_id, relation, description });
        });
        await fetchAuth(`/agents/${agentId}/relationships/`, { method: 'PUT', body: JSON.stringify({ relationships: Array.from(existing.values()) }) });
        resetHumanDraft();
        refetch();
    };

    const removeRelationship = async (relId: string) => {
        setDeletingIds(prev => new Set(prev).add(relId));
        try {
            await fetchAuth(`/agents/${agentId}/relationships/${relId}`, { method: 'DELETE' });
            refetch();
        } catch {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
            refetch();
        } finally {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
        }
    };

    const startEditRelationship = (r: any) => {
        setEditingId(r.id);
        setEditRelation(r.relation || 'collaborator');
        setEditDescription(r.description || '');
    };

    const saveEditRelationship = async (targetId: string) => {
        const updated = relationships.map((r: any) => ({
            user_id: r.user_id,
            relation: r.id === targetId ? editRelation : r.relation,
            description: r.id === targetId ? editDescription : r.description,
        }));
        await fetchAuth(`/agents/${agentId}/relationships/`, { method: 'PUT', body: JSON.stringify({ relationships: updated }) });
        setEditingId(null);
        refetch();
    };

    const addAgentRelationship = async () => {
        if (!selectedAgents.length) return;
        const existing = new Map(
            agentRelationships.map((r: any) => [r.agent_id, { agent_id: r.agent_id, relation: r.relation, description: r.description }])
        );
        selectedAgents.forEach((agent: any) => {
            existing.set(agent.agent_id, { agent_id: agent.agent_id, relation: agentRelation, description: agentDescription });
        });
        await fetchAuth(`/agents/${agentId}/relationships/agents`, { method: 'PUT', body: JSON.stringify({ relationships: Array.from(existing.values()) }) });
        resetAgentDraft();
        refetchAgentRels();
    };

    const removeAgentRelationship = async (relId: string) => {
        setDeletingIds(prev => new Set(prev).add(relId));
        try {
            await fetchAuth(`/agents/${agentId}/relationships/agents/${relId}`, { method: 'DELETE' });
            refetchAgentRels();
        } catch {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
            refetchAgentRels();
        } finally {
            setDeletingIds(prev => { const s = new Set(prev); s.delete(relId); return s; });
        }
    };

    const startEditAgentRelationship = (r: any) => {
        setEditingAgentId(r.id);
        setEditAgentRelation(r.relation || 'collaborator');
        setEditAgentDescription(r.description || '');
    };

    const saveEditAgentRelationship = async (targetId: string) => {
        const updated = agentRelationships.map((r: any) => ({
            agent_id: r.agent_id,
            relation: r.id === targetId ? editAgentRelation : r.relation,
            description: r.id === targetId ? editAgentDescription : r.description,
        }));
        await fetchAuth(`/agents/${agentId}/relationships/agents`, { method: 'PUT', body: JSON.stringify({ relationships: updated }) });
        setEditingAgentId(null);
        refetchAgentRels();
    };

    return (
        <div>
            {restrictedTooltip && (
                <div
                    style={{
                        position: 'fixed',
                        left: restrictedTooltip.x,
                        top: restrictedTooltip.y,
                        transform: 'translate(-50%, -100%)',
                        zIndex: 10000,
                        width: 'max-content',
                        maxWidth: 'min(320px, calc(100vw - 32px))',
                        padding: '8px 10px',
                        borderRadius: '8px',
                        border: '1px solid var(--border-subtle)',
                        background: 'var(--bg-primary)',
                        color: 'var(--text-primary)',
                        boxShadow: '0 10px 30px rgba(0,0,0,0.16)',
                        fontSize: '12px',
                        lineHeight: 1.45,
                        whiteSpace: 'normal',
                        overflowWrap: 'anywhere',
                        wordBreak: 'break-word',
                        pointerEvents: 'none',
                    }}
                >
                    {restrictedTooltip.text}
                </div>
            )}
            <div className="card" style={{ marginBottom: '12px' }}>
                <h4 style={{ marginBottom: '12px' }}>{t('agent.detail.humanRelationships')}</h4>
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>{t('agent.detail.humanRelationships')}</p>
                {relationships.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
                        {relationships.map((r: any) => (
                            <div key={r.id} style={{
                                borderRadius: '8px', border: '1px solid var(--border-subtle)',
                                overflow: 'hidden',
                                opacity: deletingIds.has(r.id) ? 0.4 : 1,
                                transition: 'opacity 0.2s ease',
                                pointerEvents: deletingIds.has(r.id) ? 'none' : 'auto',
                            }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px' }}>
                                    <div style={{ width: '36px', height: '36px', borderRadius: '50%', background: 'rgba(224,238,238,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '16px', fontWeight: 600, flexShrink: 0 }}>{r.member?.name?.[0] || '?'}</div>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div style={{ fontWeight: 600, fontSize: '13px' }}>
                                            {r.member?.name || '?'} <span className="badge" style={{ fontSize: '10px', marginLeft: '4px' }}>{r.relation_label}</span>
                                            {r.access_status && r.access_status !== 'active' && (
                                                <span
                                                    className="badge"
                                                    onMouseEnter={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onMouseLeave={hideRestrictedTooltip}
                                                    onFocus={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onBlur={hideRestrictedTooltip}
                                                    tabIndex={0}
                                                    style={{ fontSize: '10px', marginLeft: '4px', color: 'var(--warning)', background: 'rgba(245,158,11,0.12)', cursor: 'help' }}
                                                >
                                                    {r.access_status}
                                                </span>
                                            )}
                                        </div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {renderHumanMemberSourceBadge(r.member)}
                                            {r.member?.department_path || ''} · {r.member?.email || ''}
                                        </div>
                                        <OrgMemberIdentitySummary
                                            directorySources={r.member?.directory_sources}
                                            channelBindings={r.member?.channel_bindings}
                                        />
                                        {r.description && editingId !== r.id && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>{r.description}</div>}
                                    </div>
                                    {!readOnly && editingId !== r.id && (
                                        <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
                                            <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={() => startEditRelationship(r)}>{t('common.edit', 'Edit')}</button>
                                            <button
                                                className="btn btn-ghost"
                                                style={{ color: deletingIds.has(r.id) ? 'var(--text-tertiary)' : 'var(--error)', fontSize: '12px' }}
                                                disabled={deletingIds.has(r.id)}
                                                onClick={() => removeRelationship(r.id)}
                                            >
                                                {deletingIds.has(r.id) ? t('common.deleting', 'Deleting...') : t('common.delete')}
                                            </button>
                                        </div>
                                    )}
                                </div>
                                {editingId === r.id && (
                                    <div style={{ padding: '0 10px 10px', borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}>
                                        <div style={{ display: 'flex', gap: '8px', marginTop: '8px', marginBottom: '8px' }}>
                                            <select className="input" value={editRelation} onChange={e => setEditRelation(e.target.value)} style={{ width: '140px', fontSize: '12px' }}>
                                                {getRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                            </select>
                                        </div>
                                        <textarea className="input" value={editDescription} onChange={e => setEditDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px', width: '100%' }} placeholder={t('agent.detail.descriptionPlaceholder', 'Description...')} />
                                        <div style={{ display: 'flex', gap: '8px' }}>
                                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={() => saveEditRelationship(r.id)}>{t('common.save', 'Save')}</button>
                                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditingId(null)}>{t('common.cancel')}</button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
                {!readOnly && !showHumanForm && (
                    <button className="btn btn-secondary" type="button" onClick={() => setShowHumanForm(true)}>
                        {t('agent.detail.addRelationship', 'Add Relationship')}
                    </button>
                )}
                {!readOnly && showHumanForm && (
                    <div
                        style={{ border: '1px solid var(--border-subtle)', borderRadius: '8px', padding: '12px', background: 'var(--bg-elevated)' }}
                        onMouseDownCapture={(e) => {
                            const target = e.target as Node;
                            if (humanSearchRef.current && !humanSearchRef.current.contains(target)) {
                                setShowMemberDropdown(false);
                            }
                        }}
                    >
                        <div ref={humanSearchRef} style={{ position: 'relative', marginBottom: '8px' }}>
                            <input
                                className="input"
                                placeholder={t('agent.detail.searchMembers')}
                                value={search}
                                onChange={e => {
                                    setSearch(e.target.value);
                                    setShowMemberDropdown(true);
                                }}
                                onFocus={() => {
                                    setShowMemberDropdown(true);
                                    if (!search.trim() && searchResults.length === 0) {
                                        loadOrgMembers();
                                    }
                                }}
                                style={{ fontSize: '13px' }}
                            />
                            {showMemberDropdown && visibleMemberResults.length > 0 && (
                                <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', marginTop: '4px', maxHeight: '200px', overflowY: 'auto', zIndex: 10, boxShadow: '0 4px 12px rgba(0,0,0,0.15)' }}>
                                    {visibleMemberResults.map((m: any) => {
                                        const existingRelationship = relatedMemberById.get(m.user_id);
                                        const alreadyAdded = Boolean(existingRelationship);
                                        const checked = alreadyAdded || selectedMemberIds.has(m.user_id);
                                        return (
                                            <div
                                                key={m.user_id}
                                                style={{
                                                    padding: '8px 12px',
                                                    cursor: alreadyAdded ? 'default' : 'pointer',
                                                    fontSize: '13px',
                                                    borderBottom: '1px solid var(--border-subtle)',
                                                    display: 'flex',
                                                    alignItems: 'flex-start',
                                                    gap: '8px',
                                                    opacity: alreadyAdded ? 0.72 : 1,
                                                }}
                                                onClick={() => {
                                                    if (!alreadyAdded) toggleMemberSelection(m);
                                                }}
                                                onMouseEnter={e => (e.currentTarget.style.background = alreadyAdded ? 'transparent' : 'var(--bg-elevated)')}
                                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                                                <input type="checkbox" checked={checked} disabled={alreadyAdded} readOnly style={{ marginTop: '2px' }} />
                                                <div style={{ minWidth: 0, flex: 1 }}>
                                                    <div style={{ fontWeight: 500 }}>
                                                        {m.name}
                                                        {alreadyAdded && (
                                                            <span className="badge" style={{ fontSize: '10px', marginLeft: '6px', color: 'var(--text-tertiary)', background: 'var(--bg-elevated)' }}>
                                                                {isChinese ? '已添加' : 'Added'}
                                                            </span>
                                                        )}
                                                        {alreadyAdded && existingRelationship?.relation_label && (
                                                            <span className="badge" style={{ fontSize: '10px', marginLeft: '4px' }}>
                                                                {existingRelationship.relation_label}
                                                            </span>
                                                        )}
                                                    </div>
                                                    {m.nickname && m.nickname !== m.name && (
                                                        <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                                                            {isChinese ? '昵称' : 'Nickname'}: {m.nickname}
                                                        </div>
                                                    )}
                                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                        {renderHumanMemberSourceBadge(m)}
                                                        {m.department_path} · {m.email}
                                                    </div>
                                                    <OrgMemberIdentitySummary
                                                        directorySources={m.directory_sources}
                                                        channelBindings={m.channel_bindings}
                                                    />
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                        {showMemberDropdown && search && visibleMemberResults.length === 0 && (
                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                {t('agent.detail.noSearchResults', 'No available results')}
                            </div>
                        )}
                        {selectedMembers.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', marginBottom: '10px' }}>
                                {selectedMembers.map((member: any) => (
                                    <div
                                        key={member.user_id}
                                        style={{
                                            display: 'inline-flex',
                                            alignItems: 'center',
                                            gap: '8px',
                                            border: '1px solid var(--border-subtle)',
                                            borderRadius: '10px',
                                            padding: '8px 10px',
                                            background: 'var(--bg-primary)',
                                            fontSize: '12px',
                                            lineHeight: 1.2,
                                        }}
                                    >
                                        <div style={{ width: '24px', height: '24px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '11px', flexShrink: 0 }}>
                                            {member.name?.[0] || '?'}
                                        </div>
                                        <div style={{ minWidth: 0 }}>
                                            <div style={{ fontWeight: 600 }}>{member.name}</div>
                                            {member.nickname && member.nickname !== member.name && (
                                                <div style={{ color: 'var(--text-secondary)', fontSize: '11px' }}>
                                                    {isChinese ? '昵称' : 'Nickname'}: {member.nickname}
                                                </div>
                                            )}
                                            <div style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>{member.department_path || member.email || ''}</div>
                                            <OrgMemberIdentitySummary
                                                directorySources={member.directory_sources}
                                                channelBindings={member.channel_bindings}
                                            />
                                        </div>
                                        <button className="btn btn-ghost" type="button" style={{ fontSize: '12px', padding: 0, minWidth: 'auto', marginLeft: '2px' }} onClick={() => toggleMemberSelection(member)}>×</button>
                                    </div>
                                ))}
                            </div>
                        )}
                        <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                            <select className="input" value={relation} onChange={e => setRelation(e.target.value)} style={{ width: '160px', fontSize: '12px' }}>
                                {getRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                            </select>
                        </div>
                        <textarea className="input" placeholder="" value={description} onChange={e => setDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px' }} />
                        <div style={{ display: 'flex', gap: '8px' }}>
                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={addRelationship} disabled={selectedMembers.length === 0}>
                                {t('common.confirm')} {selectedMembers.length > 0 ? `(${selectedMembers.length})` : ''}
                            </button>
                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={resetHumanDraft}>
                                {t('common.cancel')}
                            </button>
                        </div>
                    </div>
                )}
            </div>
            <div className="card" style={{ marginBottom: '12px' }}>
                <h4 style={{ marginBottom: '12px' }}>{t('agent.detail.agentRelationships')}</h4>
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>{t('agent.detail.agentRelationships')}</p>
                {agentRelationships.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
                        {agentRelationships.map((r: any) => (
                            <div key={r.id} style={{
                                borderRadius: '8px',
                                border: `1px solid ${r.access_status && r.access_status !== 'active' ? 'rgba(245,158,11,0.35)' : 'rgba(16,185,129,0.3)'}`,
                                background: r.access_status && r.access_status !== 'active' ? 'rgba(245,158,11,0.06)' : 'rgba(16,185,129,0.05)', overflow: 'hidden',
                                opacity: deletingIds.has(r.id) ? 0.4 : 1,
                                transition: 'opacity 0.2s ease',
                                pointerEvents: deletingIds.has(r.id) ? 'none' : 'auto',
                            }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px' }}>
                                    <div style={{ width: '36px', height: '36px', borderRadius: '50%', background: 'rgba(16,185,129,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '16px', flexShrink: 0 }}>A</div>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div style={{ fontWeight: 600, fontSize: '13px' }}>
                                            {r.target_agent?.name || '?'} <span className="badge" style={{ fontSize: '10px', marginLeft: '4px', background: 'rgba(16,185,129,0.15)', color: 'rgb(16,185,129)' }}>{r.relation_label}</span>
                                            {r.access_status && r.access_status !== 'active' && (
                                                <span
                                                    className="badge"
                                                    onMouseEnter={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onMouseLeave={hideRestrictedTooltip}
                                                    onFocus={(event) => showRestrictedTooltip(event, r.access_status_reason)}
                                                    onBlur={hideRestrictedTooltip}
                                                    tabIndex={0}
                                                    style={{ fontSize: '10px', marginLeft: '4px', color: 'var(--warning)', background: 'rgba(245,158,11,0.12)', cursor: 'help' }}
                                                >
                                                    {r.access_status}
                                                </span>
                                            )}
                                        </div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                            {r.target_agent?.role_description || 'Agent'}
                                            {r.access_status_reason ? ` · ${r.access_status_reason}` : ''}
                                        </div>
                                        {r.description && editingAgentId !== r.id && <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>{r.description}</div>}
                                    </div>
                                    {!readOnly && editingAgentId !== r.id && (
                                        <div style={{ display: 'flex', gap: '4px', flexShrink: 0 }}>
                                            <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={() => startEditAgentRelationship(r)}>{t('common.edit', 'Edit')}</button>
                                            <button
                                                className="btn btn-ghost"
                                                style={{ color: deletingIds.has(r.id) ? 'var(--text-tertiary)' : 'var(--error)', fontSize: '12px' }}
                                                disabled={deletingIds.has(r.id)}
                                                onClick={() => removeAgentRelationship(r.id)}
                                            >
                                                {deletingIds.has(r.id) ? t('common.deleting', 'Deleting...') : t('common.delete')}
                                            </button>
                                        </div>
                                    )}
                                </div>
                                {editingAgentId === r.id && (
                                    <div style={{ padding: '0 10px 10px', borderTop: '1px solid rgba(16,185,129,0.2)', background: 'var(--bg-elevated)' }}>
                                        <div style={{ display: 'flex', gap: '8px', marginTop: '8px', marginBottom: '8px' }}>
                                            <select className="input" value={editAgentRelation} onChange={e => setEditAgentRelation(e.target.value)} style={{ width: '140px', fontSize: '12px' }}>
                                                {getAgentRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                            </select>
                                        </div>
                                        <textarea className="input" value={editAgentDescription} onChange={e => setEditAgentDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px', width: '100%' }} placeholder={t('agent.detail.descriptionPlaceholder', 'Description...')} />
                                        <div style={{ display: 'flex', gap: '8px' }}>
                                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={() => saveEditAgentRelationship(r.id)}>{t('common.save', 'Save')}</button>
                                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditingAgentId(null)}>{t('common.cancel')}</button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
                {!readOnly && !showAgentForm && (
                    <button className="btn btn-secondary" type="button" onClick={() => setShowAgentForm(true)}>
                        {t('agent.detail.addRelationship', 'Add Relationship')}
                    </button>
                )}
                {!readOnly && showAgentForm && (
                    <div
                        style={{ border: '1px solid rgba(16,185,129,0.3)', borderRadius: '8px', padding: '12px', background: 'var(--bg-elevated)' }}
                        onMouseDownCapture={(e) => {
                            const target = e.target as Node;
                            if (agentSearchRef.current && !agentSearchRef.current.contains(target)) {
                                setShowAgentDropdown(false);
                            }
                        }}
                    >
                        <div ref={agentSearchRef} style={{ position: 'relative', marginBottom: '8px' }}>
                            <input
                                className="input"
                                placeholder={t('agent.detail.searchAgents', '搜索可见数字员工...')}
                                value={agentSearch}
                                onChange={e => {
                                    setAgentSearch(e.target.value);
                                    setShowAgentDropdown(true);
                                }}
                                onFocus={() => {
                                    setShowAgentDropdown(true);
                                    if (!agentSearch.trim() && agentSearchResults.length === 0) {
                                        loadAgentCandidates();
                                    }
                                }}
                                style={{ fontSize: '13px' }}
                            />
                            {showAgentDropdown && visibleAgentResults.length > 0 && (
                                <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', marginTop: '4px', maxHeight: '200px', overflowY: 'auto', zIndex: 10, boxShadow: '0 4px 12px rgba(0,0,0,0.15)' }}>
                                    {visibleAgentResults.map((agent: any) => {
                                        const checked = selectedAgentIds.has(agent.agent_id);
                                        return (
                                            <div key={agent.agent_id} style={{ padding: '8px 12px', cursor: 'pointer', fontSize: '13px', borderBottom: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'flex-start', gap: '8px' }}
                                                onClick={() => toggleAgentSelection(agent)}
                                                onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-elevated)')}
                                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                                                <input type="checkbox" checked={checked} readOnly style={{ marginTop: '2px' }} />
                                                <div style={{ minWidth: 0, flex: 1 }}>
                                                    <div style={{ fontWeight: 500 }}>{agent.name}</div>
                                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{agent.role_description || 'Agent'}</div>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                        {showAgentDropdown && agentSearch && visibleAgentResults.length === 0 && (
                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                                {t('agent.detail.noSearchResults', 'No available results')}
                            </div>
                        )}
                        {selectedAgents.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', marginBottom: '10px' }}>
                                {selectedAgents.map((agent: any) => (
                                    <div
                                        key={agent.agent_id}
                                        style={{
                                            display: 'inline-flex',
                                            alignItems: 'center',
                                            gap: '8px',
                                            border: '1px solid rgba(16,185,129,0.24)',
                                            borderRadius: '10px',
                                            padding: '8px 10px',
                                            background: 'var(--bg-primary)',
                                            fontSize: '12px',
                                            lineHeight: 1.2,
                                        }}
                                    >
                                        <div style={{ width: '24px', height: '24px', borderRadius: '50%', background: 'rgba(16,185,129,0.12)', color: 'rgb(16,185,129)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '11px', flexShrink: 0 }}>
                                            {agent.name?.[0] || 'A'}
                                        </div>
                                        <div style={{ minWidth: 0 }}>
                                            <div style={{ fontWeight: 600 }}>{agent.name}</div>
                                            <div style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>{agent.role_description || 'Agent'}</div>
                                        </div>
                                        <button className="btn btn-ghost" type="button" style={{ fontSize: '12px', padding: 0, minWidth: 'auto', marginLeft: '2px' }} onClick={() => toggleAgentSelection(agent)}>×</button>
                                    </div>
                                ))}
                            </div>
                        )}
                        <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                            <select className="input" value={agentRelation} onChange={e => setAgentRelation(e.target.value)} style={{ width: '160px', flexShrink: 0, fontSize: '12px' }}>
                                {getAgentRelationOptions(t).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                            </select>
                        </div>
                        <textarea className="input" placeholder="" value={agentDescription} onChange={e => setAgentDescription(e.target.value)} rows={2} style={{ fontSize: '12px', resize: 'vertical', marginBottom: '8px' }} />
                        <div style={{ display: 'flex', gap: '8px' }}>
                            <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={addAgentRelationship} disabled={selectedAgents.length === 0}>
                                {t('common.confirm')} {selectedAgents.length > 0 ? `(${selectedAgents.length})` : ''}
                            </button>
                            <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={resetAgentDraft}>
                                {t('common.cancel')}
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
