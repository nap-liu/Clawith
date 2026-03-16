import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { adminApi } from '../services/api';
import { useAuthStore } from '../stores';

// Platform Admin company management page
export default function AdminCompanies() {
    const { t } = useTranslation();
    const user = useAuthStore((s) => s.user);
    const [companies, setCompanies] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    // Platform settings
    const [settings, setSettings] = useState<any>({});
    const [settingsLoading, setSettingsLoading] = useState(false);

    // Create company
    const [showCreate, setShowCreate] = useState(false);
    const [newName, setNewName] = useState('');
    const [creating, setCreating] = useState(false);
    const [createdCode, setCreatedCode] = useState('');

    // Toast
    const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);
    const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
        setToast({ msg, type });
        setTimeout(() => setToast(null), 3000);
    };

    const loadCompanies = async () => {
        setLoading(true);
        try {
            const data = await adminApi.listCompanies();
            setCompanies(data);
        } catch (e: any) {
            setError(e.message);
        }
        setLoading(false);
    };

    const loadSettings = async () => {
        try {
            const data = await adminApi.getPlatformSettings();
            setSettings(data);
        } catch { }
    };

    useEffect(() => {
        loadCompanies();
        loadSettings();
    }, []);

    // Guard: only platform_admin
    if (user?.role !== 'platform_admin') {
        return (
            <div style={{ padding: '60px', textAlign: 'center', color: 'var(--text-tertiary)' }}>
                {t('common.noPermission', 'You do not have permission to access this page.')}
            </div>
        );
    }

    const handleCreate = async () => {
        if (!newName.trim()) return;
        setCreating(true);
        try {
            const result = await adminApi.createCompany({ name: newName.trim() });
            setCreatedCode(result.admin_invitation_code || '');
            setNewName('');
            loadCompanies();
            showToast(t('admin.companyCreated', 'Company created'));
        } catch (e: any) {
            showToast(e.message || 'Failed', 'error');
        }
        setCreating(false);
    };

    const handleToggle = async (id: string, currentlyActive: boolean) => {
        const action = currentlyActive ? 'disable' : 'enable';
        if (currentlyActive && !confirm(t('admin.confirmDisable', 'Disable this company? All users and agents will be paused.'))) return;
        try {
            await adminApi.toggleCompany(id);
            loadCompanies();
            showToast(`Company ${action}d`);
        } catch (e: any) {
            showToast(e.message || 'Failed', 'error');
        }
    };

    const handleToggleSetting = async (key: string, value: boolean) => {
        setSettingsLoading(true);
        try {
            await adminApi.updatePlatformSettings({ [key]: value });
            setSettings((s: any) => ({ ...s, [key]: value }));
            showToast('Setting updated');
        } catch (e: any) {
            showToast(e.message || 'Failed', 'error');
        }
        setSettingsLoading(false);
    };

    return (
        <div style={{ maxWidth: '960px', margin: '0 auto', padding: '32px 24px' }}>
            {toast && (
                <div style={{
                    position: 'fixed', top: '20px', right: '20px', padding: '10px 20px',
                    borderRadius: '8px', background: toast.type === 'success' ? 'var(--success)' : 'var(--error)',
                    color: '#fff', fontSize: '13px', zIndex: 9999, boxShadow: '0 4px 12px rgba(0,0,0,0.2)',
                }}>{toast.msg}</div>
            )}

            {/* Header */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '24px' }}>
                <div>
                    <h1 style={{ fontSize: '20px', fontWeight: 600, marginBottom: '4px' }}>
                        {t('admin.title', 'Company Management')}
                    </h1>
                    <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>
                        {t('admin.desc', 'Manage all companies, create new ones, and configure platform settings.')}
                    </p>
                </div>
                <button className="btn btn-primary" onClick={() => { setShowCreate(true); setCreatedCode(''); }}>
                    + {t('admin.createCompany', 'Create Company')}
                </button>
            </div>

            {/* Platform Settings */}
            <div className="card" style={{ padding: '16px', marginBottom: '16px' }}>
                <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '12px', color: 'var(--text-secondary)' }}>
                    {t('admin.platformSettings', 'Platform Settings')}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    {[
                        { key: 'allow_self_create_company', label: t('admin.allowSelfCreate', 'Allow users to create their own companies'), desc: t('admin.allowSelfCreateDesc', 'When disabled, only platform admins can create companies.') },
                        { key: 'invitation_code_enabled', label: t('admin.inviteCodeRequired', 'Require invitation code for registration'), desc: t('admin.inviteCodeRequiredDesc', 'When enabled, new users must provide a valid invitation code to register.') },
                    ].map(s => (
                        <div key={s.key} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 0' }}>
                            <div>
                                <div style={{ fontSize: '13px', fontWeight: 500 }}>{s.label}</div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{s.desc}</div>
                            </div>
                            <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: settingsLoading ? 'not-allowed' : 'pointer', flexShrink: 0 }}>
                                <input type="checkbox" checked={!!settings[s.key]} onChange={(e) => handleToggleSetting(s.key, e.target.checked)} disabled={settingsLoading}
                                    style={{ opacity: 0, width: 0, height: 0 }} />
                                <span style={{ position: 'absolute', inset: 0, background: settings[s.key] ? '#22c55e' : 'var(--bg-tertiary)', borderRadius: '11px', transition: 'background 0.2s' }}>
                                    <span style={{ position: 'absolute', left: settings[s.key] ? '20px' : '2px', top: '2px', width: '18px', height: '18px', background: '#fff', borderRadius: '50%', transition: 'left 0.2s' }} />
                                </span>
                            </label>
                        </div>
                    ))}
                </div>
            </div>

            {/* Create Company Dialog */}
            {showCreate && (
                <div className="card" style={{ padding: '16px', marginBottom: '16px', border: '1px solid var(--accent-primary)' }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '12px' }}>
                        {t('admin.createCompany', 'Create Company')}
                    </div>
                    <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
                        <input className="form-input" value={newName} onChange={e => setNewName(e.target.value)}
                            placeholder={t('admin.companyNamePlaceholder', 'Company name')}
                            onKeyDown={e => e.key === 'Enter' && handleCreate()}
                            style={{ flex: 1 }} autoFocus />
                        <button className="btn btn-primary" onClick={handleCreate} disabled={creating || !newName.trim()}>
                            {creating ? '...' : t('common.create', 'Create')}
                        </button>
                        <button className="btn btn-secondary" onClick={() => { setShowCreate(false); setCreatedCode(''); }}>
                            {t('common.cancel', 'Cancel')}
                        </button>
                    </div>
                    {createdCode && (
                        <div style={{ padding: '12px', borderRadius: '8px', background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.2)' }}>
                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
                                {t('admin.adminInviteCode', 'Admin invite code (single-use, first user becomes org_admin):')}
                            </div>
                            <div style={{ fontFamily: 'monospace', fontSize: '18px', fontWeight: 700, letterSpacing: '3px', color: 'var(--success)' }}>
                                {createdCode}
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Company List */}
            <div className="card" style={{ padding: '0', overflow: 'hidden' }}>
                <div style={{
                    display: 'grid', gridTemplateColumns: '2fr 1fr 1fr 1fr 120px',
                    gap: '12px', padding: '10px 16px', fontSize: '11px', fontWeight: 600,
                    color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em',
                    borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)',
                }}>
                    <div>{t('admin.company', 'Company')}</div>
                    <div>{t('admin.users', 'Users')}</div>
                    <div>{t('admin.agents', 'Agents')}</div>
                    <div>{t('admin.tokens', 'Token Usage')}</div>
                    <div>{t('admin.status', 'Status')}</div>
                </div>

                {loading && (
                    <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                        {t('common.loading', 'Loading...')}
                    </div>
                )}

                {error && (
                    <div style={{ textAlign: 'center', padding: '24px', color: 'var(--error)', fontSize: '13px' }}>
                        {error}
                    </div>
                )}

                {!loading && companies.map((c: any) => (
                    <div key={c.id} style={{
                        display: 'grid', gridTemplateColumns: '2fr 1fr 1fr 1fr 120px',
                        gap: '12px', padding: '12px 16px', alignItems: 'center',
                        borderBottom: '1px solid var(--border-subtle)', fontSize: '13px',
                        opacity: c.is_active ? 1 : 0.5,
                    }}>
                        <div>
                            <div style={{ fontWeight: 500 }}>{c.name}</div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'monospace' }}>{c.slug}</div>
                        </div>
                        <div>{c.user_count ?? '-'}</div>
                        <div>{c.agent_count ?? '-'}</div>
                        <div style={{ fontSize: '12px' }}>{c.total_tokens != null ? c.total_tokens.toLocaleString() : '-'}</div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <span className={`badge ${c.is_active ? 'badge-success' : 'badge-error'}`} style={{ fontSize: '10px' }}>
                                {c.is_active ? t('admin.active', 'Active') : t('admin.disabled', 'Disabled')}
                            </span>
                            <button
                                className="btn btn-ghost"
                                style={{ padding: '2px 6px', fontSize: '10px', color: c.is_active ? 'var(--error)' : 'var(--success)' }}
                                onClick={() => handleToggle(c.id, c.is_active)}
                            >
                                {c.is_active ? t('admin.disable', 'Disable') : t('admin.enable', 'Enable')}
                            </button>
                        </div>
                    </div>
                ))}

                {!loading && companies.length === 0 && !error && (
                    <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                        {t('common.noData', 'No data')}
                    </div>
                )}
            </div>
        </div>
    );
}
