import { useTranslation } from 'react-i18next';

export type DirectorySourceSummary = {
    provider_id: string;
    provider_type: string;
    provider_name: string;
};

export type ChannelBindingSummary = {
    provider_id: string | null;
    provider_name?: string | null;
    provider_type?: string | null;
    channel_type: string;
    installation_scope?: string | null;
    id_type?: string | null;
};

export default function OrgMemberIdentitySummary({
    directorySources,
    channelBindings,
}: {
    directorySources?: DirectorySourceSummary[];
    channelBindings?: ChannelBindingSummary[];
}) {
    const { t } = useTranslation();
    const providerLabel = (item: DirectorySourceSummary | ChannelBindingSummary): string => {
        const channelType = 'channel_type' in item ? item.channel_type : '';
        const type = (item.provider_type || channelType || '').toLowerCase();
        const translatedTypes: Record<string, string> = {
            dingtalk: t('common.channels.dingtalk'),
            feishu: t('common.channels.feishu'),
            wecom: t('common.channels.wecom'),
            web: t('accessPicker.providerNames.platform'),
            platform: t('accessPicker.providerNames.platform'),
        };
        return translatedTypes[type] || item.provider_name?.trim() || type;
    };
    const providerNames = Array.from(new Set(
        [...(directorySources || []), ...(channelBindings || [])]
            .map(providerLabel)
            .filter((name): name is string => Boolean(name)),
    ));
    if (!providerNames.length) return null;

    return (
        <span
            aria-label={t('accessPicker.linkedProviders')}
            style={{ display: 'inline-flex', flexWrap: 'wrap', gap: '4px' }}
        >
            {providerNames.map((name) => (
                <span key={name} className="badge badge-info">{name}</span>
            ))}
        </span>
    );
}
