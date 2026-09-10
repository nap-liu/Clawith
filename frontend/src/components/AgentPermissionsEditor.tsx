import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import i18n from '../i18n';
import { IconBuilding, IconLock, IconUsers } from '@tabler/icons-react';
import RadioCardGroup from './ui/RadioCardGroup';
import Button from './ui/Button';
import { useDialog } from './Dialog/DialogProvider';
import { useToast } from './Toast/ToastProvider';
import OrgMemberAccessPicker, {
    type AgentAccessDepartment, type AgentAccessUser,
} from './OrgMemberAccessPicker';
import './AgentPermissionsEditor.css';

export type AgentGrant = {
    scope_type: 'company' | 'department' | 'user';
    scope_id: string | null;
    access_level: 'use' | 'manage';
};

export type AgentPermissionsValue = {
    company: 'off' | 'use' | 'manage';
    users: AgentAccessUser[];
    departments: AgentAccessDepartment[];
};

export function agentPermissionsValue(data: {
    grants: AgentGrant[];
    user_access?: AgentAccessUser[];
    department_access?: AgentAccessDepartment[];
}): AgentPermissionsValue {
    const users = new Map(data.user_access?.map(user => [user.id, user]));
    const departments = new Map(data.department_access?.map(department => [department.id, department]));
    return {
        company: data.grants.find(grant => grant.scope_type === 'company')?.access_level || 'off',
        users: data.grants.filter(grant => grant.scope_type === 'user').map(grant => ({
            id: grant.scope_id!, name: i18n.t('agentPermissions.unavailableMember'),
            ...users.get(grant.scope_id!),
            access_level: grant.access_level, is_required: false, required_reason: null,
        })),
        departments: data.grants.filter(grant => grant.scope_type === 'department').map(grant => ({
            id: grant.scope_id!, name: i18n.t('agentPermissions.unavailableDepartment'), path: '',
            ...departments.get(grant.scope_id!),
            access_level: grant.access_level,
        })),
    };
}

export function permissionGrants(value: AgentPermissionsValue): AgentGrant[] {
    const grants: AgentGrant[] = value.company === 'off' ? [] : [{
        scope_type: 'company', scope_id: null, access_level: value.company,
    }];
    grants.push(...value.users.filter(user => !user.is_required).map(user => ({
        scope_type: 'user' as const, scope_id: user.id, access_level: user.access_level,
    })));
    grants.push(...value.departments.map(department => ({
        scope_type: 'department' as const, scope_id: department.id, access_level: department.access_level,
    })));
    return grants;
}

