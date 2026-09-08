import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconPlugConnected, IconPlus, IconKey } from '@tabler/icons-react';
import { Modal, useDialog } from '../../../../components/Dialog/DialogProvider';
import { SettingsDrawer } from '../../../../components/ui/SettingsForm';
import { useToast } from '../../../../components/Toast/ToastProvider';
import Button from '../../../../components/ui/Button';
import LinearCopyButton from '../../../../components/LinearCopyButton';
import { fetchJson } from '../../../../services/api';
import ApplicationEditor from './ApplicationEditor';
import { applicationPath, type Application, type ApplicationConfig, type AuditEntry } from './types';
import './OpenAPIApplications.css';

export default function OpenAPIApplications({ tenantId }: { tenantId: string }) {
    const { t, i18n } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const queryClient = useQueryClient();
    const queryKey = ['enterprise-openapi', tenantId];
    const path = (suffix = '') => `${applicationPath}${suffix}?tenant_id=${encodeURIComponent(tenantId)}`;
    const query = useQuery({ queryKey, queryFn: ({ signal }) => fetchJson<Application[]>(path(), { signal }), enabled: !!tenantId });
    const [editor, setEditor] = useState<{ application: Application | null } | null>(null);
    const [secret, setSecret] = useState<Application | null>(null);
    const [audit, setAudit] = useState<{ name: string; rows: AuditEntry[] } | null>(null);
    const [busy, setBusy] = useState(false);
    const refresh = () => void queryClient.invalidateQueries({ queryKey });
    const save = async (value: ApplicationConfig) => {
        const application = editor?.application;
        const result = await fetchJson<Application>(path(application ? `/${application.id}` : ''), {
            method: application ? 'PUT' : 'POST', body: JSON.stringify(value),
        });
        setEditor(null);
        if (result.client_secret) setSecret(result);
        toast.success(t('openapi.saved'));
        refresh();
    };
    const action = async (item: Application, operation: 'credentials' | 'rotate-secret' | 'revoke' | 'audit') => {
        if (busy) return;
        setBusy(true);
        try {
            if (operation === 'credentials') {
                setSecret(await fetchJson<Application>(path(`/${item.id}/credentials`), { method: 'POST', cache: 'no-store' }));
                return;
            }
            if (operation === 'audit') {
                const rows = await fetchJson<AuditEntry[]>(path(`/${item.id}/audit`));
                setAudit({ name: item.name, rows });
                return;
            }
            const confirmed = await dialog.confirm(t(operation === 'revoke' ? 'openapi.revokeConfirm' : 'openapi.rotateConfirm', { name: item.name }), {
                title: t('openapi.confirmTitle'), danger: operation === 'revoke',
                confirmLabel: t(operation === 'revoke' ? 'openapi.revoke' : 'openapi.rotate'),
            });
            if (!confirmed) return;
            const result = await fetchJson<Application>(path(`/${item.id}/${operation}`), { method: 'POST' });
            if (result.client_secret) setSecret(result);
            toast.success(t(operation === 'revoke' ? 'openapi.revokeSuccess' : 'openapi.rotateSuccess'));
            refresh();
        } catch {
            toast.error(t('openapi.operationError'));
        } finally {
            setBusy(false);
        }
    };
    const copy = (value: string) => <LinearCopyButton textToCopy={value}
        label={t('openapi.copy')} copiedLabel={t('openapi.copied')} iconOnly />;
    return <section className="openapi-applications">
        <header className="openapi-applications__header">
            <div><h2>{t('openapi.title')}</h2><p>{t('openapi.description')}</p></div>
            <Button variant="primary" disabled={busy || !tenantId || query.isPending || query.isError}
                onClick={() => setEditor({ application: null })}><IconPlus size={16} />{t('openapi.create')}</Button>
        </header>
        {!tenantId ? <p role="status">{t('openapi.selectCompany')}</p> : query.isPending ?
            <p className="openapi-applications__loading" role="status">{t('common.loading')}</p> : query.isError ?
                <div className="openapi-error" role="alert">{t('openapi.loadError')}
                    <Button variant="secondary" onClick={() => void query.refetch()}>{t('openapi.retry')}</Button>
                </div> : !query.data?.length ?
                    <div className="card empty-state openapi-applications__empty">
                        <IconPlugConnected size={28} stroke={1.4} aria-hidden="true" />
                        <h3>{t('openapi.empty')}</h3><p>{t('openapi.emptyHint')}</p>
                    </div> : <div className="openapi-applications__list">
                        {query.data.map(item => {
                            const expired = item.expires_at && new Date(item.expires_at).getTime() <= Date.now();
                            const status = item.revoked_at ? 'revoked' : expired ? 'expired' : item.enabled ? 'enabled' : 'disabled';
                            return <article className="card openapi-application" key={item.id}>
                                <div className="openapi-application__heading">
                                    <h3>{item.name}</h3>
                                    <span className={`badge ${status === 'enabled' ? 'badge-success' : ''}`}>{t(`openapi.${status}`)}</span>
                                </div>
                                <div className="openapi-application__id"><span>{t('openapi.clientId')}</span><code>{item.client_id}</code>{copy(item.client_id)}</div>
                                <div className="openapi-application__footer">
                                    <div className="openapi-application__scopes">
                                        {item.scopes.map(scope => <span key={scope} className="badge">{t(scope === 'employees:read' ? 'openapi.employeeScope' : 'openapi.loginScope')}</span>)}
                                        {!item.scopes.length && <span>{t('openapi.noScopes')}</span>}
                                    </div>
                                    <div className="openapi-application__actions">
                                        <Button variant="ghost" disabled={busy || !!item.revoked_at} onClick={() => void action(item, 'credentials')}>{t('openapi.viewCredentials')}</Button>
                                        <Button variant="secondary" disabled={busy || !!item.revoked_at} onClick={() => setEditor({ application: item })}>{t('openapi.edit')}</Button>
                                        <Button variant="ghost" disabled={busy || !!item.revoked_at} onClick={() => void action(item, 'rotate-secret')}>{t('openapi.rotate')}</Button>
                                        <Button variant="ghost" disabled={busy} onClick={() => void action(item, 'audit')}>{t('openapi.audit')}</Button>
                                        <Button variant="ghost" className="openapi-application__revoke" disabled={busy || !!item.revoked_at} onClick={() => void action(item, 'revoke')}>{t('openapi.revoke')}</Button>
                                    </div>
                                </div>
                            </article>;
                        })}
                    </div>}
        {editor && <ApplicationEditor application={editor.application} onSave={save} onClose={() => setEditor(null)} />}
        <Modal open={!!secret} onClose={() => setSecret(null)} ariaLabel={t('openapi.secretTitle')} className="app-modal-surface--compact openapi-secret">
            <header className="openapi-secret__header"><IconKey size={20} stroke={1.5} aria-hidden="true" />
                <h2>{t('openapi.secretTitle')}</h2></header>
            <p>{secret?.name}</p>
            <div className="openapi-secret__credential"><label>{t('openapi.clientId')}</label><div><code>{secret?.client_id}</code>{copy(secret?.client_id || '')}</div></div>
            <div className="openapi-secret__credential"><label>{t('openapi.clientSecret')}</label><div><code>{secret?.client_secret}</code>{copy(secret?.client_secret || '')}</div></div>
            <footer className="openapi-secret__footer"><Button variant="primary" onClick={() => setSecret(null)}>{t('openapi.dismiss')}</Button></footer>
        </Modal>
        {audit && <SettingsDrawer title={t('openapi.audit')} description={audit.name} onClose={() => setAudit(null)}
            footer={<Button variant="secondary" onClick={() => setAudit(null)}>{t('openapi.dismiss')}</Button>}>
            {!audit.rows.length && <p className="openapi-applications__loading">{t('openapi.noAudit')}</p>}
            {audit.rows.map(row => {
                const actionKey = `openapi.auditActions.${row.action.replace('openapi.', '').replace(/\./g, '_')}`;
                return <div className="openapi-audit-row" key={row.id}>
                    <div><strong>{i18n.exists(actionKey) ? t(actionKey) : t('openapi.auditActions.request')}</strong>
                        <span>{t(row.details.outcome === 'success' || /^2\d\d:/.test(row.details.outcome || '')
                            ? 'openapi.auditSuccess' : 'openapi.auditFailure')}</span></div>
                    <time>{new Date(row.created_at).toLocaleString(i18n.language)}</time>
                </div>;
            })}
        </SettingsDrawer>}
    </section>;
}
