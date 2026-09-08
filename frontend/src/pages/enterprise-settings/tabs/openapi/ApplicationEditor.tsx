import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import Button from '../../../../components/ui/Button';
import TextInput, { TextArea } from '../../../../components/ui/TextInput';
import { SettingsDrawer, SettingsField, SettingsSection, SettingsToggle } from '../../../../components/ui/SettingsForm';
import { configOf, emptyConfig, type Application, type ApplicationConfig } from './types';

function localDate(value: string | null) {
    if (!value) return '';
    const date = new Date(value);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function origins(value: string) {
    return [...new Set(value.split(/[\n,，]/).map(part => part.trim()).filter(Boolean))];
}

function validOrigin(value: string) {
    try {
        const url = new URL(value);
        return (url.protocol === 'https:' || (url.protocol === 'http:' &&
            ['localhost', '127.0.0.1', '[::1]', 'host.docker.internal'].includes(url.hostname))) &&
            !url.username && !url.password && !url.search && !url.hash && url.pathname === '/' &&
            !value.includes('\\');
    } catch {
        return false;
    }
}

export default function ApplicationEditor({ application, onSave, onClose }: {
    application: Application | null;
    onSave: (value: ApplicationConfig) => Promise<void>;
    onClose: () => void;
}) {
    const { t } = useTranslation();
    const formId = useId();
    const [draft, setDraft] = useState(() => application ? configOf(application) : emptyConfig());
    const [embed, setEmbed] = useState(draft.embed_origins.join('\n'));
    const [redirect, setRedirect] = useState(draft.redirect_origins.join('\n'));
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const update = <K extends keyof ApplicationConfig>(key: K, value: ApplicationConfig[K]) =>
        setDraft(current => ({ ...current, [key]: value }));
    const submit = async () => {
        if (busy) return;
        const embedOrigins = origins(embed);
        const redirectOrigins = origins(redirect);
        if (![...embedOrigins, ...redirectOrigins].every(validOrigin) ||
            embedOrigins.length > 20 || redirectOrigins.length > 20) {
            setError(t('openapi.invalidOrigins'));
            return;
        }
        setBusy(true);
        setError('');
        try {
            await onSave({ ...draft, name: draft.name.trim(), embed_origins: embedOrigins, redirect_origins: redirectOrigins });
        } catch {
            setError(t('openapi.operationError'));
        } finally {
            setBusy(false);
        }
    };
    return <SettingsDrawer title={t(application ? 'openapi.editTitle' : 'openapi.create')}
        description={t('openapi.editorDescription')} busy={busy} onClose={onClose}
        footer={<>
            <Button type="button" variant="secondary" disabled={busy} onClick={onClose}>{t('openapi.cancel')}</Button>
            <Button type="submit" form={formId} variant="primary" disabled={busy || !draft.name.trim()}>
                {t(busy ? 'openapi.saving' : 'openapi.save')}
            </Button>
        </>}>
        <form id={formId} onSubmit={event => { event.preventDefault(); void submit(); }} aria-busy={busy}>
            {error && <p className="openapi-error" role="alert">{error}</p>}
            <SettingsSection title={t('openapi.basicSettings')}>
                <SettingsField label={t('openapi.name')} htmlFor={`${formId}-name`}>
                    <TextInput id={`${formId}-name`} required maxLength={100} autoComplete="off"
                        placeholder={t('openapi.namePlaceholder')} value={draft.name} disabled={busy}
                        onChange={event => update('name', event.target.value)} />
                </SettingsField>
                <SettingsToggle label={t('openapi.enableApplication')} description={t('openapi.enableHint')}
                    checked={draft.enabled} disabled={busy} onChange={value => update('enabled', value)} />
            </SettingsSection>
            <SettingsSection title={t('openapi.scopes')}>
                {(['employees:read', 'auth:login'] as const).map(scope => <SettingsToggle key={scope}
                    label={t(scope === 'employees:read' ? 'openapi.employeeScope' : 'openapi.loginScope')}
                    description={t(scope === 'employees:read' ? 'openapi.employeeHint' : 'openapi.loginHint')}
                    disabled={busy} checked={draft.scopes.includes(scope)}
                    onChange={value => update('scopes', value ? [...draft.scopes, scope] : draft.scopes.filter(item => item !== scope))} />)}
                <SettingsToggle label={t('openapi.trustIdentity')} description={t('openapi.identityHint')}
                    checked={draft.trust_user_identity} disabled={busy} onChange={value => update('trust_user_identity', value)} />
            </SettingsSection>
            <SettingsSection title={t('openapi.allowedOrigins')} description={t('openapi.originsHint')}>
                <SettingsField label={t('openapi.embedOrigins')} htmlFor={`${formId}-embed`} hint={t('openapi.embedHint')}>
                    <TextArea id={`${formId}-embed`} rows={2} value={embed} disabled={busy}
                        aria-describedby={`${formId}-embed-hint`} placeholder={t('openapi.originPlaceholder')}
                        onChange={event => setEmbed(event.target.value)} />
                </SettingsField>
                <SettingsField label={t('openapi.redirectOrigins')} htmlFor={`${formId}-redirect`} hint={t('openapi.redirectHint')}>
                    <TextArea id={`${formId}-redirect`} rows={2} value={redirect} disabled={busy}
                        aria-describedby={`${formId}-redirect-hint`} placeholder={t('openapi.originPlaceholder')}
                        onChange={event => setRedirect(event.target.value)} />
                </SettingsField>
            </SettingsSection>
            <SettingsSection title={t('openapi.usageLimits')}>
                <div className="settings-field-grid">
                    <SettingsField label={t('openapi.rateLimit')} htmlFor={`${formId}-rate`}>
                        <TextInput id={`${formId}-rate`} type="number" required min={1} max={10000}
                            value={draft.rate_limit_per_minute} disabled={busy}
                            onChange={event => update('rate_limit_per_minute', Number(event.target.value))} />
                    </SettingsField>
                    <SettingsField label={t('openapi.expiresAt')} htmlFor={`${formId}-expiry`}>
                        <TextInput id={`${formId}-expiry`} type="datetime-local" value={localDate(draft.expires_at)}
                            disabled={busy} onChange={event => update('expires_at', event.target.value ? new Date(event.target.value).toISOString() : null)} />
                    </SettingsField>
                </div>
            </SettingsSection>
        </form>
    </SettingsDrawer>;
}
