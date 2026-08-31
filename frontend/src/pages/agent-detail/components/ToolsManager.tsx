import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import {
    IconBrowser,
    IconClock,
    IconFileText,
    IconMessageCircle,
    IconSearch,
    IconSettings,
    IconTerminal2,
    IconTrash,
    IconTools,
} from '@tabler/icons-react';

import { useDialog } from '../../../components/Dialog/DialogProvider';
import { useToast } from '../../../components/Toast/ToastProvider';
import ToggleSwitch from '../../../components/ToggleSwitch';
import { useAuthStore } from '../../../stores';
import MCPServerEditor from '../../../components/MCPServerEditor';
import { effectiveEditorRole } from '../../../components/MCPServerEditor/role';
import ToolCatalogPanel, { type ToolCatalogPanelGroup } from '../../../components/tools/ToolCatalogPanel';
import { getLocalizedToolPresentation } from '../../../utils/toolPresentation';
import { applyConfigDefaults, CATEGORY_CONFIG_SCHEMAS, getSensitiveKeys } from './toolsManager/config';
import { ToolConfigModal } from './toolsManager/ToolConfigModal';
import type { ToolsManagerProps } from './toolsManager/types';

export default function ToolsManager({
    agentId,
    canManage = false,
    canConfigure = canManage,
    scope = 'agent',
    projectContext,
    draftTools,
    onDraftToolsChange,
    draftMcpOverrides,
    onDraftMcpOverridesChange,
}: ToolsManagerProps) {
    const { t } = useTranslation();
    const dialog = useDialog();
    const toast = useToast();
    const currentUser = useAuthStore((s) => s.user);
    const tmQueryClient = useQueryClient();
    const [tools, setTools] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    const [configTool, setConfigTool] = useState<any | null>(null);
    const [configData, setConfigData] = useState<Record<string, any>>({});
    const [configJson, setConfigJson] = useState('');
    const [configSaving, setConfigSaving] = useState(false);
    const [toolTab, setToolTab] = useState<'company' | 'installed'>('company');
    const [deletingToolId, setDeletingToolId] = useState<string | null>(null);
    const [deletingMcpServerId, setDeletingMcpServerId] = useState<string | null>(null);
    const [configCategory, setConfigCategory] = useState<string | null>(null);
    const [focusedField, setFocusedField] = useState<string | null>(null);
    const [showAdvancedToolConfig, setShowAdvancedToolConfig] = useState(false);
    const [expandedCategories, setExpandedCategories] = useState<Set<string>>(() => new Set());
    const [toolSearch, setToolSearch] = useState('');
    const [toolStatusFilter, setToolStatusFilter] = useState<'all' | 'enabled' | 'disabled' | 'configured'>('all');
    const [mcpEditor, setMcpEditor] = useState<{ serverId: string; toolDisplayName: string } | null>(null);
    const [updatingCategories, setUpdatingCategories] = useState<Set<string>>(() => new Set());
    // Global (company-level) config for the currently open modal — used to show
    // lock hints and prevent agent from overriding company-set fields.
    const [configGlobalData, setConfigGlobalData] = useState<Record<string, any>>({});
    const assignmentUrl = projectContext
        ? `/api/projects/${projectContext.projectId}/members/${projectContext.memberId}/tools`
        : `/api/tools/agents/${agentId}`;

    const loadTools = async () => {
        if (draftTools) {
            setTools(draftTools);
            setLoading(false);
            return;
        }
        try {
            const token = localStorage.getItem('token');
            const res = await fetch(projectContext ? assignmentUrl : `${assignmentUrl}/with-config`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (res.ok) {
                const payload = await res.json();
                setTools(payload);
            }
            else {
                // Fallback to old endpoint
                const res2 = await fetch(assignmentUrl, { headers: { Authorization: `Bearer ${token}` } });
                if (res2.ok) {
                    const payload = await res2.json();
                    setTools(payload);
                }
            }
        } catch (e) { console.error(e); }
        setLoading(false);
    };

    useEffect(() => { loadTools(); }, [agentId, scope, projectContext?.projectId, projectContext?.memberId, draftTools]);

    const toggleTool = async (toolId: string, enabled: boolean) => {
        const previous = tools;
        const selected = tools.find(tool => tool.id === toolId);
        if (selected?.category === 'project_management') return;
        const affectedToolIds = new Set(
            selected?.category === 'subagent'
                ? tools.filter(tool => tool.category === selected.category).map(tool => tool.id)
                : [toolId],
        );
        setTools(prev => prev.map(tool => (
            affectedToolIds.has(tool.id) ? { ...tool, enabled } : tool
        )));
        if (draftTools && onDraftToolsChange) {
            onDraftToolsChange(tools.map(tool => (
                affectedToolIds.has(tool.id) ? { ...tool, enabled } : tool
            )));
            return;
        }
        try {
            const token = localStorage.getItem('token');
            const response = await fetch(assignmentUrl, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                body: JSON.stringify(
                    Array.from(affectedToolIds).map(id => ({ tool_id: id, enabled })),
                ),
            });
            if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `HTTP ${response.status}`);
            await tmQueryClient.invalidateQueries({ queryKey: ['agent', agentId] });
        } catch (e: any) {
            setTools(previous);
            toast.error(t('agent.tools.updateFailed', 'Tool update failed'), { details: scope === 'project' ? undefined : String(e?.message || e) });
        }
    };

    const openConfig = (tool: any) => {
        setConfigTool(tool);
        setShowAdvancedToolConfig(false);
        // Build merged config: start with global defaults, overlay agent overrides.
        // For sensitive fields, only use agent_config values (global ones are masked
        // like "****xxxx" and should not pre-fill the input).
        const sensitiveKeys = getSensitiveKeys(tool.config_schema);
        const globalCfg = tool.global_config || {};
        const agentCfg = tool.agent_config || {};
        const merged: Record<string, any> = {};
        for (const [k, v] of Object.entries(globalCfg)) {
            if (!sensitiveKeys.has(k)) merged[k] = v;
        }
        Object.assign(merged, agentCfg);
        Object.assign(merged, applyConfigDefaults(tool.config_schema?.fields || [], merged));
        setConfigData(merged);
        setConfigJson(JSON.stringify(agentCfg, null, 2));
        setFocusedField(null);
    };

    const openCategoryConfig = async (category: string) => {
        setConfigCategory(category);
        setShowAdvancedToolConfig(false);
        setConfigData({});
        setConfigGlobalData({});
        setConfigSaving(true);
        setFocusedField(null);
        if (draftTools) {
            const configured = draftTools.find(tool => tool.category === category)?.agent_config || {};
            setConfigData(configured);
            setConfigSaving(false);
            return;
        }
        try {
            const token = localStorage.getItem('token');
            const res = await fetch(`/api/tools/agents/${agentId}/category-config/${category}`, {
                headers: { Authorization: `Bearer ${token}` },
            });
            if (res.ok) {
                const data = await res.json();
                // global_config: company-level (masked sensitive fields like ****xxxx)
                // agent_config: agent-level overrides only
                const globalCfg = data.global_config || {};
                const agentCfg = data.agent_config || {};
                setConfigGlobalData(globalCfg);
                // Pre-fill only agent-level values; company fields show as hints
                const catSchema = CATEGORY_CONFIG_SCHEMAS[category];
                const sensitiveKeys = getSensitiveKeys(catSchema);
                const merged: Record<string, any> = {};
                for (const [k, v] of Object.entries(globalCfg)) {
                    // Non-sensitive global fields (e.g. os_type) pre-fill; sensitive ones don't
                    if (!sensitiveKeys.has(k)) merged[k] = v;
                }
                Object.assign(merged, agentCfg);
                setConfigData(merged);
            }
        } catch (e) { console.error(e); }
        setConfigSaving(false);
    };

    const saveConfig = async () => {
        if (!configTool && !configCategory) return;
        setConfigSaving(true);
        try {
            if (draftTools && onDraftToolsChange) {
                const targetConfig = configTool
                    ? (configTool.config_schema?.fields?.length > 0
                        ? configData
                        : JSON.parse(configJson || '{}'))
                    : configData;
                const next = draftTools.map(tool => (
                    configTool?.id === tool.id || (configCategory && tool.category === configCategory)
                        ? { ...tool, agent_config: targetConfig }
                        : tool
                ));
                setTools(next);
                onDraftToolsChange(next);
                setConfigTool(null);
                setConfigCategory(null);
                setConfigSaving(false);
                return;
            }
            const token = localStorage.getItem('token');

            if (configCategory) {
                const raw = configData;
                // Strip empty sensitive fields so untouched password inputs
                // don't send empty values that would clear an inherited company key
                const catSchema = CATEGORY_CONFIG_SCHEMAS[configCategory!];
                const sensitiveKeys = getSensitiveKeys(catSchema);
                const payload: Record<string, any> = {};
                for (const [k, v] of Object.entries(raw)) {
                    if (sensitiveKeys.has(k) && (v === '' || v === undefined || v === null)) continue;
                    payload[k] = v;
                }
                await fetch(`/api/tools/agents/${agentId}/category-config/${configCategory}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                    body: JSON.stringify({ config: payload }),
                });
                setConfigCategory(null);
            } else {
                const hasSchema = configTool.config_schema?.fields?.length > 0;
                const raw = hasSchema ? configData : JSON.parse(configJson || '{}');
                // Strip empty sensitive fields only — agent CAN override company values
                const sensitiveKeys = getSensitiveKeys(configTool.config_schema);
                const payload: Record<string, any> = {};
                for (const [k, v] of Object.entries(raw)) {
                    if (sensitiveKeys.has(k) && (v === '' || v === undefined || v === null)) continue;
                    payload[k] = v;
                }
                await fetch(`/api/tools/agents/${agentId}/tool-config/${configTool.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                    body: JSON.stringify({ config: payload }),
                });
                setConfigTool(null);
            }
            loadTools();
        } catch (e: any) { toast.error(t('common.error.saveFailed'), { details: scope === 'project' ? undefined : String(e?.message || e) }); }
        setConfigSaving(false);
    };

    if (loading) return <div style={{ color: 'var(--text-tertiary)', padding: '20px' }}>{t('common.loading')}</div>;

    // Company tools = platform presets (builtin) + company admin-added tools (admin)
    // Hide system-internal tools — they are protocol-level and not user-facing.
    // EXCEPT system tools that expose user config (e.g. request_confirmation's per-agent
    // DingTalk card template), which must stay configurable here.
    const isHiddenSystemTool = (t: any) => t.category === 'system' && !(t.config_schema?.fields?.length > 0);
    const isSelfInstalledTool = (tool: any) => (
        tool.agent_tool_source === 'user_installed'
        && tool.installed_by_agent_id === agentId
    );
    const companyTools = tools.filter(t => (
        !isSelfInstalledTool(t)
        && (t.source === 'builtin' || t.source === 'admin')
        && !isHiddenSystemTool(t)
    ));
    const agentInstalledTools = tools.filter(t => (
        isSelfInstalledTool(t)
        && !isHiddenSystemTool(t)
    ));

    const getToolGroupMeta = (groupKey: string, toolsInGroup: any[]) => {
        const first = toolsInGroup.find((tool) => tool.type === 'mcp' && tool.mcp_server_name) || toolsInGroup[0];
        const presentation = getLocalizedToolPresentation(t, first || {});
        return {
            label: presentation.groupLabel,
            description: presentation.groupDescription,
            iconCategory: groupKey.startsWith('mcp:') ? 'custom' : presentation.categoryKey,
            configCategory: presentation.categoryKey,
        };
    };

    const groupByCategory = (toolList: any[]) =>
        toolList.reduce((acc: Record<string, any[]>, tool) => {
            const cat = getLocalizedToolPresentation(t, tool).groupKey;
            (acc[cat] = acc[cat] || []).push(tool);
            return acc;
        }, {});
    const renderCategoryIcon = (category: string, size = 15) => {
        const style = { color: 'var(--text-tertiary)' };
        switch (category) {
            case 'agentbay': return <IconBrowser size={size} stroke={1.8} style={style} />;
            case 'browser': return <IconBrowser size={size} stroke={1.8} style={style} />;
            case 'file': return <IconFileText size={size} stroke={1.8} style={style} />;
            case 'communication':
            case 'feishu':
            case 'email':
            case 'social':
                return <IconMessageCircle size={size} stroke={1.8} style={style} />;
            case 'search':
            case 'discovery':
                return <IconSearch size={size} stroke={1.8} style={style} />;
            case 'code': return <IconTerminal2 size={size} stroke={1.8} style={style} />;
            case 'aware': return <IconClock size={size} stroke={1.8} style={style} />;
            case 'custom': return <IconSettings size={size} stroke={1.8} style={style} />;
            default: return <IconTools size={size} stroke={1.8} style={style} />;
        }
    };

    const toggleCategoryExpanded = (category: string) => {
        setExpandedCategories(prev => {
            const next = new Set(prev);
            if (next.has(category)) next.delete(category);
            else next.add(category);
            return next;
        });
    };

    const bulkToggleCategory = async (category: string, catTools: any[], enabled: boolean) => {
        if (updatingCategories.has(category)) return;
        const catToolIds = new Set(catTools.filter(t => t.can_disable !== false).map(t => t.id));
        if (catToolIds.size === 0) return;
        const previousEnabledById = new Map(
            tools
                .filter(tool => catToolIds.has(tool.id))
                .map(tool => [tool.id, !!tool.enabled]),
        );
        setUpdatingCategories(prev => new Set(prev).add(category));
        setTools(prev => prev.map(t => catToolIds.has(t.id) ? { ...t, enabled } : t));
        if (draftTools && onDraftToolsChange) {
            const next = tools.map(tool => catToolIds.has(tool.id) ? { ...tool, enabled } : tool);
            setTools(next);
            onDraftToolsChange(next);
            setUpdatingCategories(prev => {
                const updated = new Set(prev);
                updated.delete(category);
                return updated;
            });
            return;
        }
        try {
            const token = localStorage.getItem('token');
            const payload = Array.from(catToolIds).map(id => ({ tool_id: id, enabled }));
            const response = await fetch(assignmentUrl, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                body: JSON.stringify(payload),
            });
            if (!response.ok) {
                const body = await response.json().catch(() => null);
                const detail = typeof body?.detail === 'string'
                    ? body.detail
                    : typeof body?.detail?.message === 'string'
                        ? body.detail.message
                        : `HTTP ${response.status}`;
                throw new Error(detail);
            }
            await Promise.all([
                tmQueryClient.invalidateQueries({ queryKey: ['agent', agentId] }),
                tmQueryClient.invalidateQueries({ queryKey: ['agent-tools', agentId] }),
            ]);
            await loadTools();
        } catch (err: any) {
            setTools(prev => prev.map(tool => (
                previousEnabledById.has(tool.id)
                    ? { ...tool, enabled: previousEnabledById.get(tool.id) }
                    : tool
            )));
            toast.error(t('common.error.batchUpdateFailed', 'Batch update failed'), {
                details: scope === 'project' ? undefined : String(err?.message || err),
            });
        } finally {
            setUpdatingCategories(prev => {
                const next = new Set(prev);
                next.delete(category);
                return next;
            });
        }
    };

    const uninstallMcpGroup = async (serverId: string, label: string, toolCount: number) => {
        const ok = await dialog.confirm(
            `${t('common.confirmActions.deleteLabel', 'Delete')} “${label}” (${toolCount})?`,
            {
                title: t('agent.tools.deleteMcpGroupTitle', 'Delete MCP tool group'),
                danger: true,
                confirmLabel: t('common.delete', 'Delete'),
            },
        );
        if (!ok) return;

        setDeletingMcpServerId(serverId);
        try {
            const token = localStorage.getItem('token');
            const res = await fetch(`/api/tools/agents/${agentId}/mcp-servers/${serverId}`, {
                method: 'DELETE',
                headers: { Authorization: `Bearer ${token}` },
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail?.error || body?.detail || `HTTP ${res.status}`);
            }
            await loadTools();
            toast.success(t('agent.tools.mcpGroupRemoved', 'MCP group removed'));
        } catch (e: any) {
            toast.error(t('agent.tools.deleteFailed', 'Delete failed'), { details: scope === 'project' ? undefined : String(e?.message || e) });
        } finally {
            setDeletingMcpServerId(null);
        }
    };

    const deleteCompanyMcpGroup = async (serverId: string, label: string, toolCount: number) => {
        const ok = await dialog.confirm(
            `${t('common.confirmActions.deleteLabel', 'Delete')} “${label}” · ${t('agent.tools.companyTools', 'Company Tools')} (${toolCount})?`,
            {
                title: t('agent.tools.deleteCompanyMcpGroupTitle', 'Delete company MCP tool group'),
                danger: true,
                confirmLabel: t('common.delete', 'Delete'),
            },
        );
        if (!ok) return;

        setDeletingMcpServerId(serverId);
        try {
            const token = localStorage.getItem('token');
            const res = await fetch(`/api/admin/mcp-servers/${serverId}`, {
                method: 'DELETE',
                headers: { Authorization: `Bearer ${token}` },
            });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(body?.detail?.error || body?.detail || `HTTP ${res.status}`);
            }
            await loadTools();
            toast.success(t('agent.tools.companyMcpGroupDeleted', 'Company MCP tool group deleted'));
        } catch (e: any) {
            toast.error(t('agent.tools.deleteFailed', 'Delete failed'), { details: scope === 'project' ? undefined : String(e?.message || e) });
        } finally {
            setDeletingMcpServerId(null);
        }
    };

    const renderToolActions = (tool: any, category: string) => {
        const hasConfig = tool.config_schema?.fields?.length > 0 || tool.type === 'mcp';
        const isGlobalCategoryConfig = category === 'agentbay' && tool.name === 'agentbay_browser_navigate';
        const presentation = getLocalizedToolPresentation(t, tool);
        const toolDisplayName = presentation.name;
        const categoryOnlyToggle = category === 'project_management';
        return (
            <>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                    {!categoryOnlyToggle && tool.name === 'publish_page' && (
                        <a
                            href={`/published-pages?agent_id=${encodeURIComponent(agentId)}`}
                            onClick={event => event.stopPropagation()}
                            style={{ border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: '4px', textDecoration: 'none' }}
                        ><IconFileText size={12} stroke={1.8} /> {t('agent.tools.managePublishedContent')}</a>
                    )}
                    {!categoryOnlyToggle && canConfigure && tool.type === 'mcp' && tool.mcp_server_id && scope === 'agent' && (
                        <button
                            onClick={(e) => {
                                e.stopPropagation();
                                setMcpEditor({
                                    serverId: tool.mcp_server_id,
                                    toolDisplayName: tool.mcp_server_name || tool.display_name || tool.name,
                                });
                            }}
                            style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                            title={t('agent.tools.editMcpService')}
                        ><IconSettings size={12} stroke={1.8} /> {t('agent.tools.config', 'Config')}</button>
                    )}
                    {/* Non-MCP tools that have a config_schema still use the legacy openConfig path */}
                    {!categoryOnlyToggle && canConfigure && hasConfig && !isGlobalCategoryConfig && !(tool.type === 'mcp' && tool.mcp_server_id && scope === 'agent') && (
                        <button
                            onClick={() => openConfig(tool)}
                            style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                            title={t('agent.tools.config', 'Config')}
                        ><IconSettings size={12} stroke={1.8} /> {t('agent.tools.config', 'Config')}</button>
                    )}
                    {!categoryOnlyToggle && canConfigure && scope === 'agent' && !draftTools && tool.source === 'agent' && tool.agent_tool_id && (
                        <button
                            onClick={async () => {
                                const ok = await dialog.confirm(
                                    `${t('common.confirmActions.removeLabel', 'Remove')} “${toolDisplayName}”?`,
                                    { danger: true, confirmLabel: t('common.confirmActions.removeLabel') },
                                );
                                if (!ok) return;
                                setDeletingToolId(tool.id);
                                try {
                                    const token = localStorage.getItem('token');
                                    const res = await fetch(`/api/tools/agent-tool/${tool.agent_tool_id}`, {
                                        method: 'DELETE',
                                        headers: { Authorization: `Bearer ${token}` },
                                    });
                                    if (res.ok) await loadTools();
                                    else toast.error(t('agent.tools.deleteFailed', 'Delete failed'));
                                } catch (e: any) { toast.error(t('agent.tools.deleteFailed', 'Delete failed'), { details: String(e?.message || e) }); }
                                setDeletingToolId(null);
                            }}
                            disabled={deletingToolId === tool.id}
                            style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-tertiary)', opacity: deletingToolId === tool.id ? 0.5 : 1 }}
                            title={t('common.confirmActions.removeLabel', 'Remove')}
                        >{deletingToolId === tool.id ? '...' : '✕'}</button>
                    )}
                    {categoryOnlyToggle ? (
                        <span style={{ fontSize: '11px', color: tool.enabled ? 'var(--accent-primary)' : 'var(--text-tertiary)', fontWeight: 600, whiteSpace: 'nowrap' }}>
                            {tool.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                        </span>
                    ) : tool.can_disable === false ? (
                        <span style={{ fontSize: '11px', color: 'var(--accent-primary)', fontWeight: 600, whiteSpace: 'nowrap' }}>
                            {t('agent.tools.alwaysAvailable', 'Always available')}
                        </span>
                    ) : canManage ? (
                        <ToggleSwitch
                            checked={tool.enabled}
                            onChange={(checked) => void toggleTool(tool.id, checked)}
                            ariaLabel={`${toolDisplayName} ${tool.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}`}
                        />
                    ) : (
                        <span style={{ fontSize: '11px', color: tool.enabled ? 'var(--accent-primary)' : 'var(--text-tertiary)', fontWeight: 500 }}>
                            {tool.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                        </span>
                    )}
                </div>
            </>
        );
    };

    const renderToolBadges = (tool: any) => {
        const hasAgentOverride = tool.agent_config && Object.keys(tool.agent_config).length > 0;
        return (
            <>
                {tool.type === 'mcp' && <span className="tool-catalog-panel__badge is-mcp">MCP</span>}
                {tool.type === 'builtin' && (
                    <span className="tool-catalog-panel__badge">{t('agent.tools.builtin', 'Built-in')}</span>
                )}
                {hasAgentOverride && (
                    <span className="tool-catalog-panel__badge is-configured">{t('enterprise.tools.configured', 'Configured')}</span>
                )}
            </>
        );
    };

    const activeTools = toolTab === 'company'
        ? companyTools
        : agentInstalledTools;
    const matchesStatusFilter = (tool: any) => {
        if (toolStatusFilter === 'enabled') return !!tool.enabled;
        if (toolStatusFilter === 'disabled') return !tool.enabled;
        if (toolStatusFilter === 'configured') return !!(tool.agent_config && Object.keys(tool.agent_config).length > 0);
        return true;
    };
    const groupedActiveTools = groupByCategory(activeTools);
    const hasFilters = !!toolSearch.trim() || toolStatusFilter !== 'all';

    const renderCatalogGroupSummary = (group: ToolCatalogPanelGroup<any>) => {
        const enabledCount = group.allItems.filter(tool => tool.enabled).length;
        const configuredCount = group.allItems.filter(tool => tool.agent_config && Object.keys(tool.agent_config).length > 0).length;
        if (group.key === 'project_management') {
            const contract = group.allItems.find(tool => tool.capability_group)?.capability_group;
            const controllable = group.allItems.filter(tool => tool.can_disable !== false);
            const controllableEnabled = controllable.filter(tool => tool.enabled).length;
            const state = ['disabled', 'partial', 'enabled'].includes(contract?.state)
                ? String(contract.state)
                : controllableEnabled === 0
                    ? 'disabled'
                    : controllableEnabled === controllable.length
                        ? 'enabled'
                        : 'partial';
            return t(`agent.tools.capabilityGroupStates.${state}`, state);
        }
        return (
            <>
                {t('agent.tools.groupSummary', {
                    total: group.allItems.length,
                    enabled: enabledCount,
                    defaultValue: '{{total}} tools · {{enabled}} enabled',
                })}
                {group.items.length !== group.allItems.length
                    ? ` · ${t('agent.tools.groupShown', { count: group.items.length, defaultValue: '{{count}} shown' })}`
                    : ''}
                {configuredCount > 0 ? ` · ${t('agent.tools.groupConfigured', { count: configuredCount, defaultValue: '{{count}} configured' })}` : ''}
            </>
        );
    };

    const renderCatalogGroupActions = (group: ToolCatalogPanelGroup<any>) => {
        const meta = getToolGroupMeta(group.key, group.allItems);
        const controllableTools = group.allItems.filter(tool => tool.can_disable !== false);
        const enabledCount = controllableTools.filter(tool => tool.enabled).length;
        const allEnabled = controllableTools.length > 0 && enabledCount === controllableTools.length;
        const mixed = enabledCount > 0 && enabledCount < controllableTools.length;
        const updating = updatingCategories.has(group.key);
        const mcpServerId = group.allItems[0]?.mcp_server_id as string | undefined;
        const removableMcpGroup = scope === 'agent' && !draftTools && toolTab === 'installed'
            && group.key.startsWith('mcp:')
            && !!mcpServerId
            && group.allItems.every(tool => tool.agent_tool_source === 'user_installed' && tool.installed_by_agent_id === agentId);
        const deletableCompanyMcpGroup = scope === 'agent' && !draftTools && toolTab === 'company'
            && currentUser?.role === 'platform_admin'
            && group.key.startsWith('mcp:')
            && !!mcpServerId
            && group.allItems.every(tool => tool.type === 'mcp' && tool.source === 'admin' && tool.mcp_server_id === mcpServerId);
        return (
            <>
                {canConfigure && (removableMcpGroup || deletableCompanyMcpGroup) ? (
                    <button
                        type="button"
                        className="tool-catalog-panel__action is-danger"
                        onClick={() => void (deletableCompanyMcpGroup
                            ? deleteCompanyMcpGroup(mcpServerId!, group.label, group.allItems.length)
                            : uninstallMcpGroup(mcpServerId!, group.label, group.allItems.length))}
                        disabled={deletingMcpServerId === mcpServerId}
                    >
                        <IconTrash size={12} />
                        {deletingMcpServerId === mcpServerId ? '...' : t('common.delete', 'Delete')}
                    </button>
                ) : null}
                {CATEGORY_CONFIG_SCHEMAS[meta.configCategory] && canConfigure ? (
                    <button
                        type="button"
                        className="tool-catalog-panel__action"
                        onClick={() => openCategoryConfig(meta.configCategory)}
                    >
                        <IconSettings size={12} />
                        {t('agent.tools.config', 'Config')}
                    </button>
                ) : null}
                {canManage && controllableTools.length > 0 ? (
                    <ToggleSwitch
                        checked={allEnabled}
                        mixed={mixed}
                        disabled={updating}
                        onChange={(checked) => void bulkToggleCategory(group.key, group.allItems, checked)}
                        ariaLabel={t('agent.tools.enableDisableAll', 'Enable/Disable all {{category}} tools', { category: group.label })}
                    />
                ) : null}
            </>
        );
    };

    return (
        <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <div className="tool-source-tabs" role="tablist" aria-label={t('agent.tools.sourceTabs', 'Tool sources')}>
                    <button
                        type="button"
                        role="tab"
                        aria-selected={toolTab === 'company'}
                        className={toolTab === 'company' ? 'active' : ''}
                        onClick={() => setToolTab('company')}
                    >
                        <span>{t('agent.tools.companyTools', 'Company Tools')}</span>
                        <span className="tool-source-tab-count">{companyTools.length}</span>
                    </button>
                    <button
                        type="button"
                        role="tab"
                        aria-selected={toolTab === 'installed'}
                        className={toolTab === 'installed' ? 'active' : ''}
                        onClick={() => setToolTab('installed')}
                    >
                        <span>
                            {t('agent.sessionViewer.digitalEmployee', 'Digital Employee')}
                            {' · '}
                            {t('agent.tools.installed', 'Installed')}
                        </span>
                        <span className="tool-source-tab-count">{agentInstalledTools.length}</span>
                    </button>
                </div>

                <ToolCatalogPanel
                    items={activeTools.filter(matchesStatusFilter)}
                    allItems={activeTools}
                    getKey={(tool) => tool.id}
                    getPresentation={(tool) => getLocalizedToolPresentation(t, tool)}
                    searchValue={toolSearch}
                    onSearchChange={setToolSearch}
                    searchPlaceholder={t('agent.tools.searchTools', 'Search tools...')}
                    emptyLabel={hasFilters
                        ? t('agent.tools.noMatchingTools', 'No matching tools')
                        : toolTab === 'installed'
                            ? t('agent.tools.noInstalled', 'No agent-installed tools yet')
                            : t('agent.tools.noCompany', 'No company-configured tools')}
                    ariaLabel={t('agent.tools.sourceTabs', 'Tool sources')}
                    expandedGroups={expandedCategories}
                    onExpandedGroupsChange={setExpandedCategories}
                    renderGroupIcon={(group) => renderCategoryIcon(getToolGroupMeta(group.key, group.allItems).iconCategory, 16)}
                    renderGroupSummary={renderCatalogGroupSummary}
                    renderGroupActions={renderCatalogGroupActions}
                    renderGroupBody={(group) => group.key === 'project_management' ? (
                        <div className="tool-catalog-panel__group-note">
                            {t('agent.tools.projectManagementScope', 'Browse projects, follow progress, and coordinate project work.')}
                        </div>
                    ) : undefined}
                    renderItemBadges={renderToolBadges}
                    renderItemActions={(tool, group) => renderToolActions(tool, group.key)}
                    toolbar={(
                        <>
                            {(['all', 'enabled', 'disabled', 'configured'] as const).map(filter => (
                                <button
                                    key={filter}
                                    type="button"
                                    className={`tool-catalog-panel__filter${toolStatusFilter === filter ? ' is-active' : ''}`}
                                    onClick={() => setToolStatusFilter(filter)}
                                >
                                    {filter === 'all' ? t('common.all', 'All')
                                        : filter === 'enabled' ? t('common.enabled', 'Enabled')
                                            : filter === 'disabled' ? t('common.disabled', 'Disabled')
                                                : t('agent.tools.configured', 'Configured')}
                                </button>
                            ))}
                            <button
                                type="button"
                                className="tool-catalog-panel__action"
                                onClick={() => {
                                    const categories = Object.keys(groupedActiveTools);
                                    setExpandedCategories(prev => prev.size >= categories.length ? new Set() : new Set(categories));
                                }}
                            >
                                {expandedCategories.size >= Object.keys(groupedActiveTools).length
                                    ? t('agent.tools.collapseAll', 'Collapse all')
                                    : t('agent.tools.expandAll', 'Expand all')}
                            </button>
                        </>
                    )}
                />
            </div>
            {tools.length === 0 && (
                <div className="card" style={{ textAlign: 'center', padding: '30px', color: 'var(--text-tertiary)' }}>
                    {t('common.noData')}
                </div>
            )}

            <ToolConfigModal
                agentId={agentId}
                scope={scope}
                configTool={configTool}
                configCategory={configCategory}
                configData={configData}
                setConfigData={setConfigData}
                configJson={configJson}
                setConfigJson={setConfigJson}
                configSaving={configSaving}
                configGlobalData={configGlobalData}
                focusedField={focusedField}
                setFocusedField={setFocusedField}
                showAdvancedToolConfig={showAdvancedToolConfig}
                setShowAdvancedToolConfig={setShowAdvancedToolConfig}
                saveConfig={saveConfig}
                setConfigTool={setConfigTool}
                setConfigCategory={setConfigCategory}
                loadTools={loadTools}
            />
            {mcpEditor && (
                <MCPServerEditor
                    serverId={mcpEditor.serverId}
                    mode="agent"
                    defaultTab="override"
                    agentId={agentId}
                    role={effectiveEditorRole(currentUser)}
                    titleSuffix={mcpEditor.toolDisplayName}
                    onClose={() => setMcpEditor(null)}
                    onSaved={() => {
                        loadTools();
                        tmQueryClient.invalidateQueries({ queryKey: ['agent-tools', agentId] });
                    }}
                    draftOverride={draftMcpOverrides?.[mcpEditor.serverId] ?? null}
                    onDraftOverrideChange={onDraftMcpOverridesChange ? (override) => {
                        const next = { ...(draftMcpOverrides || {}) };
                        if (override) next[mcpEditor.serverId] = override;
                        else delete next[mcpEditor.serverId];
                        onDraftMcpOverridesChange(next);
                    } : undefined}
                />
            )}
        </>
    );
}
