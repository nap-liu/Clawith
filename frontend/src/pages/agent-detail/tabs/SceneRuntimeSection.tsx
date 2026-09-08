import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import SelectDropdown from '../../../components/SelectDropdown';
import ToggleSwitch from '../../../components/ToggleSwitch';
import Button from '../../../components/ui/Button';
import { request } from '../../../services/api/core';
import type { Scene } from '../../../services/api/scenes';
import type { MCPServerEditorDraftOverride } from '../../../types/mcpServer';
import ToolsTab from './ToolsTab';

export default function SceneRuntimeSection({ agentId, value, disabled, onChange }: {
    agentId: string;
    value: Scene;
    disabled: boolean;
    onChange: (patch: Partial<Scene>) => void;
}) {
    const { t } = useTranslation();
    const [catalog, setCatalog] = useState<any[]>([]);
    const [initialOverrides, setInitialOverrides] = useState<Scene['mcp_server_overrides']>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(false);
    const [retry, setRetry] = useState(0);
    const custom = value.tools != null;

    useEffect(() => {
        let active = true;
        setLoading(true);
        setError(false);
        request<{ tools: any[]; mcp_server_overrides: Scene['mcp_server_overrides'] }>(`/agents/${agentId}/scenes/tool-options`)
            .then((result) => { if (active) { setCatalog(result.tools); setInitialOverrides(result.mcp_server_overrides); } })
            .catch(() => { if (active) setError(true); })
            .finally(() => { if (active) setLoading(false); });
        return () => { active = false; };
    }, [agentId, retry]);

    const draftTools = useMemo(() => {
        if (!custom) return catalog;
        const settings = new Map(value.tools!.map((item) => [item.tool_id, item]));
        return catalog.map((tool) => ({
            ...tool,
            enabled: tool.can_disable === false || (settings.get(tool.id)?.enabled ?? false),
            agent_config: settings.get(tool.id)?.config ?? {},
        }));
    }, [catalog, custom, value.tools]);
    const overrides = Object.fromEntries((value.mcp_server_overrides || []).map(({ server_id, ...item }) => [server_id, item]));
    const saveTools = (tools: any[]) => tools.map((tool) => ({
        tool_id: tool.id, enabled: tool.enabled, config: tool.agent_config || {},
    }));

    return (
        <section className="scene-config__panel">
            <div className="scene-config__runtime-settings">
                <div className="scene-config__runtime-row">
                    <div className="scene-config__enabled-control">
                        <span>{t('sceneRuntime.soul')}</span>
                        <ToggleSwitch checked={value.include_soul !== false} disabled={disabled}
                            ariaLabel={t('sceneRuntime.soul')} onChange={(include_soul) => onChange({ include_soul })} />
                    </div>
                    <div className="scene-config__enabled-control">
                        <span>{t('sceneRuntime.memory')}</span>
                        <ToggleSwitch checked={value.include_memory !== false} disabled={disabled}
                            ariaLabel={t('sceneRuntime.memory')} onChange={(include_memory) => onChange({ include_memory })} />
                    </div>
                    <div className="scene-config__runtime-tools">
                        <span>{t('sceneRuntime.tools')}</span>
                        <SelectDropdown value={custom ? 'custom' : 'inherit'}
                            ariaLabel={t('sceneRuntime.tools')} disabled={disabled || loading || error}
                            options={[
                                { value: 'inherit', label: t('sceneRuntime.inherit') },
                                { value: 'custom', label: t('sceneRuntime.custom') },
                            ]}
                            onChange={(mode) => onChange({
                                tools: mode === 'custom' ? saveTools(catalog) : null,
                                mcp_server_overrides: mode === 'custom' ? initialOverrides : [],
                            })} />
                    </div>
                </div>
                <p className="scene-config__runtime-hint">{t('sceneRuntime.memoryDescription')}</p>
            </div>
            {loading ? <p>{t('common.loading')}</p> : error ? (
                <p role="alert">{t('sceneRuntime.loadError')} <Button variant="ghost" onClick={() => setRetry(retry + 1)}>{t('sceneAuto.retry')}</Button></p>
            ) : (
                <ToolsTab agentId={agentId} canManage={custom && !disabled} canConfigure={custom && !disabled}
                    draftTools={draftTools}
                    onDraftToolsChange={(tools) => onChange({ tools: saveTools(tools) })}
                    draftMcpOverrides={overrides}
                    onDraftMcpOverridesChange={(next: Record<string, MCPServerEditorDraftOverride>) => onChange({
                        mcp_server_overrides: Object.entries(next).map(([server_id, item]) => ({ server_id, ...item })),
                    })} />
            )}
        </section>
    );
}
