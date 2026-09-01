import { useTranslation } from 'react-i18next';
import { IconChevronDown } from '@tabler/icons-react';
import { shortIdentity, type ExecutionUserOption } from '../shared';

type ExecutionIdentityRailProps = {
    creatorId?: string | null;
    creatorName?: string | null;
    executionUserId?: string | null;
    executionUserName?: string | null;
    users: ExecutionUserOption[];
    canReassign: boolean;
    isPending: boolean;
    onChoose: () => void;
};

export default function ExecutionIdentityRail({
    creatorId,
    creatorName,
    executionUserId,
    executionUserName,
    users,
    canReassign,
    isPending,
    onChoose,
}: ExecutionIdentityRailProps) {
    const { t } = useTranslation();
    const effectiveExecutionUserId = executionUserId || creatorId || '';
    const labelFor = (userId?: string | null, preferredName?: string | null) => {
        if (preferredName) return preferredName;
        const user = users.find((item) => item.id === userId);
        return user?.display_name || user?.username || user?.email || shortIdentity(userId);
    };
    const creatorLabel = labelFor(creatorId, creatorName);
    const executionLabel = labelFor(effectiveExecutionUserId, executionUserName);

    return (
        <div className="execution-identity-rail" onClick={(event) => event.stopPropagation()}>
            <span className="execution-identity-person" title={creatorId || undefined}>
                <span className="execution-identity-label">{t('agent.aware.executionIdentity.createdBy')}</span>
                <span className="execution-identity-value">{creatorLabel}</span>
            </span>
            <span className="execution-identity-arrow" aria-hidden="true">→</span>
            <span className="execution-identity-person">
                <span className="execution-identity-label">{t('agent.aware.executionIdentity.runsAs')}</span>
                {canReassign ? (
                    <button
                        type="button"
                        className="execution-identity-picker-trigger"
                        aria-label={t('agent.aware.executionIdentity.executionUser')}
                        title={t('agent.aware.executionIdentity.chooseExecutionUser')}
                        disabled={isPending || !effectiveExecutionUserId}
                        onClick={onChoose}
                    >
                        <span>{executionLabel}</span>
                        <IconChevronDown size={13} stroke={1.8} aria-hidden="true" />
                    </button>
                ) : (
                    <span className="execution-identity-value" title={effectiveExecutionUserId || undefined}>
                        {executionLabel}
                    </span>
                )}
            </span>
        </div>
    );
}
