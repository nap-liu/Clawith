import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import Button from '../../components/ui/Button';
import TextInput from '../../components/ui/TextInput';
import Checkbox from '../../components/ui/Checkbox';
import SelectDropdown from '../../components/SelectDropdown';
import LinearCopyButton from '../../components/LinearCopyButton';
import ConfirmModal from '../../components/ConfirmModal';
import { adminApi, fetchJson } from '../../services/api';

type Application = {
    id: string; client_id: string; name: string; tenant_id: string;
    enabled: boolean; trust_user_identity: boolean; scopes: string[];
    embed_origins: string[]; redirect_origins: string[];
    rate_limit_per_minute: number; expires_at: string | null;
    revoked_at?: string | null; client_secret?: string; created_at?: string;
};
const path = '/admin/openapi/applications';
const empty = (): Application => ({
    id: '', client_id: '', name: '', tenant_id: '', enabled: true,
    trust_user_identity: false, scopes: ['employees:read', 'auth:login'],
    embed_origins: [], redirect_origins: [], rate_limit_per_minute: 120, expires_at: null,
});

export default function OpenAPIApplications() {
    const { t } = useTranslation();
    const [items, setItems] = useState<Application[]>([]);
    const [companies, setCompanies] = useState<{ id: string; name: string }[]>([]);
    const [draft, setDraft] = useState<Application | null>(null);
    const [secret, setSecret] = useState<Application | null>(null);
    const [embedOrigins, setEmbedOrigins] = useState('');
    const [redirectOrigins, setRedirectOrigins] = useState('');
    const [audit, setAudit] = useState<{ id: string; action: string; created_at: string; details: { outcome: string; request_id: string } }[] | null>(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [confirm, setConfirm] = useState<{ item: Application; action: 'revoke' | 'rotate-secret' } | null>(null);
    const load = async () => setItems(await fetchJson<Application[]>(path));
    useEffect(() => {
        Promise.all([load(), adminApi.listCompanies().then(setCompanies)])
            .catch(() => setError(t('openapi.loadError')));
    }, [t]);
    const update = <K extends keyof Application>(key: K, value: Application[K]) =>
        setDraft(current => current ? { ...current, [key]: value } : null);
    const edit = (item: Application) => {
        setDraft({ ...item }); setSecret(null); setAudit(null);
        setEmbedOrigins(item.embed_origins.join(', '));
        setRedirectOrigins(item.redirect_origins.join(', '));
    };
    const run = async (action: () => Promise<void>) => {
        setBusy(true); setError('');
        try { await action(); } catch { setError(t('openapi.operationError')); }
        finally { setBusy(false); }
    };
    const save = () => run(async () => {
        if (!draft) return;
        const { id, client_id, revoked_at, client_secret, created_at, ...body } = draft;
        body.embed_origins = embedOrigins.split(',').map(value => value.trim()).filter(Boolean);
        body.redirect_origins = redirectOrigins.split(',').map(value => value.trim()).filter(Boolean);
        const result = await fetchJson<Application>(id ? `${path}/${id}` : path, {
            method: id ? 'PUT' : 'POST', body: JSON.stringify(body),
        });
        if (result.client_secret) setSecret(result);
        setDraft(null); await load();
    });
    const confirmAction = () => run(async () => {
        if (!confirm) return;
        const result = await fetchJson<Application>(`${path}/${confirm.item.id}/${confirm.action}`, { method: 'POST' });
        if (result.client_secret) setSecret(result);
        setConfirm(null); await load();
    });
    return <section className="card" style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3>{t('openapi.title')}</h3>
            <Button onClick={() => edit(empty())} disabled={busy}>{t('openapi.create')}</Button>
        </div>
        <p style={{ color: 'var(--text-secondary)' }}>{t('openapi.description')}</p>
        {error && <p role="alert" style={{ color: 'var(--error)' }}>{error}</p>}
        {secret && <div className="card" style={{ padding: 12, marginBottom: 12 }}>
            <strong>{t('openapi.secretOnce')}</strong>
            <p>{t('openapi.clientId')}: <code>{secret.client_id}</code></p>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', overflowWrap: 'anywhere' }}>
                <code>{secret.client_secret}</code>
                <LinearCopyButton label={t("openapi.copy")} copiedLabel={t("openapi.copied")} textToCopy={secret.client_secret || ''} />
            </div>
            <Button onClick={() => setSecret(null)}>{t('openapi.dismiss')}</Button>
        </div>}
        {draft && <form onSubmit={event => { event.preventDefault(); void save(); }} style={{ display: 'grid', gap: 12, marginBlock: 16 }}>
            <label>{t('openapi.name')}<TextInput required maxLength={100} value={draft.name} onChange={event => update('name', event.target.value)} /></label>
            <label>{t('openapi.tenant')}<SelectDropdown value={draft.tenant_id} disabled={!!draft.id} ariaLabel={t('openapi.tenant')}
                options={[{ value: '', label: t('openapi.selectTenant') }, ...companies.map(company => ({ value: company.id, label: company.name }))]}
                onChange={value => update('tenant_id', value)} /></label>
            <label style={{ display: 'flex', gap: 8 }}><Checkbox checked={draft.enabled} onChange={event => update('enabled', event.target.checked)} />{t('openapi.enabled')}</label>
            <label style={{ display: 'flex', gap: 8 }}><Checkbox checked={draft.trust_user_identity} onChange={event => update('trust_user_identity', event.target.checked)} />{t('openapi.trustIdentity')}</label>
            <fieldset><legend>{t('openapi.scopes')}</legend>{['employees:read', 'auth:login'].map(scope => <label key={scope} style={{ display: 'flex', gap: 8 }}>
                <Checkbox checked={draft.scopes.includes(scope)} onChange={event => update('scopes', event.target.checked ? [...draft.scopes, scope] : draft.scopes.filter(value => value !== scope))} />
                {t(scope === 'employees:read' ? 'openapi.employeeScope' : 'openapi.loginScope')}
            </label>)}</fieldset>
            {(['embed_origins', 'redirect_origins'] as const).map(key => <label key={key}>{t(key === 'embed_origins' ? 'openapi.embedOrigins' : 'openapi.redirectOrigins')}
                <TextInput value={key === 'embed_origins' ? embedOrigins : redirectOrigins} placeholder="https://example.com"
                    onChange={event => (key === 'embed_origins' ? setEmbedOrigins : setRedirectOrigins)(event.target.value)} />
            </label>)}
            <label>{t('openapi.rateLimit')}<TextInput type="number" min={1} max={10000} required value={draft.rate_limit_per_minute} onChange={event => update('rate_limit_per_minute', Number(event.target.value))} /></label>
            <label>{t('openapi.expiresAt')}<TextInput type="datetime-local" value={draft.expires_at ? new Date(new Date(draft.expires_at).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : ''}
                onChange={event => update('expires_at', event.target.value ? new Date(event.target.value).toISOString() : null)} /></label>
            <div style={{ display: 'flex', gap: 8 }}><Button type="submit" variant="primary" disabled={busy || !draft.tenant_id}>{t('openapi.save')}</Button>
                <Button type="button" onClick={() => setDraft(null)}>{t('openapi.cancel')}</Button></div>
        </form>}
        <ConfirmModal open={!!confirm} title={t('openapi.confirmTitle')} danger
            message={confirm ? t(confirm.action === 'revoke' ? 'openapi.revokeConfirm' : 'openapi.rotateConfirm', { name: confirm.item.name }) : ''}
            confirmLabel={t('openapi.confirm')} cancelLabel={t('openapi.cancel')}
            onConfirm={() => { if (!busy) void confirmAction(); }}
            onCancel={() => { if (!busy) setConfirm(null); }} />
        {!items.length && <p>{t('openapi.empty')}</p>}
        {items.map(item => <div key={item.id} style={{ borderTop: '1px solid var(--border)', paddingBlock: 12 }}>
            <strong>{item.name}</strong> · {t(item.revoked_at ? 'openapi.revoked' : item.enabled ? 'openapi.enabled' : 'openapi.disabled')}
            <p style={{ color: 'var(--text-secondary)', fontSize: 12 }}>{t('openapi.clientId')}: {item.client_id}</p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                <Button disabled={busy || !!item.revoked_at} onClick={() => edit(item)}>{t('openapi.edit')}</Button>
                <Button disabled={busy || !!item.revoked_at} onClick={() => setConfirm({ item, action: 'rotate-secret' })}>{t('openapi.rotate')}</Button>
                <Button disabled={busy || !!item.revoked_at} onClick={() => setConfirm({ item, action: 'revoke' })}>{t('openapi.revoke')}</Button>
                <Button disabled={busy} onClick={() => void run(async () => setAudit(await fetchJson(`${path}/${item.id}/audit`)))}>{t('openapi.audit')}</Button>
            </div>
        </div>)}
        {audit && <div style={{ marginTop: 16 }}><h4>{t('openapi.audit')}</h4>
            {!audit.length && <p>{t('openapi.noAudit')}</p>}
            {audit.map(row => <div key={row.id} style={{ fontSize: 12, paddingBlock: 4 }}>
                {new Date(row.created_at).toLocaleString()} · {row.action} · {row.details.outcome}
                {row.details.request_id && <code> · {row.details.request_id}</code>}
            </div>)}
            <Button onClick={() => setAudit(null)}>{t('openapi.dismiss')}</Button>
        </div>}
    </section>;
}
