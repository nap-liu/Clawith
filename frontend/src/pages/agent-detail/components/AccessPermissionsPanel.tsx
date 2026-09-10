import { useTranslation } from 'react-i18next';
import type { QueryClient } from '@tanstack/react-query';
import AgentPermissionsEditor, {
    agentPermissionsValue, permissionGrants, type AgentPermissionsValue,
} from '../../../components/AgentPermissionsEditor';
import { fetchAuth } from '../utils/fetchAuth';

export default function AccessPermissionsPanel({
    agentId, permData, canManage, queryClient,
}: {
    agentId: string;
    permData: any;
    canManage: boolean;
    queryClient: QueryClient;
}) {
    const { t } = useTranslation();
    const value = agentPermissionsValue(permData || { grants: [] });
    const save = async (next: AgentPermissionsValue) => {
        await fetchAuth(`/agents/${agentId}/permissions`, {
            method: 'PUT', body: JSON.stringify({ grants: permissionGrants(next) }),
        });
        await Promise.all([
            queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] }),
            queryClient.invalidateQueries({ queryKey: ['agent', agentId] }),
            queryClient.invalidateQueries({ queryKey: ['agents'] }),
        ]);
    };
    return (
        <div className="card" style={{ marginBottom: 12 }}>
            <h4 style={{ marginBottom: 16 }}>{t('agentPermissions.title')}</h4>
            <AgentPermissionsEditor agentId={agentId} value={value} onChange={save}
                disabled={!permData || !(permData?.can_manage ?? canManage)} />
        </div>
    );
}
