import { useTranslation } from 'react-i18next';
import { IconLock, IconX } from '@tabler/icons-react';
import type { AgentAccessUser } from './OrgMemberAccessPicker';
import OrgMemberIdentitySummary from './OrgMemberIdentitySummary';
import Avatar from './ui/Avatar';

type Props = {
    user: AgentAccessUser;
    subtitle: string;
    onLevelChange?: (level: 'use' | 'manage') => void;
    onRemove?: () => void;
};

export default function OrgAccessMemberRow({ user, subtitle, onLevelChange, onRemove }: Props) {
    const { t } = useTranslation();
    return (
        <div className="org-access-picker__selected-row">
            <Avatar
                className="org-access-picker__avatar"
                src={user.avatar_url}
                name={user.name.trim().slice(-2) || '?'}
            />
            <div className="org-access-picker__selected-copy">
                <strong>{[user.name, user.phone_masked].filter(Boolean).join(' · ')}</strong>
                <small>{subtitle}</small>
                <OrgMemberIdentitySummary
                    directorySources={user.directory_sources}
                    channelBindings={user.channel_bindings}
                />
                {user.is_required && <small>{t(user.required_reason === 'creator'
                    ? 'accessPicker.creator' : 'accessPicker.companyAdmins')}</small>}
            </div>
            {user.is_required ? (
                <span className="org-access-picker__readonly-level">
                    <IconLock size={13} aria-hidden="true" />
                    {t('accessPicker.manage')}
                </span>
            ) : <>
                {onLevelChange && <select
                    value={user.access_level}
                    onChange={event => onLevelChange(event.target.value as 'use' | 'manage')}
                    aria-label={t('accessPicker.accessLevel', { name: user.name })}
                >
                    <option value="use">{t('accessPicker.use')}</option>
                    <option value="manage">{t('accessPicker.manage')}</option>
                </select>}
                {onRemove && <button type="button" onClick={onRemove} aria-label={`${t('common.cancel')} ${user.name}`}>
                    <IconX size={14} />
                </button>}
            </>}
        </div>
    );
}
