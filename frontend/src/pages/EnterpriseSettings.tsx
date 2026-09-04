import React, { useState, useEffect, useMemo, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import UserManagement from './UserManagement';
import InvitationCodes from './InvitationCodes';
import { useDialog } from '../components/Dialog/DialogProvider';
import { useToast } from '../components/Toast/ToastProvider';
import { buildCompanyRegions, type CompanyRegion } from '../utils/companyRegions';
import { useAuthStore } from '../stores';
import OrgTab from './enterprise-settings/tabs/OrgTab';
import SkillsTab from './enterprise-settings/tabs/SkillsTab';
import LlmTab from './enterprise-settings/tabs/LlmTab';
import SpeechRecognitionTab from './enterprise-settings/tabs/SpeechRecognitionTab';
import EnterpriseKBBrowser from './enterprise-settings/components/EnterpriseKBBrowser';
import { A2AAsyncToggle, CompanyLogoEditor, CompanyNameEditor, CompanyTimezoneEditor } from './enterprise-settings/components/CompanyInfoEditors';
import { getLocalizedToolPresentation } from '../utils/toolPresentation';
import fetchJson from './enterprise-settings/api';
import ThemeColorPicker from './enterprise-settings/components/ThemeColorPicker';
import EnterpriseToolsTab from './enterprise-settings/components/EnterpriseToolsTab';
import {
    IconBrowser,
    IconBulb,
    IconClock,
    IconCheck,
    IconFileText,
    IconMessageCircle,
    IconSearch,
    IconSettings,
    IconTerminal2,
    IconTools,
    IconUser,
} from '@tabler/icons-react';





export default function EnterpriseSettings() {
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const qc = useQueryClient();
    const currentUser = useAuthStore((s) => s.user);
    const isPlatformAdmin = currentUser?.role === 'platform_admin' || currentUser?.is_platform_admin === true;
    type TabKey = 'llm' | 'speech' | 'org' | 'info' | 'approvals' | 'audit' | 'tools' | 'skills' | 'quotas' | 'users' | 'invites';
    const VALID_TABS: TabKey[] = ['info', 'llm', 'speech', 'tools', 'skills', 'invites', 'quotas', 'users', 'org', 'approvals', 'audit'];
    const getTabFromHash = (): TabKey => {
        const hash = window.location.hash.replace('#', '') as TabKey;
        return VALID_TABS.includes(hash) ? hash : 'info';
    };
    const [activeTab, setActiveTab] = useState<TabKey>(getTabFromHash);
    // Sync hash ↔ activeTab: hashchange navigation (back/forward) updates state
    useEffect(() => {
        const handler = () => setActiveTab(getTabFromHash());
        window.addEventListener('hashchange', handler);
        return () => window.removeEventListener('hashchange', handler);
    }, []);

    // Track selected tenant as state so page refreshes on company switch
    const [selectedTenantId, setSelectedTenantId] = useState(localStorage.getItem('current_tenant_id') || '');
    useEffect(() => {
        const handler = (e: StorageEvent) => {
            if (e.key === 'current_tenant_id') {
                setSelectedTenantId(e.newValue || '');
            }
        };
        window.addEventListener('storage', handler);
        return () => window.removeEventListener('storage', handler);
    }, []);

    // Tenant quota defaults
    const [quotaForm, setQuotaForm] = useState({
        default_message_limit: 50, default_message_period: 'permanent',
        default_max_agents: 2, default_agent_ttl_hours: 0,
        default_max_llm_calls_per_day: 1000, min_heartbeat_interval_minutes: 120,
        default_max_triggers: 20, min_poll_interval_floor: 5, max_webhook_rate_ceiling: 5,
    });
    const [quotaSaving, setQuotaSaving] = useState(false);
    const [quotaSaved, setQuotaSaved] = useState(false);
    useEffect(() => {
        if (activeTab === 'quotas') {
            fetchJson<any>('/enterprise/tenant-quotas').then(d => {
                if (d && Object.keys(d).length) setQuotaForm(f => ({ ...f, ...d }));
            }).catch(() => { });
        }
    }, [activeTab]);
    const saveQuotas = async () => {
        setQuotaSaving(true);
        try {
            await fetchJson('/enterprise/tenant-quotas', { method: 'PATCH', body: JSON.stringify(quotaForm) });
            setQuotaSaved(true); setTimeout(() => setQuotaSaved(false), 2000);
        } catch (e: any) { toast.error(t('common.error.saveFailed', '保存失败'), { details: String(e?.message || e) }); }
        setQuotaSaving(false);
    };
    const [companyIntro, setCompanyIntro] = useState('');
    const [companyIntroSaving, setCompanyIntroSaving] = useState(false);
    const [companyIntroSaved, setCompanyIntroSaved] = useState(false);


    // Company intro key: always per-tenant scoped
    const companyIntroKey = selectedTenantId ? `company_intro_${selectedTenantId}` : 'company_intro';

    // Load Company Intro (tenant-scoped only, no fallback to global)
    useEffect(() => {
        setCompanyIntro('');
        if (!selectedTenantId) return;
        const tenantKey = `company_intro_${selectedTenantId}`;
        fetchJson<any>(`/enterprise/system-settings/${tenantKey}`)
            .then(d => {
                if (d?.value?.content) {
                    setCompanyIntro(d.value.content);
                }
                // No fallback — each company starts empty with placeholder watermark
            })
            .catch(() => { });
    }, [selectedTenantId]);

    const saveCompanyIntro = async () => {
        setCompanyIntroSaving(true);
        try {
            await fetchJson(`/enterprise/system-settings/${companyIntroKey}`, {
                method: 'PUT', body: JSON.stringify({ value: { content: companyIntro } }),
            });
            setCompanyIntroSaved(true);
            setTimeout(() => setCompanyIntroSaved(false), 2000);
        } catch (e) { }
        setCompanyIntroSaving(false);
    };
    const [auditFilter, setAuditFilter] = useState<'all' | 'background' | 'actions'>('all');
    const [infoRefresh, setInfoRefresh] = useState(0);
    const [kbToast, setKbToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);

    const [allTools, setAllTools] = useState<any[]>([]);
    const [showAddMCP, setShowAddMCP] = useState(false);
    // Edit Server modal state — null when closed, otherwise the server to edit.
    // server_id is the FK from tools.mcp_server_id; using it directly avoids fragile
    // base_url_template string matching (host.docker.internal vs in-cluster names diverge).
    const [editingMcpServer, setEditingMcpServer] = useState<{
        server_id: string;
        server_name: string;
    } | null>(null);

    const [editingToolId, setEditingToolId] = useState<string | null>(null);
    const [editingConfig, setEditingConfig] = useState<Record<string, any>>({});
    const [editingConfigInitial, setEditingConfigInitial] = useState('');
    const [showAdvancedToolConfig, setShowAdvancedToolConfig] = useState(false);

    const [configCategory, setConfigCategory] = useState<string | null>(null);

    // Category-level config schemas: tools sharing the same key have config on category header
    const GLOBAL_CATEGORY_CONFIG_SCHEMAS: Record<string, { title: string; fields: any[] }> = {
        agentbay: {
            title: 'AgentBay Settings',
            fields: [
                { key: 'api_key', label: 'API Key (from AgentBay)', type: 'password', placeholder: 'Enter your AgentBay API key' },
                { key: 'os_type', label: 'Cloud Computer OS', type: 'select', default: 'windows', options: [{ value: 'linux', label: 'Linux' }, { value: 'windows', label: 'Windows' }] },
            ],
        },
    };
    const GLOBAL_CATEGORY_CONFIG_PRIMARY_TOOL: Record<string, string> = {
        agentbay: 'agentbay_browser_navigate',
    };

    const applyConfigDefaults = (fields: any[] = [], config: Record<string, any> = {}) => {
        const next = { ...config };
        for (const field of fields) {
            if (field.default !== undefined && (next[field.key] === undefined || next[field.key] === null || next[field.key] === '')) {
                next[field.key] = field.default;
            }
        }
        return next;
    };

    const renderCategoryIcon = (category: string, size = 15) => {
        const style = { color: 'var(--text-tertiary)' };
        switch (category) {
            case 'agentbay': return <IconBrowser size={size} stroke={1.8} style={style} />;
            case 'browser': return <IconBrowser size={size} stroke={1.8} style={style} />;
            case 'file': return <IconFileText size={size} stroke={1.8} style={style} />;
            case 'communication':
            case 'feishu':
            case 'email':
            case 'social':
                return <IconMessageCircle size={size} stroke={1.8} style={style} />;
            case 'search':
            case 'discovery':
                return <IconSearch size={size} stroke={1.8} style={style} />;
            case 'code': return <IconTerminal2 size={size} stroke={1.8} style={style} />;
            case 'aware': return <IconClock size={size} stroke={1.8} style={style} />;
            case 'custom': return <IconSettings size={size} stroke={1.8} style={style} />;
            default: return <IconTools size={size} stroke={1.8} style={style} />;
        }
    };
    const getToolGroupMeta = (groupKey: string, toolsInGroup: any[]) => {
        const first = toolsInGroup.find((tool: any) => tool.type === 'mcp' && tool.mcp_server_name) || toolsInGroup[0];
        const presentation = getLocalizedToolPresentation(t, first || {});
        return {
            label: presentation.groupLabel,
            description: presentation.groupDescription,
            iconCategory: groupKey.startsWith('mcp:') ? 'custom' : presentation.categoryKey,
            configCategory: presentation.categoryKey,
        };
    };
    const switchTrack = (enabled: boolean, mixed = false) => ({
        position: 'absolute' as const,
        inset: 0,
        background: enabled ? 'var(--accent-primary)' : mixed ? 'var(--border-default)' : 'var(--bg-tertiary)',
        borderRadius: '11px',
        transition: 'background 0.2s',
    });
    const switchKnob = (enabled: boolean) => ({
        position: 'absolute' as const,
        left: enabled ? '20px' : '2px',
        top: '2px',
        width: '18px',
        height: '18px',
        background: '#fff',
        borderRadius: '50%',
        transition: 'left 0.2s',
        boxShadow: '0 1px 3px rgba(0,0,0,0.12)',
    });
    const [toolsView, setToolsView] = useState<'global' | 'agent-installed'>('global');
    const [agentInstalledTools, setAgentInstalledTools] = useState<any[]>([]);
    const [toolSearch, setToolSearch] = useState('');
    const [toolStatusFilter, setToolStatusFilter] = useState<'all' | 'enabled' | 'disabled' | 'default' | 'configured'>('all');
    const [expandedToolCategories, setExpandedToolCategories] = useState<Set<string>>(() => new Set());
    const [expandedAgentInstalledGroups, setExpandedAgentInstalledGroups] = useState<Set<string>>(() => new Set());
    const hasMeaningfulConfigValue = (value: any): boolean => {
        if (value == null) return false;
        if (typeof value === 'string') return value.trim().length > 0;
        if (typeof value === 'number') return Number.isFinite(value);
        if (typeof value === 'boolean') return value;
        if (Array.isArray(value)) return value.some(hasMeaningfulConfigValue);
        if (typeof value === 'object') return Object.values(value).some(hasMeaningfulConfigValue);
        return false;
    };
    const hasMeaningfulConfig = (config?: Record<string, any> | null): boolean => {
        if (!config) return false;
        return Object.values(config).some(hasMeaningfulConfigValue);
    };
    const loadAllTools = async () => {
        const tid = selectedTenantId;
        const data = await fetchJson<any[]>(`/tools${tid ? `?tenant_id=${tid}` : ''}`);
        setAllTools(data);
    };
    const loadAgentInstalledTools = async () => {
        try {
            const tid = selectedTenantId;
            const data = await fetchJson<any[]>(`/tools/agent-installed${tid ? `?tenant_id=${tid}` : ''}`);
            setAgentInstalledTools(data);
        } catch (error) {
            console.warn('[EnterpriseTools] Failed to load agent-installed tools', error);
            setAgentInstalledTools([]);
        }
    };
    useEffect(() => { if (activeTab === 'tools') { loadAllTools(); loadAgentInstalledTools(); } }, [activeTab, selectedTenantId]);

    // ─── Jina API Key
    const [jinaKey, setJinaKey] = useState('');
    const [jinaKeySaved, setJinaKeySaved] = useState(false);
    const [jinaKeySaving, setJinaKeySaving] = useState(false);
    const [jinaKeyMasked, setJinaKeyMasked] = useState('');  // stored key from DB
    useEffect(() => {
        if (activeTab !== 'tools') return;
        const token = localStorage.getItem('token');
        fetch('/api/enterprise/system-settings/jina_api_key', { headers: { Authorization: `Bearer ${token}` } })
            .then(r => r.json())
            .then(d => { if (d.value?.api_key) setJinaKeyMasked(d.value.api_key.slice(0, 8) + '••••••••'); })
            .catch(() => { });
    }, [activeTab]);
    const saveJinaKey = async () => {
        setJinaKeySaving(true);
        const token = localStorage.getItem('token');
        await fetch('/api/enterprise/system-settings/jina_api_key', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            body: JSON.stringify({ value: { api_key: jinaKey } }),
        });
        setJinaKeyMasked(jinaKey.slice(0, 8) + '••••••••');
        setJinaKey('');
        setJinaKeySaving(false);
        setJinaKeySaved(true);
        setTimeout(() => setJinaKeySaved(false), 2000);
    };
    const clearJinaKey = async () => {
        const token = localStorage.getItem('token');
        await fetch('/api/enterprise/system-settings/jina_api_key', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            body: JSON.stringify({ value: {} }),
        });
        setJinaKeyMasked('');
        setJinaKey('');
    };


    const { data: currentTenant } = useQuery({
        queryKey: ['tenant', selectedTenantId],
        queryFn: () => fetchJson<any>(`/tenants/${selectedTenantId}`),
        enabled: !!selectedTenantId,
    });

    // ─── Stats (scoped to selected tenant)
    const { data: stats } = useQuery({
        queryKey: ['enterprise-stats', selectedTenantId],
        queryFn: () => fetchJson<any>(`/enterprise/stats${selectedTenantId ? `?tenant_id=${selectedTenantId}` : ''}`),
    });

    // ─── Approvals
    const { data: approvals = [] } = useQuery({
        queryKey: ['approvals', selectedTenantId],
        queryFn: () => fetchJson<any[]>(`/enterprise/approvals${selectedTenantId ? `?tenant_id=${selectedTenantId}` : ''}`),
        enabled: activeTab === 'approvals',
    });
    const resolveApproval = useMutation({
        mutationFn: ({ id, action }: { id: string; action: string }) =>
            fetchJson(`/enterprise/approvals/${id}/resolve`, { method: 'POST', body: JSON.stringify({ action }) }),
        onSuccess: () => qc.invalidateQueries({ queryKey: ['approvals', selectedTenantId] }),
    });

    // ─── Audit Logs
    const BG_ACTIONS = ['supervision_tick', 'supervision_fire', 'supervision_error', 'schedule_tick', 'schedule_fire', 'schedule_error', 'heartbeat_tick', 'heartbeat_fire', 'heartbeat_error', 'server_startup'];
    const { data: auditLogs = [] } = useQuery({
        queryKey: ['audit-logs', selectedTenantId],
        queryFn: () => fetchJson<any[]>(`/enterprise/audit-logs?limit=200${selectedTenantId ? `&tenant_id=${selectedTenantId}` : ''}`),
        enabled: activeTab === 'audit',
    });
    const filteredAuditLogs = auditLogs.filter((log: any) => {
        if (auditFilter === 'background') return BG_ACTIONS.includes(log.action);
        if (auditFilter === 'actions') return !BG_ACTIONS.includes(log.action);
        return true;
    });

    return (
        <>
            <div>
                <div className="page-header">
                    <div>
                        <h1 className="page-title">{t('nav.enterprise')}</h1>
                        {stats && (
                            <div style={{ display: 'flex', gap: '24px', marginTop: '8px' }}>
                                <span className="badge badge-info">{t('enterprise.stats.users', { count: stats.total_users })}</span>
                                <span className="badge badge-success">{t('enterprise.stats.runningAgents', { running: stats.running_agents, total: stats.total_agents })}</span>
                                {stats.pending_approvals > 0 && <span className="badge badge-warning">{stats.pending_approvals} {t('enterprise.tabs.approvals')}</span>}
                            </div>
                        )}
                    </div>
                </div>

                <div className="tabs">
                    {(['info', 'llm', 'speech', 'tools', 'skills', 'invites', 'quotas', 'users', 'org', 'approvals', 'audit'] as const).map(tab => (
                        <div
                            key={tab}
                            className={`tab ${activeTab === tab ? 'active' : ''}`}
                            onClick={() => {
                                // Update URL hash so each tab has a bookmarkable address
                                window.location.hash = tab;
                                setActiveTab(tab);
                            }}
                        >
                            {tab === 'quotas' ? t('enterprise.tabs.quotas', 'Quotas') : tab === 'users' ? t('enterprise.tabs.users', 'Users') : tab === 'invites' ? t('enterprise.tabs.invites', 'Invitations') : t(`enterprise.tabs.${tab}`)}
                        </div>
                    ))}
                </div>

                {/* ── LLM Model Pool ── */}
                {activeTab === 'llm' && <LlmTab selectedTenantId={selectedTenantId} />}

                {/* ── Speech Recognition Service ── */}
                {activeTab === 'speech' && <SpeechRecognitionTab selectedTenantId={selectedTenantId} />}

                {/* ── Org Structure ── */}
                {activeTab === 'org' && <OrgTab tenant={currentTenant} />}

                {/* ── Approvals ── */}
                {activeTab === 'approvals' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        {approvals.map((a: any) => (
                            <div key={a.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                                <div>
                                    <div style={{ fontWeight: 500 }}>{a.action_type}</div>
                                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                                        {a.agent_name || `Agent ${a.agent_id.slice(0, 8)}`} · {new Date(a.created_at).toLocaleString()}
                                    </div>
                                </div>
                                {a.status === 'pending' ? (
                                    <div style={{ display: 'flex', gap: '8px' }}>
                                        <button className="btn btn-primary" onClick={() => resolveApproval.mutate({ id: a.id, action: 'approve' })}>{t('common.confirm')}</button>
                                        <button className="btn btn-danger" onClick={() => resolveApproval.mutate({ id: a.id, action: 'reject' })}>Reject</button>
                                    </div>
                                ) : (
                                    <span className={`badge ${a.status === 'approved' ? 'badge-success' : 'badge-error'}`}>
                                        {a.status === 'approved' ? 'Approved' : 'Rejected'}
                                    </span>
                                )}
                            </div>
                        ))}
                        {approvals.length === 0 && <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.noData')}</div>}
                    </div>
                )}

                {/* ── Audit Logs ── */}
                {activeTab === 'audit' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                        {/* Sub-filter pills */}
                        <div style={{ display: 'flex', gap: '8px', padding: '8px 12px', borderBottom: '1px solid var(--border-color)' }}>
                            {([
                                ['all', t('enterprise.audit.filterAll')],
                                ['background', t('enterprise.audit.filterBackground')],
                                ['actions', t('enterprise.audit.filterActions')],
                            ] as const).map(([key, label]) => (
                                <button key={key}
                                    onClick={() => setAuditFilter(key as any)}
                                    style={{
                                        padding: '4px 14px', borderRadius: '12px', fontSize: '12px', fontWeight: 500,
                                        border: auditFilter === key ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                                        background: auditFilter === key ? 'var(--accent-primary)' : 'transparent',
                                        color: auditFilter === key ? '#fff' : 'var(--text-secondary)',
                                        cursor: 'pointer', transition: 'all 0.15s',
                                    }}
                                >{label}</button>
                            ))}
                            <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-tertiary)', alignSelf: 'center' }}>
                                {t('enterprise.audit.records', { count: filteredAuditLogs.length })}
                            </span>
                        </div>
                        {/* Log entries */}
                        {filteredAuditLogs.map((log: any) => {
                            const isBg = BG_ACTIONS.includes(log.action);
                            const details = log.details && typeof log.details === 'object' && Object.keys(log.details).length > 0 ? log.details : null;
                            return (
                                <div key={log.id} style={{ borderBottom: '1px solid var(--border-subtle)', padding: '6px 12px' }}>
                                    <div style={{ display: 'flex', gap: '12px', fontSize: '13px', alignItems: 'center' }}>
                                        <span style={{ color: 'var(--text-tertiary)', whiteSpace: 'nowrap', fontFamily: 'var(--font-mono)', fontSize: '11px' }}>
                                            {new Date(log.created_at).toLocaleString()}
                                        </span>
                                        <span style={{
                                            padding: '1px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 500,
                                            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                                            background: isBg ? 'rgba(99,102,241,0.12)' : 'rgba(34,197,94,0.12)',
                                            color: isBg ? 'var(--accent-color)' : 'rgb(34,197,94)',
                                        }}>{isBg ? <IconSettings size={12} stroke={1.8} /> : <IconUser size={12} stroke={1.8} />}</span>
                                        <span style={{ flex: 1, fontWeight: 500 }}>{log.action}</span>
                                        <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>{log.agent_id?.slice(0, 8) || '-'}</span>
                                    </div>
                                    {details && (
                                        <div style={{ marginLeft: '100px', marginTop: '2px', fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)' }}>
                                            {Object.entries(details).map(([k, v]) => (
                                                <span key={k} style={{ marginRight: '12px' }}>{k}={typeof v === 'string' ? v : JSON.stringify(v)}</span>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                        {filteredAuditLogs.length === 0 && <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.noData')}</div>}
                    </div>
                )}

                {/* ── Company Management ── */}
                {activeTab === 'info' && (
                    <div>
                        <CompanyLogoEditor key={`logo-${selectedTenantId}`} />
                        <CompanyNameEditor key={`name-${selectedTenantId}`} />
                        <CompanyTimezoneEditor key={`tz-${selectedTenantId}`} />
                        <div className="card" style={{ padding: '16px', marginBottom: '24px' }}>
                            <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '4px' }}>
                                {t('enterprise.companyIntro.title', 'Company Intro')}
                            </div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                                {t('enterprise.companyIntro.description', 'Describe your company\'s mission, products, and culture. This information is included in every agent conversation as context.')}
                            </div>
                            <textarea
                                className="form-input"
                                value={companyIntro}
                                onChange={e => setCompanyIntro(e.target.value)}
                                placeholder={`# Company Name\nExample Corp\n\n# About\nOpenClaw\uD83E\uDD9E For Teams\nOpen Source \u00B7 Multi-Agent Collaboration\n\nOpenClaw empowers individuals.\nOur platform scales it to frontier organizations.`}
                                style={{
                                    minHeight: '200px', resize: 'vertical',
                                    fontFamily: 'var(--font-mono)', fontSize: '13px',
                                    lineHeight: '1.6', whiteSpace: 'pre-wrap',
                                }}
                            />
                            <div style={{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                                <button className="btn btn-primary" onClick={saveCompanyIntro} disabled={companyIntroSaving}>
                                    {companyIntroSaving ? t('common.loading') : t('common.save', 'Save')}
                                </button>
                                {companyIntroSaved && <span style={{ color: 'var(--success)', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}><IconCheck size={13} stroke={2} /> {t('enterprise.config.saved', 'Saved')}</span>}
                                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                                    <IconBulb size={13} stroke={1.8} /> {t('enterprise.companyIntro.hint', 'This content appears in every agent\'s system prompt')}
                                </span>
                            </div>
                        </div>
                        <div className="card" style={{ marginBottom: '24px', padding: '16px' }}>
                            <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '4px' }}>
                                {t('enterprise.kb.title')}
                            </div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                                {t('enterprise.kb.description', 'Shared files accessible to all agents via enterprise_info/ directory.')}
                            </div>
                            <EnterpriseKBBrowser onRefresh={() => setInfoRefresh((v: number) => v + 1)} refreshKey={infoRefresh} />
                        </div>
                        <ThemeColorPicker />
                        <A2AAsyncToggle key={`a2a-${selectedTenantId}`} />

                        {/* ── Danger Zone: Delete Company ── */}
                        <div style={{ marginTop: '32px', padding: '16px', border: '1px solid var(--status-error, #e53e3e)', borderRadius: '8px' }}>
                            <h3 style={{ marginBottom: '4px', color: 'var(--status-error, #e53e3e)' }}>{t('enterprise.dangerZone', 'Danger Zone')}</h3>
                            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                                {t('enterprise.deleteCompanyDesc', 'Permanently delete this company and all its data including agents, models, tools, and skills. This action cannot be undone.')}
                            </p>
                            <button
                                className="btn"
                                onClick={async () => {
                                    const ok = await dialog.confirm(
                                        t('enterprise.deleteCompanyConfirm', 'Are you sure you want to delete this company and ALL its data? This cannot be undone.'),
                                        {
                                            title: t('enterprise.deleteCompanyTitle', 'Delete company'),
                                            danger: true,
                                            confirmLabel: t('enterprise.deleteCompanyConfirmButton', 'Permanently delete'),
                                        },
                                    );
                                    if (!ok) return;
                                    try {
                                        const res = await fetchJson<any>(`/tenants/${selectedTenantId}`, { method: 'DELETE' });
                                        // Switch to fallback tenant
                                        const fallbackId = res.fallback_tenant_id;
                                        localStorage.setItem('current_tenant_id', fallbackId);
                                        setSelectedTenantId(fallbackId);
                                        window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: fallbackId }));
                                        qc.invalidateQueries({ queryKey: ['tenants'] });
                                    } catch (e: any) {
                                        await dialog.alert(t('enterprise.deleteCompanyFailed', 'Failed to delete company'), { type: 'error', details: String(e?.message || e) });
                                    }
                                }}
                                style={{
                                    background: 'transparent', color: 'var(--status-error, #e53e3e)',
                                    border: '1px solid var(--status-error, #e53e3e)', borderRadius: '6px',
                                    padding: '6px 16px', fontSize: '13px', cursor: 'pointer',
                                }}
                            >
                                {t('enterprise.deleteCompany', 'Delete This Company')}
                            </button>
                        </div>
                    </div>
                )}

                {/* ── Quotas Tab ── */}
                {activeTab === 'quotas' && (
                    <div>
                        <h3 style={{ marginBottom: '4px' }}>{t('enterprise.quotas.defaultUserQuotas')}</h3>
                        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
                            {t('enterprise.quotas.defaultsApply')}
                        </p>
                        <div className="card" style={{ padding: '16px' }}>
                            {/* ── Conversation Limits ── */}
                            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '10px' }}>{t('enterprise.quotas.conversationLimits')}</div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '20px' }}>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.messageLimit')}</label>
                                    <input className="form-input" type="number" min={0} value={quotaForm.default_message_limit}
                                        onChange={e => setQuotaForm({ ...quotaForm, default_message_limit: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.quotas.maxMessagesPerPeriod')}</div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.messagePeriod')}</label>
                                    <select className="form-input" value={quotaForm.default_message_period}
                                        onChange={e => setQuotaForm({ ...quotaForm, default_message_period: e.target.value })}>
                                        <option value="permanent">{t('enterprise.quotas.permanent')}</option>
                                        <option value="daily">{t('enterprise.quotas.daily')}</option>
                                        <option value="weekly">{t('enterprise.quotas.weekly')}</option>
                                        <option value="monthly">{t('enterprise.quotas.monthly')}</option>
                                    </select>
                                </div>
                            </div>

                            {/* ── Agent Limits ── */}
                            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '10px' }}>{t('enterprise.quotas.agentLimits')}</div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px', marginBottom: '20px' }}>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.maxAgents')}</label>
                                    <input className="form-input" type="number" min={0} value={quotaForm.default_max_agents}
                                        onChange={e => setQuotaForm({ ...quotaForm, default_max_agents: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.quotas.agentsUserCanCreate')}</div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.agentTTL')}</label>
                                    <select
                                        className="form-input"
                                        value={quotaForm.default_agent_ttl_hours > 0 ? 'custom' : 'permanent'}
                                        onChange={e => setQuotaForm({
                                            ...quotaForm,
                                            default_agent_ttl_hours: e.target.value === 'permanent' ? 0 : 48,
                                        })}
                                    >
                                        <option value="permanent">{t('enterprise.quotas.permanent')}</option>
                                        <option value="custom">{t('enterprise.quotas.customHours', 'Custom hours')}</option>
                                    </select>
                                    {quotaForm.default_agent_ttl_hours > 0 && (
                                        <input
                                            className="form-input"
                                            type="number"
                                            min={1}
                                            value={quotaForm.default_agent_ttl_hours}
                                            onChange={e => setQuotaForm({ ...quotaForm, default_agent_ttl_hours: Number(e.target.value) })}
                                            style={{ marginTop: '8px' }}
                                        />
                                    )}
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.quotas.agentAutoExpiry')}</div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.dailyLLMCalls')}</label>
                                    <input className="form-input" type="number" min={0} value={quotaForm.default_max_llm_calls_per_day}
                                        onChange={e => setQuotaForm({ ...quotaForm, default_max_llm_calls_per_day: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.quotas.maxLLMCallsPerDay')}</div>
                                </div>
                            </div>

                            {/* ── System Limits ── */}
                            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '10px' }}>{t('enterprise.quotas.system')}</div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px' }}>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.minHeartbeatInterval')}</label>
                                    <input className="form-input" type="number" min={1} value={quotaForm.min_heartbeat_interval_minutes}
                                        onChange={e => setQuotaForm({ ...quotaForm, min_heartbeat_interval_minutes: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.quotas.minHeartbeatDesc')}</div>
                                </div>
                            </div>

                            {/* ── Trigger Limits ── */}
                            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '10px' }}>{t('enterprise.quotas.triggerLimits')}</div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px', marginBottom: '20px' }}>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.defaultMaxTriggers', 'Default Max Triggers')}</label>
                                    <input className="form-input" type="number" min={1} max={100} value={quotaForm.default_max_triggers}
                                        onChange={e => setQuotaForm({ ...quotaForm, default_max_triggers: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        {t('enterprise.quotas.defaultMaxTriggersDesc', 'Default trigger limit for new agents')}
                                    </div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.minPollInterval', 'Min Poll Interval (min)')}</label>
                                    <input className="form-input" type="number" min={1} max={60} value={quotaForm.min_poll_interval_floor}
                                        onChange={e => setQuotaForm({ ...quotaForm, min_poll_interval_floor: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        {t('enterprise.quotas.minPollIntervalDesc', 'Company-wide floor: agents cannot poll faster than this')}
                                    </div>
                                </div>
                                <div className="form-group">
                                    <label className="form-label">{t('enterprise.quotas.maxWebhookRate', 'Max Webhook Rate (/min)')}</label>
                                    <input className="form-input" type="number" min={1} max={60} value={quotaForm.max_webhook_rate_ceiling}
                                        onChange={e => setQuotaForm({ ...quotaForm, max_webhook_rate_ceiling: Number(e.target.value) })} />
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                                        {t('enterprise.quotas.maxWebhookRateDesc', 'Company-wide ceiling: max webhook hits per minute per agent')}
                                    </div>
                                </div>
                            </div>
                            <div style={{ marginTop: '16px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                                <button className="btn btn-primary" onClick={saveQuotas} disabled={quotaSaving}>
                                    {quotaSaving ? t('common.loading') : t('common.save', 'Save')}
                                </button>
                                {quotaSaved && <span style={{ color: 'var(--success)', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}><IconCheck size={13} stroke={2} /> Saved</span>}
                            </div>
                        </div>
                    </div>
                )}

                {/* ── Users Tab ── */}
                {activeTab === 'users' && (
                    <UserManagement key={selectedTenantId} />
                )}


                {/* ── Tools Tab ── */}
                {activeTab === 'tools' && (
                    <EnterpriseToolsTab model={{
                        GLOBAL_CATEGORY_CONFIG_PRIMARY_TOOL, GLOBAL_CATEGORY_CONFIG_SCHEMAS, allTools, agentInstalledTools,
                        applyConfigDefaults, configCategory, currentUser, dialog, editingConfig, editingConfigInitial, editingMcpServer, editingToolId,
                        expandedAgentInstalledGroups, expandedToolCategories, getToolGroupMeta, hasMeaningfulConfig, loadAgentInstalledTools,
                        loadAllTools, renderCategoryIcon,
                        selectedTenantId, setConfigCategory, setEditingConfig, setEditingConfigInitial, setEditingMcpServer, setEditingToolId,
                        setExpandedAgentInstalledGroups, setExpandedToolCategories, setShowAddMCP, setShowAdvancedToolConfig, setToolSearch, setToolStatusFilter,
                        setToolsView, showAddMCP, showAdvancedToolConfig, switchKnob, switchTrack, t, toast, toolSearch, toolStatusFilter, toolsView,
                    }} />
                )}

                {/* ── Skills Tab ── */}
                {activeTab === 'skills' && <SkillsTab />}

                {/* ── Invitation Codes Tab ── */}
                {activeTab === 'invites' && <InvitationCodes />}
            </div>

            {
                kbToast && (
                    <div style={{
                        position: 'fixed', top: '20px', right: '20px', zIndex: 20000,
                        padding: '12px 20px', borderRadius: '8px',
                        background: kbToast.type === 'success' ? 'rgba(34, 197, 94, 0.9)' : 'rgba(239, 68, 68, 0.9)',
                        color: '#fff', fontSize: '14px', fontWeight: 500,
                        boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                    }}>
                        {''}{kbToast.message}
                    </div>
                )
            }
        </>
    );
}
