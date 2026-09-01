import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
    IconBuilding,
    IconEye,
    IconLock,
    IconSettings,
    IconUser,
} from '@tabler/icons-react';
import OrgMemberAccessPicker from '../../../components/OrgMemberAccessPicker';
import { fetchAuth } from '../utils/fetchAuth';
import type { AccessDepartment, AccessUser } from '../shared';

export default function AccessPermissionsPanel({
    agentId,
    permData,
    canManage,
    queryClient,
}: {
    agentId: string;
    permData: any;
    canManage: boolean;
    queryClient: any;
}) {
    const { t, i18n } = useTranslation();
    const isChinese = i18n.language?.startsWith('zh');
    const canManagePermissions = permData?.can_manage ?? canManage;
    const isOwner = permData?.is_owner ?? false;
    const currentScope = permData?.scope_type === 'user' ? 'private' : (permData?.scope_type || 'company');
    const currentAccessLevel = permData?.access_level || 'use';
    const [localScope, setLocalScope] = useState(currentScope);
    const [localAccessLevel, setLocalAccessLevel] = useState(currentAccessLevel);
    const [savingScope, setSavingScope] = useState<string | null>(null);
    const [permissionError, setPermissionError] = useState<string | null>(null);
    const [showMemberPicker, setShowMemberPicker] = useState(false);
    const userAccess: AccessUser[] = useMemo(
        () => (permData?.user_access || []).map((u: any) => ({
            id: u.id,
            name: u.name,
            username: u.username,
            email: u.email,
            title: u.title,
            avatar_url: u.avatar_url,
            department_path: u.department_path,
            access_level: u.access_level === 'manage' ? 'manage' : 'use',
            is_required: !!u.is_required,
            required_reason: u.required_reason || null,
        })),
        [permData?.user_access],
    );
    const departmentAccess: AccessDepartment[] = useMemo(
        () => (permData?.department_access || []).map((department: any) => ({
            id: department.id,
            name: department.name,
            path: department.path,
            access_level: department.access_level === 'manage' ? 'manage' : 'use',
            include_descendants: true,
        })),
        [permData?.department_access],
    );
    const toPermissionPayloadScope = (scope: string) => scope === 'private' ? 'user' : scope;
    const businessUsers = userAccess.filter(user => !user.is_required);
    const requiredUsers = userAccess.filter(user => user.is_required);

    useEffect(() => {
        setLocalScope(currentScope);
        setLocalAccessLevel(currentAccessLevel);
    }, [currentScope, currentAccessLevel]);

    const savePermissions = async (payload: any) => {
        setPermissionError(null);
        await fetchAuth(`/agents/${agentId}/permissions`, {
            method: 'PUT',
            body: JSON.stringify(payload),
        });
        queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] });
        queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
        queryClient.invalidateQueries({ queryKey: ['agents'] });
    };

    const scopeOptions = [
        {
            value: 'company',
            icon: <IconBuilding size={14} stroke={1.8} />,
            label: t('agent.settings.perm.companyWide', 'Company-wide'),
            desc: isChinese ? '所有平台用户和数字员工都可以访问。' : 'All platform users and digital employees can access it.',
        },
        {
            value: 'private',
            icon: <IconUser size={14} stroke={1.8} />,
            label: t('agent.settings.perm.onlyMe', 'Only Me'),
            desc: isChinese ? '只有创建者可以使用和管理。' : 'Only the creator can use and manage it.',
        },
        {
            value: 'custom',
            icon: <IconLock size={14} stroke={1.8} />,
            label: isChinese ? '指定访问' : 'Custom',
            desc: isChinese ? '指定可访问的部门或成员；数字员工关系请在“关系”里配置。' : 'Choose departments or members. Digital employee relationships are configured in Relationships.',
        },
    ] as const;

    const accessLevels = [
        { val: 'use', label: <><IconEye size={13} stroke={1.8} /> {t('agent.settings.perm.useAccess', 'Use')}</>, desc: t('agent.settings.perm.useAccessDesc', 'Task, Chat, Tools, Skills, Workspace') },
        { val: 'manage', label: <><IconSettings size={13} stroke={1.8} /> {t('agent.settings.perm.manageAccess', 'Manage')}</>, desc: t('agent.settings.perm.manageAccessDesc', 'Full access including Settings, Mind, Relationships') },
    ];

    const setScope = async (scope: string) => {
        if (!canManagePermissions) return;
        if (scope === 'private' && !isOwner) {
            setPermissionError(isChinese ? '仅创建者可以切换为“仅我可见”，否则管理员会立即失去管理入口。' : 'Only the creator can switch to Only Me, otherwise the manager would lose access immediately.');
            return;
        }
        const previousScope = localScope;
        setLocalScope(scope);
        setSavingScope(scope);
        try {
            await savePermissions({
                scope_type: toPermissionPayloadScope(scope),
                access_level: localAccessLevel,
                user_access: userAccess,
                department_access: departmentAccess,
            });
        } catch (e) {
            setLocalScope(previousScope);
            setPermissionError(e instanceof Error ? e.message : String(e));
            console.error('Failed to update permissions', e);
        } finally {
            setSavingScope(null);
        }
    };

    const setCompanyAccessLevel = async (level: string) => {
        const previousLevel = localAccessLevel;
        setLocalAccessLevel(level);
        setSavingScope(`level:${level}`);
        try {
            await savePermissions({
                scope_type: toPermissionPayloadScope(localScope),
                access_level: level,
                user_access: userAccess,
                department_access: departmentAccess,
            });
        } catch (e) {
            setLocalAccessLevel(previousLevel);
            setPermissionError(e instanceof Error ? e.message : String(e));
            console.error('Failed to update access level', e);
        } finally {
            setSavingScope(null);
        }
    };

    const saveCustomAccess = async (
        nextBusinessUsers: AccessUser[],
        nextDepartments: AccessDepartment[],
    ) => {
        try {
            await savePermissions({
                scope_type: 'custom',
                access_level: localAccessLevel,
                user_access: nextBusinessUsers,
                department_access: nextDepartments,
            });
        } catch (error) {
            setPermissionError(error instanceof Error ? error.message : String(error));
            throw error;
        }
    };

    return (
        <div className="card" style={{ marginBottom: '12px' }}>
            <h4 style={{ marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <IconLock size={16} stroke={1.8} /> {t('agent.settings.perm.title', 'Access Permissions')}
            </h4>
            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
                {t('agent.settings.perm.description', 'Control who can see and interact with this agent. Only the creator or admin can change this.')}
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '16px' }}>
                {scopeOptions.map((scope) => {
                    const disabled = !canManagePermissions || (scope.value === 'private' && !isOwner);
                    const selected = localScope === scope.value;
                    return (
                        <button
                            key={scope.value}
                            type="button"
                            disabled={!canManagePermissions}
                            onClick={() => setScope(scope.value)}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '10px',
                                width: '100%',
                                textAlign: 'left',
                                padding: '12px 14px',
                                borderRadius: '8px',
                                cursor: disabled ? 'not-allowed' : 'pointer',
                                border: selected ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                                background: selected ? 'rgba(99,102,241,0.06)' : 'transparent',
                                opacity: disabled ? 0.55 : 1,
                                transition: 'all 0.15s',
                            }}
                        >
                            <input
                                type="radio"
                                name="perm_scope"
                                checked={selected}
                                disabled={disabled}
                                readOnly
                                style={{ accentColor: 'var(--accent-primary)' }}
                            />
                            <div>
                                <div style={{ fontWeight: 500, fontSize: '13px', display: 'flex', alignItems: 'center', gap: '5px' }}>
                                    {scope.icon} {scope.label}
                                    {savingScope === scope.value && <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 400 }}>{isChinese ? '保存中...' : 'Saving...'}</span>}
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{scope.desc}</div>
                            </div>
                        </button>
                    );
                })}
            </div>

            {permissionError && (
                <div style={{ margin: '-4px 0 12px', fontSize: '12px', color: 'var(--error)' }}>
                    {permissionError}
                </div>
            )}

            {localScope === 'company' && canManagePermissions && (
                <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
                    <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '8px' }}>
                        {t('agent.settings.perm.defaultAccess', 'Default Access Level')}
                    </label>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        {accessLevels.map(opt => (
                            <label
                                key={opt.val}
                                style={{
                                    flex: 1,
                                    padding: '10px 12px',
                                    borderRadius: '8px',
                                    cursor: 'pointer',
                                    border: localAccessLevel === opt.val ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                                    background: localAccessLevel === opt.val ? 'rgba(99,102,241,0.06)' : 'transparent',
                                    transition: 'all 0.15s',
                                }}
                            >
                                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                    <input
                                        type="radio"
                                        name="access_level"
                                        checked={localAccessLevel === opt.val}
                                        onChange={() => setCompanyAccessLevel(opt.val)}
                                        style={{ accentColor: 'var(--accent-primary)' }}
                                    />
                                    <span style={{ fontWeight: 500, fontSize: '13px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>{opt.label}</span>
                                </div>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', marginLeft: '20px' }}>{opt.desc}</div>
                            </label>
                        ))}
                    </div>
                </div>
            )}

            {localScope === 'custom' && canManagePermissions && (
                <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px' }}>
                        <div style={{ minWidth: 0 }}>
                            <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                                <IconBuilding size={14} stroke={1.8} />
                                {isChinese
                                    ? `已指定 ${departmentAccess.length} 个部门节点、${businessUsers.length} 名成员`
                                    : `${departmentAccess.length} departments and ${businessUsers.length} members selected`}
                            </div>
                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                {isChinese
                                    ? `创建者及 ${Math.max(requiredUsers.length - 1, 0)} 名公司管理员保留管理权限`
                                    : `The creator and ${Math.max(requiredUsers.length - 1, 0)} company administrators retain manage access`}
                            </div>
                        </div>
                        <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            onClick={() => setShowMemberPicker(true)}
                            disabled={savingScope !== null}
                        >
                            {isChinese ? '选择部门或成员' : 'Choose Departments or Members'}
                        </button>
                    </div>
                    {(departmentAccess.length > 0 || businessUsers.length > 0) && (
                        <div className="agent-access-summary">
                            {departmentAccess.length > 0 && (
                                <div className="agent-access-summary__group">
                                    <div className="agent-access-summary__group-label">
                                        <IconBuilding size={13} stroke={1.8} />
                                        <span>{isChinese ? '部门权限' : 'Departments'}</span>
                                    </div>
                                    <div className="agent-access-summary__items">
                                        {departmentAccess.slice(0, 4).map(department => (
                                            <span key={department.id} className="agent-access-summary__item agent-access-summary__item--department" title={department.path}>
                                                <strong>{department.name}</strong>
                                                <span className="agent-access-summary__scope">{isChinese ? '含下级' : 'Descendants'}</span>
                                                <span className={`agent-access-summary__level is-${department.access_level}`}>
                                                    {department.access_level === 'manage' ? (isChinese ? '管理' : 'Manage') : (isChinese ? '使用' : 'Use')}
                                                </span>
                                            </span>
                                        ))}
                                        {departmentAccess.length > 4 && <span className="agent-access-summary__more">+{departmentAccess.length - 4}</span>}
                                    </div>
                                </div>
                            )}
                            {businessUsers.length > 0 && (
                                <div className="agent-access-summary__group">
                                    <div className="agent-access-summary__group-label">
                                        <IconUser size={13} stroke={1.8} />
                                        <span>{isChinese ? '成员权限' : 'Members'}</span>
                                    </div>
                                    <div className="agent-access-summary__items">
                                        {businessUsers.slice(0, 6).map(user => (
                                            <span key={user.id} className="agent-access-summary__item" title={user.department_path || user.email || user.name}>
                                                <strong>{user.name}</strong>
                                                <span className={`agent-access-summary__level is-${user.access_level}`}>
                                                    {user.access_level === 'manage' ? (isChinese ? '管理' : 'Manage') : (isChinese ? '使用' : 'Use')}
                                                </span>
                                            </span>
                                        ))}
                                        {businessUsers.length > 6 && <span className="agent-access-summary__more">+{businessUsers.length - 6}</span>}
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                    <OrgMemberAccessPicker
                        open={showMemberPicker}
                        agentId={agentId}
                        users={userAccess}
                        departments={departmentAccess}
                        onClose={() => setShowMemberPicker(false)}
                        onSave={saveCustomAccess}
                    />
                </div>
            )}

            {localScope !== 'company' && (
                <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    {isChinese ? '访问范围仅影响谁可以查看和使用该数字员工。' : 'Access scope controls who can view and use this digital employee.'}
                </div>
            )}

            {!canManagePermissions && (
                <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-tertiary)', fontStyle: 'italic' }}>
                    {t('agent.settings.perm.readOnly', 'Only the creator or admin can change permissions')}
                </div>
            )}
        </div>
    );
}
