import { useTranslation } from 'react-i18next';

import ToolsManager from '../components/ToolsManager';

export default function ToolsTab({
    agentId,
    agentName = 'Agent',
    canManage,
}: {
    agentId: string;
    agentName?: string;
    canManage: boolean;
}) {
    const { t } = useTranslation();

    return (
        <div>
            <div style={{ marginBottom: '16px' }}>
                <h3 style={{ marginBottom: '4px' }}>{t('agent.toolMgmt.title')}</h3>
                <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.toolMgmt.description')}</p>
            </div>
            <ToolsManager agentId={agentId} agentName={agentName} canManage={canManage} />
        </div>
    );
}
