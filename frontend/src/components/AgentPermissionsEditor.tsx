import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconBuilding, IconLock, IconUsers } from '@tabler/icons-react';
import SelectDropdown from './SelectDropdown';
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
            id: grant.scope_id!, name: grant.scope_id!,
            ...users.get(grant.scope_id!),
            access_level: grant.access_level, is_required: false, required_reason: null,
        })),
        departments: data.grants.filter(grant => grant.scope_type === 'department').map(grant => ({
            id: grant.scope_id!, name: grant.scope_id!, path: '',
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
    const [open, setOpen] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState(false);
    const businessUsers = value.users.filter(user => !user.is_required);
    const save = async (next: AgentPermissionsValue) => {
        setSaving(true);
        setError(false);
        try {
            await onChange(next);
        } catch (cause) {
            setError(true);
            throw cause;
        } finally {
            setSaving(false);
        }
    };
    const saveControl = (next: AgentPermissionsValue) => { void save(next).catch(() => {}); };
    return (
        <div className="agent-permissions-editor" aria-busy={saving}>
            <div className="agent-permissions-editor__row">
                <div>
                    <label className="agent-permissions-editor__label"><IconBuilding size={16} />{t('agentPermissions.company')}</label>
                    <p>{t('agentPermissions.companyDescription')}</p>
                </div>
                <SelectDropdown
                    value={value.company}
                    options={(['off', 'use', 'manage'] as const).map(level => ({
                        value: level, label: t(`agentPermissions.${level}`),
                    }))}
                    ariaLabel={t('agentPermissions.company')}
                    disabled={disabled || saving}
                    onChange={company => saveControl({ ...value, company })}
                />
            </div>
            <div className="agent-permissions-editor__row">
                <div>
                    <span className="agent-permissions-editor__label"><IconUsers size={16} />{t('agentPermissions.additional')}</span>
                    <p>{t('agentPermissions.count', { departments: value.departments.length, users: businessUsers.length })}</p>
                </div>
                <button type="button" className="btn btn-secondary btn-sm"
                    disabled={disabled || saving} onClick={() => setOpen(true)}>
                    {t('agentPermissions.choose')}
                </button>
            </div>
            {(value.departments.length > 0 || businessUsers.length > 0) && (
                <div className="agent-permissions-editor__grants">
                    {value.departments.map(department => (
                        <span key={`department:${department.id}`} title={department.path}>
                            {department.name} · {t('agentPermissions.descendants')} · {t(`agentPermissions.${department.access_level}`)}
                        </span>
                    ))}
                    {businessUsers.map(user => (
                        <span key={`user:${user.id}`}>{user.name} · {t(`agentPermissions.${user.access_level}`)}</span>
                    ))}
                </div>
            )}
            <p>{t('agentPermissions.additiveHint')}</p>
            <div className="agent-permissions-editor__builtin">
                <IconLock size={14} />{t('agentPermissions.builtin')}
            </div>
            <button type="button" className="btn btn-ghost btn-sm" disabled={disabled || saving}
                onClick={() => saveControl({
                    company: 'off', departments: [], users: value.users.filter(user => user.is_required),
                })}>
                {t('agentPermissions.onlyMe')}
            </button>
            {saving && <p role="status">{t('agentPermissions.saving')}</p>}
            {error && <p role="alert" className="agent-permissions-editor__error">{t('agentPermissions.saveError')}</p>}
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