export default function AgentPermissionsEditor({
    value, onChange, agentId = '', disabled = false,
}: {
    value: AgentPermissionsValue;
    onChange: (value: AgentPermissionsValue) => Promise<void>;
    agentId?: string;
    disabled?: boolean;
}) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const [open, setOpen] = useState(false);
    const [confirming, setConfirming] = useState(false);
    const [expanded, setExpanded] = useState(false);
    const [saving, setSaving] = useState(false);
    const [pendingCompany, setPendingCompany] = useState<AgentPermissionsValue['company'] | null>(null);
    const businessUsers = value.users.filter(user => !user.is_required);
    const hasSubjects = value.departments.length > 0 || businessUsers.length > 0;
    const isPrivate = value.company === 'off' && !hasSubjects;
    const save = async (next: AgentPermissionsValue) => {
        setSaving(true);
        try {
            await onChange(next);
        } catch (cause) {
            toast.error(t('agentPermissions.saveError'));
            throw cause;
        } finally {
            setSaving(false);
        }
    };
    const saveControl = (next: AgentPermissionsValue) => { void save(next).catch(() => {}); };
    const setCompany = async (company: AgentPermissionsValue['company']) => {
        if (saving || disabled || company === value.company) return;
        setPendingCompany(company);
        try {
            await save({ ...value, company });
        } catch {
            // Return to the persisted selection; save() reports the failure.
        } finally {
            setPendingCompany(null);
        }
    };
    const resetAccess = async () => {
        setConfirming(true);
        const confirmed = await dialog.confirm(t('agentPermissions.resetDescription'), {
            title: t('agentPermissions.resetTitle'),
            confirmLabel: t('agentPermissions.resetConfirm'),
            danger: true,
        });
        if (confirmed) saveControl({
            company: 'off', departments: [], users: value.users.filter(user => user.is_required),
        });
        setConfirming(false);
    };
    return (
        <div className="agent-permissions-editor" aria-busy={saving}>
            <section className="agent-permissions-editor__section" aria-label={t('agentPermissions.company')}>
                <div className="agent-permissions-editor__heading agent-permissions-editor__company-heading">
                    <h5><IconBuilding size={16} aria-hidden="true" />{t('agentPermissions.company')}</h5>
                    <span className="agent-permissions-editor__save-status" role="status" aria-live="polite">
                        {saving ? t('agentPermissions.saving') : ''}
                    </span>
                </div>
                <RadioCardGroup
                    appearance="segmented"
                    value={pendingCompany ?? value.company}
                    options={[
                        { value: 'off', label: t('agentPermissions.companyOff') },
                        { value: 'use', label: t('agentPermissions.companyUse') },
                        { value: 'manage', label: t('agentPermissions.companyManage') },
                    ]}
                    label={t('agentPermissions.company')}
                    disabled={disabled}
                    busy={saving}
                    onChange={company => void setCompany(company)}
                />
            </section>
            <section className="agent-permissions-editor__section" aria-label={t('agentPermissions.additional')}>
                <div className="agent-permissions-editor__row">
                    <div className="agent-permissions-editor__heading">
                        <h5><IconUsers size={16} aria-hidden="true" />{t('agentPermissions.additional')}</h5>
                        <p>{hasSubjects
                            ? t('agentPermissions.count', { departments: value.departments.length, users: businessUsers.length })
                            : t('agentPermissions.empty')}</p>
                    </div>
                    <Button type="button" variant="secondary" className="btn-sm"
                        disabled={disabled || saving} onClick={() => setOpen(true)}>
                        {t('agentPermissions.choose')}
                    </Button>
                </div>
                {hasSubjects && (
                    <div className="agent-permissions-editor__summary">
                        {[
                            { key: 'departments', items: value.departments, icon: <IconBuilding size={13} /> },
                            { key: 'members', items: businessUsers, icon: <IconUsers size={13} /> },
                        ].filter(group => group.items.length > 0).map(group => (
                            <div key={group.key} className="agent-permissions-editor__group">
                                <span className="agent-permissions-editor__group-label">
                                    <span aria-hidden="true">{group.icon}</span>{t(`agentPermissions.${group.key}`)}
                                </span>
                                <ul className="agent-permissions-editor__grants">
                                    {(expanded ? group.items : group.items.slice(0, 6)).map(subject => (
                                        <li key={subject.id} title={'path' in subject ? subject.path : subject.department_path}>
                                            <strong>{subject.name}</strong>
                                            {group.key === 'departments' && <span className="agent-permissions-editor__scope">
                                                {t('agentPermissions.descendants')}
                                            </span>}
                                            <span className={`agent-permissions-editor__level is-${subject.access_level}`}>
                                                {t(`agentPermissions.${subject.access_level}`)}
                                            </span>
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        ))}
                        {(value.departments.length > 6 || businessUsers.length > 6) && (
                            <Button type="button" variant="ghost" className="btn-sm" aria-expanded={expanded}
                                onClick={() => setExpanded(!expanded)}>
                                {t(expanded ? 'agentPermissions.showLess' : 'agentPermissions.showAll')}
                            </Button>
                        )}
                    </div>
                )}
            </section>
            <footer className="agent-permissions-editor__footer">
                <div className="agent-permissions-editor__builtin">
                    <IconLock size={14} aria-hidden="true" />
                    <span>{t('agentPermissions.builtin')}</span>
                </div>
                <Button type="button" variant="ghost" className="btn-sm"
                    disabled={disabled || saving || confirming || isPrivate} onClick={() => void resetAccess()}>
                    {t('agentPermissions.resetTitle')}
                </Button>
            </footer>
            <OrgMemberAccessPicker
                open={open} agentId={agentId}
                directoryBaseUrl={agentId ? undefined : '/agents/permissions/directory'}
                users={value.users} departments={value.departments}
                onClose={() => setOpen(false)}
                onSave={(users, departments) => save({ ...value, users, departments })}
            />
        </div>
    );
}
