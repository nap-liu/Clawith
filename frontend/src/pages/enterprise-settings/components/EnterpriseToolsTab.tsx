import { CliToolsSection } from '../../../components/cli-tools/CliToolsSection';
import LlmModelSelect from '../../../components/LlmModelSelect';
import MCPServerEditor from '../../../components/MCPServerEditor';
import { effectiveEditorRole } from '../../../components/MCPServerEditor/role';
import { getLocalizedToolPresentation } from '../../../utils/toolPresentation';
import {
    IconChevronDown,
    IconSearch,
    IconSettings,
} from '@tabler/icons-react';
import fetchJson from '../api';
import AgentInstalledToolsPanel from './AgentInstalledToolsPanel';
import MCPImportPanel from './MCPImportPanel';
import MCPToolDisplayNameEditor from './MCPToolDisplayNameEditor';

export default function EnterpriseToolsTab({ model }: { model: any }) {
    const {
        GLOBAL_CATEGORY_CONFIG_PRIMARY_TOOL,
        GLOBAL_CATEGORY_CONFIG_SCHEMAS,
        agentInstalledTools,
        allTools,
        applyConfigDefaults,
        configCategory,
        currentUser,
        dialog,
        editingConfig,
        editingConfigInitial,
        editingMcpServer,
        editingToolId,
        expandedToolCategories,
        getToolGroupMeta,
        hasMeaningfulConfig,
        loadAgentInstalledTools,
        loadAllTools,
        selectedTenantId,
        renderCategoryIcon,
        setConfigCategory,
        setEditingConfig,
        setEditingConfigInitial,
        setEditingMcpServer,
        setEditingToolId,
        setExpandedToolCategories,
        setShowAddMCP,
        setShowAdvancedToolConfig,
        setToolSearch,
        setToolStatusFilter,
        setToolsView,
        showAddMCP,
        showAdvancedToolConfig,
        switchKnob,
        switchTrack,
        t,
        toast,
        toolSearch,
        toolStatusFilter,
        toolsView,
    } = model;

    return (
                    <div>
                        {/* Sub-tab pills */}
                        <div className="tool-source-tabs enterprise-tool-source-tabs" role="tablist" aria-label={t('enterprise.tools.sourceTabs', 'Tool sources')}>
                            {([['global', t('enterprise.tools.globalTools')], ['agent-installed', t('enterprise.tools.agentInstalled')]] as const).map(([key, label]) => (
                                <button
                                    key={key}
                                    type="button"
                                    role="tab"
                                    aria-selected={toolsView === key}
                                    className={toolsView === key ? 'active' : ''}
                                    onClick={() => { setToolsView(key as any); if (key === 'agent-installed') loadAgentInstalledTools(); }}
                                >
                                    <span>{label}</span>
                                    <span className="tool-source-tab-count">{key === 'global' ? allTools.length : agentInstalledTools.length}</span>
                                </button>
                            ))}
                        </div>

                        {toolsView === 'agent-installed' && <AgentInstalledToolsPanel model={model} />}

                        {toolsView === 'global' && <>
                            {/* Fork's CLI tools admin (binary uploads + per-tool wizard).
                                Self-contained section, sits above MCP servers. */}
                            <CliToolsSection />

                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                                <div />
                                <button className="btn btn-primary" onClick={() => setShowAddMCP(true)}>+ {t('enterprise.tools.addMcpServer')}</button>
                            </div>

                            {showAddMCP && (
                                <MCPImportPanel
                                    selectedTenantId={selectedTenantId}
                                    dialog={dialog}
                                    toast={toast}
                                    onCancel={() => setShowAddMCP(false)}
                                    onImported={loadAllTools}
                                />
                            )}

                            {/* ─── Category-grouped tool list ─── */}
                            {(() => {
                                const normalizedSearch = toolSearch.trim().toLowerCase();
                                const matchesSearch = (tool: any) => {
                                    if (!normalizedSearch) return true;
                                    return getLocalizedToolPresentation(t, tool).searchText.includes(normalizedSearch);
                                };
                                const matchesStatus = (tool: any) => {
                                    if (toolStatusFilter === 'enabled') return !!tool.enabled;
                                    if (toolStatusFilter === 'disabled') return !tool.enabled;
                                    if (toolStatusFilter === 'default') return !!tool.is_default;
                                    if (toolStatusFilter === 'configured') return hasMeaningfulConfig(tool.config);
                                    return true;
                                };
                                const filteredTools = allTools.filter((tool: any) => matchesSearch(tool) && matchesStatus(tool));
                                const groupTools = (toolList: any[]) => toolList.reduce((acc: Record<string, any[]>, tool: any) => {
                                    const cat = getLocalizedToolPresentation(t, tool).groupKey;
                                    (acc[cat] = acc[cat] || []).push(tool);
                                    return acc;
                                }, {} as Record<string, any[]>);
                                const grouped = groupTools(filteredTools);
                                const allGrouped = groupTools(allTools);
                                const hasFilters = !!normalizedSearch || toolStatusFilter !== 'all';

                                const toggleCategoryExpanded = (category: string) => {
                                    setExpandedToolCategories((prev: Set<string>) => {
                                        const next = new Set(prev);
                                        if (next.has(category)) next.delete(category);
                                        else next.add(category);
                                        return next;
                                    });
                                };

                                const bulkToggle = async (tools: any[], enabled: boolean) => {
                                    const controllableTools = tools.filter(tool => tool.can_disable !== false);
                                    if (controllableTools.length === 0) return;
                                    try {
                                        const payload = controllableTools.map(t => ({ tool_id: t.id, enabled }));
                                        await fetchJson('/tools/bulk', { method: 'PUT', body: JSON.stringify(payload) });
                                        loadAllTools();
                                    } catch (err: any) {
                                        toast.error(t('common.error.batchUpdateFailed'), { details: String(err?.message || err) });
                                    }
                                };

                                const renderToolRow = (tool: any, category: string, idx: number, total: number) => {
                                    const presentation = getLocalizedToolPresentation(t, tool);
                                    const hasCategoryConfig = !!GLOBAL_CATEGORY_CONFIG_SCHEMAS[category];
                                    // Count only company-configurable fields — a tool whose fields are all
                                    // agent_only (e.g. request_confirmation's card template) has nothing to
                                    // configure globally, so it shows no config entry here.
                                    const globalFieldCount = (tool.config_schema?.fields || []).filter((f: any) => !f.agent_only).length;
                                    const hasOwnConfig = globalFieldCount > 0 && !hasCategoryConfig;
                                    const isConfigured = hasMeaningfulConfig(tool.config);
                                    return (
                                        <div key={tool.id} style={{
                                            display: 'grid',
                                            gridTemplateColumns: 'minmax(0, 1fr) auto',
                                            alignItems: 'center',
                                            gap: '12px',
                                            padding: '10px 14px',
                                            borderTop: idx === 0 ? '1px solid var(--border-subtle)' : 'none',
                                            borderBottom: idx < total - 1 ? '1px solid var(--border-subtle)' : 'none',
                                            background: 'var(--bg-primary)',
                                        }}>
                                            <div style={{ minWidth: 0 }}>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0, flexWrap: 'wrap' }}>
                                                    <span style={{ fontWeight: 500, fontSize: '13px', color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{presentation.name}</span>
                                                    <span style={{ fontSize: '10px', background: tool.type === 'mcp' ? 'var(--primary)' : 'var(--bg-tertiary)', color: tool.type === 'mcp' ? '#fff' : 'var(--text-secondary)', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>
                                                        {tool.type === 'mcp' ? 'MCP' : t('enterprise.tools.builtIn')}
                                                    </span>
                                                    {tool.is_default && <span style={{ fontSize: '10px', background: 'rgba(0,200,100,0.15)', color: 'var(--success)', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>{t('enterprise.tools.default')}</span>}
                                                    {isConfigured && <span style={{ fontSize: '10px', background: 'rgba(99,102,241,0.15)', color: 'var(--accent-color)', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>{t('enterprise.tools.configured', 'Configured')}</span>}
                                                </div>
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {presentation.description}
                                                    {(tool.mcp_server_display_name || tool.mcp_server_name) && (
                                                        <span> · {tool.mcp_server_display_name || tool.mcp_server_name}</span>
                                                    )}
                                                </div>
                                            </div>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                                                {tool.type === 'mcp' && (
                                                    <MCPToolDisplayNameEditor
                                                        toolId={tool.id}
                                                        displayName={tool.display_name || tool.name}
                                                        onSaved={loadAllTools}
                                                        toast={toast}
                                                    />
                                                )}
                                                {hasOwnConfig && (
                                                    <button
                                                        style={{ background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }}
                                                        title={t('enterprise.tools.configureSettings', 'Configure settings')}
                                                        onClick={async () => {
                                                            setEditingToolId(tool.id);
                                                            setShowAdvancedToolConfig(false);
                                                            let cfg = applyConfigDefaults(tool.config_schema?.fields || [], tool.config || {});
                                                            if (tool.name === 'jina_search' || tool.name === 'jina_read') {
                                                                try {
                                                                    const token = localStorage.getItem('token');
                                                                    const res = await fetch('/api/enterprise/system-settings/jina_api_key', { headers: { Authorization: `Bearer ${token}` } });
                                                                    const d = await res.json();
                                                                    if (d.value?.api_key) cfg.api_key = d.value.api_key;
                                                                } catch { }
                                                            }
                                                            setEditingConfig(cfg); setEditingConfigInitial(JSON.stringify(cfg));
                                                        }}
                                                    >
                                                        {t('enterprise.tools.configure')}
                                                    </button>
                                                )}
                                                {tool.type !== 'builtin' && (
                                                    <button className="btn btn-danger" style={{ padding: '4px 8px', fontSize: '11px' }} onClick={async () => {
                                                        const ok = await dialog.confirm(t('common.dialog.deleteToolConfirm', { name: presentation.name }), { title: t('common.dialog.deleteTool'), danger: true, confirmLabel: t('common.confirmActions.deleteLabel') });
                                                        if (!ok) return;
                                                        await fetchJson(`/tools/${tool.id}`, { method: 'DELETE' });
                                                        loadAllTools();
                                                        loadAgentInstalledTools();
                                                    }}>{t('common.delete')}</button>
                                                )}
                                                {tool.can_disable === false ? (
                                                    <span style={{ fontSize: '11px', color: 'var(--accent-color)', fontWeight: 600, whiteSpace: 'nowrap' }}>
                                                        {t('agent.tools.alwaysAvailable', 'Always available')}
                                                    </span>
                                                ) : <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer', flexShrink: 0 }}>
                                                    <input type="checkbox" checked={tool.enabled} onChange={async (e) => {
                                                        await fetchJson(`/tools/${tool.id}`, { method: 'PUT', body: JSON.stringify({ enabled: e.target.checked }) });
                                                        loadAllTools();
                                                    }} style={{ opacity: 0, width: 0, height: 0 }} />
                                                    <span style={switchTrack(tool.enabled)}>
                                                        <span style={switchKnob(tool.enabled)} />
                                                    </span>
                                                </label>}
                                            </div>
                                        </div>
                                    );
                                };

                                if (allTools.length === 0) {
                                    return <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('enterprise.tools.emptyState')}</div>;
                                }
                                if (filteredTools.length === 0) {
                                    return (
                                        <>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' }}>
                                                <div style={{ position: 'relative', flex: '1 1 260px', minWidth: '220px' }}>
                                                    <IconSearch size={15} style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-tertiary)' }} />
                                                    <input value={toolSearch} onChange={(e) => setToolSearch(e.target.value)} placeholder={t('agent.tools.searchTools', 'Search tools...')} style={{ width: '100%', boxSizing: 'border-box', border: '1px solid var(--border-subtle)', borderRadius: '8px', background: 'var(--bg-primary)', color: 'var(--text-primary)', padding: '8px 10px 8px 32px', fontSize: '13px', outline: 'none' }} />
                                                </div>
                                            </div>
                                            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{hasFilters ? t('agent.tools.noMatchingTools', 'No matching tools') : t('enterprise.tools.emptyState')}</div>
                                        </>
                                    );
                                }

                                return (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                                            <div style={{ position: 'relative', flex: '1 1 260px', minWidth: '220px' }}>
                                                <IconSearch size={15} style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: 'var(--text-tertiary)' }} />
                                                <input value={toolSearch} onChange={(e) => setToolSearch(e.target.value)} placeholder={t('agent.tools.searchTools', 'Search tools...')} style={{ width: '100%', boxSizing: 'border-box', border: '1px solid var(--border-subtle)', borderRadius: '8px', background: 'var(--bg-primary)', color: 'var(--text-primary)', padding: '8px 10px 8px 32px', fontSize: '13px', outline: 'none' }} />
                                            </div>
                                            {(['all', 'enabled', 'disabled', 'default', 'configured'] as const).map(filter => (
                                                <button key={filter} type="button" onClick={() => setToolStatusFilter(filter)} style={{ border: '1px solid var(--border-subtle)', borderRadius: '999px', background: toolStatusFilter === filter ? 'var(--text-primary)' : 'var(--bg-primary)', color: toolStatusFilter === filter ? 'var(--bg-primary)' : 'var(--text-secondary)', padding: '6px 10px', fontSize: '11px', cursor: 'pointer' }}>
                                                    {filter === 'all' ? t('common.all', 'All')
                                                        : filter === 'enabled' ? t('common.enabled', 'Enabled')
                                                            : filter === 'disabled' ? t('common.disabled', 'Disabled')
                                                                : filter === 'default' ? t('enterprise.tools.default')
                                                                    : t('agent.tools.configured', 'Configured')}
                                                </button>
                                            ))}
                                            <button type="button" onClick={() => {
                                                const categories = Object.keys(allGrouped);
                                        setExpandedToolCategories((prev: Set<string>) => prev.size >= categories.length ? new Set() : new Set(categories));
                                            }} style={{ border: '1px solid var(--border-subtle)', borderRadius: '8px', background: 'var(--bg-primary)', color: 'var(--text-secondary)', padding: '6px 10px', fontSize: '11px', cursor: 'pointer' }}>
                                                {expandedToolCategories.size >= Object.keys(allGrouped).length ? t('agent.tools.collapseAll', 'Collapse all') : t('agent.tools.expandAll', 'Expand all')}
                                            </button>
                                        </div>

                                        {Object.entries(grouped)
                                            .sort(([a, aTools], [b, bTools]) => {
                                                const aMeta = getToolGroupMeta(a, allGrouped[a] || aTools as any[]);
                                                const bMeta = getToolGroupMeta(b, allGrouped[b] || bTools as any[]);
                                                return aMeta.label.localeCompare(bMeta.label);
                                            })
                                            .map(([category, catTools]) => {
                                            const allCatTools = allGrouped[category] || catTools;
                                            const meta = getToolGroupMeta(category, allCatTools);
                                            const hasCategoryConfig = !!GLOBAL_CATEGORY_CONFIG_SCHEMAS[meta.configCategory];
                                            const label = meta.label;
                                            const enabledCount = allCatTools.filter((tool: any) => tool.enabled).length;
                                            const controllableTools = allCatTools.filter((tool: any) => tool.can_disable !== false);
                                            const controllableEnabledCount = controllableTools.filter((tool: any) => tool.enabled).length;
                                            const defaultCount = allCatTools.filter((tool: any) => tool.is_default).length;
                                            const configuredCount = allCatTools.filter((tool: any) => hasMeaningfulConfig(tool.config)).length;
                                            const allEnabled = controllableTools.length > 0 && controllableEnabledCount === controllableTools.length;
                                            const mixed = controllableEnabledCount > 0 && controllableEnabledCount < controllableTools.length;
                                            const expanded = expandedToolCategories.has(category) || !!toolSearch.trim();
                                            const visibleCount = (catTools as any[]).length;
                                            const mcpServerTool = allCatTools.find((tool: any) => tool.type === 'mcp' && tool.mcp_server_id);

                                            return (
                                                <div key={category} style={{ border: '1px solid var(--border-subtle)', borderRadius: '8px', overflow: 'hidden', background: 'var(--bg-primary)' }}>
                                                    <div role="button" tabIndex={0} onClick={() => toggleCategoryExpanded(category)} onKeyDown={(e) => {
                                                        if (e.key === 'Enter' || e.key === ' ') {
                                                            e.preventDefault();
                                                            toggleCategoryExpanded(category);
                                                        }
                                                    }} style={{ width: '100%', background: 'var(--bg-secondary)', padding: '13px 16px', display: 'grid', gridTemplateColumns: '1fr auto', gap: '14px', alignItems: 'center', cursor: 'pointer', textAlign: 'left', boxSizing: 'border-box' }}>
                                                        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}>
                                                            <IconChevronDown size={16} style={{ transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)', transition: 'transform 120ms ease', color: 'var(--text-tertiary)', flexShrink: 0 }} />
                                                            <span style={{ width: '28px', height: '28px', borderRadius: '7px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{renderCategoryIcon(meta.iconCategory, 16)}</span>
                                                            <div style={{ minWidth: 0 }}>
                                                                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                                                                    <span style={{ fontSize: '13px', fontWeight: 650, color: 'var(--text-primary)' }}>{label}</span>
                                                                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                                        {t('agent.tools.groupSummary', { total: allCatTools.length, enabled: enabledCount })}
                                                                        {defaultCount > 0 ? ` · ${t('enterprise.tools.defaultCount', { count: defaultCount })}` : ''}
                                                                        {visibleCount !== allCatTools.length ? ` · ${t('agent.tools.groupShown', { count: visibleCount })}` : ''}
                                                                    </span>
                                                                    {configuredCount > 0 && <span style={{ fontSize: '10px', background: 'rgba(99,102,241,0.15)', color: 'var(--accent-color)', borderRadius: '4px', padding: '1px 5px' }}>{t('agent.tools.groupConfigured', { count: configuredCount })}</span>}
                                                                </div>
                                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{meta.description}</div>
                                                            </div>
                                                        </div>
                                                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }} onClick={(e) => e.stopPropagation()}>
                                                            {mcpServerTool && (
                                                                <button className="btn btn-secondary" style={{ padding: '4px 8px', fontSize: '11px' }} onClick={() => setEditingMcpServer({
                                                                    server_id: mcpServerTool.mcp_server_id,
                                                                    server_name: mcpServerTool.mcp_server_display_name || mcpServerTool.mcp_server_name,
                                                                })}>
                                                                    {t('enterprise.tools.editMcpGroup')}
                                                                </button>
                                                            )}
                                                            {hasCategoryConfig && (
                                                                <button onClick={() => {
                                                                    setConfigCategory(meta.configCategory);
                                                                    const firstToolWithConfig = (allCatTools as any[]).find((tl: any) => tl.category === meta.configCategory && hasMeaningfulConfig(tl.config));
                                                                    const initialConfig = firstToolWithConfig?.config ? { ...firstToolWithConfig.config } : {};
                                                                    setEditingConfig(initialConfig); setEditingConfigInitial(JSON.stringify(initialConfig));
                                                                }} style={{ background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '4px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }} title={t('agent.tools.configureCategory', { category: label })}>
                                                                    {t('enterprise.tools.configure', 'Configure')}
                                                                </button>
                                                            )}
                                                            {controllableTools.length > 0 && <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer', flexShrink: 0 }} title={t('agent.tools.enableDisableAll', { category: label })}>
                                                                <input type="checkbox" checked={allEnabled} onChange={(e) => void bulkToggle(allCatTools, e.target.checked)} style={{ opacity: 0, width: 0, height: 0 }} />
                                                                <span style={switchTrack(allEnabled, mixed)}>
                                                                    <span style={switchKnob(allEnabled)} />
                                                                </span>
                                                            </label>}
                                                        </div>
                                                    </div>
                                                    {expanded && (
                                                        <div>
                                                            {(catTools as any[]).map((tool: any, idx: number) => renderToolRow(tool, category, idx, (catTools as any[]).length))}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                );
                            })()}

                            {/* ─── Edit MCP Server Modal — unified <MCPServerEditor> ─── */}
                            {editingMcpServer && (
                                <MCPServerEditor
                                    serverId={editingMcpServer.server_id}
                                    mode="server-admin"
                                    defaultTab="basic"
                                    role={effectiveEditorRole(currentUser)}
                                    titleSuffix={editingMcpServer.server_name}
                                    onClose={() => { setEditingMcpServer(null); }}
                                    onSaved={() => loadAllTools()}
                                />
                            )}

                            {/* Per-Tool Config Modal */}
                            {editingToolId && (() => {
                                const tool = allTools.find((t: any) => t.id === editingToolId);
                                if (!tool) return null;
                                const presentation = getLocalizedToolPresentation(t, tool);
                                const visibleFields = (tool.config_schema.fields || []).filter((field: any) => {
                                    // agent_only fields are configured per-agent (e.g. a DingTalk card
                                    // template bound to each agent's own app) — never at company level.
                                    if (field.agent_only) return false;
                                    if (field.depends_on) {
                                        return Object.entries(field.depends_on).every(([k, vals]: [string, any]) =>
                                            vals.includes(editingConfig[k])
                                        );
                                    }
                                    return true;
                                });
                                const primaryFields = visibleFields.filter((field: any) => !field.advanced);
                                const advancedFields = visibleFields.filter((field: any) => field.advanced);
                                const configDirty = JSON.stringify(editingConfig) !== editingConfigInitial;
                                const missingRequiredModel = visibleFields.some((field: any) => field.type === 'llm_model_picker' && field.required && !(editingConfig[field.key] ?? field.default));
                                const renderField = (field: any) => (
                                    <div key={field.key}>
                                        <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>{field.label}</label>
                                        {field.type === 'checkbox' ? (
                                            <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer' }}>
                                                <input
                                                    type="checkbox"
                                                    checked={editingConfig[field.key] ?? field.default ?? false}
                                                    onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.checked }))}
                                                    style={{ opacity: 0, width: 0, height: 0 }}
                                                />
                                                <span style={{
                                                    position: 'absolute', inset: 0,
                                                    background: (editingConfig[field.key] ?? field.default) ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                                                    borderRadius: '11px', transition: 'background 0.2s',
                                                }}>
                                                    <span style={{
                                                        position: 'absolute', left: (editingConfig[field.key] ?? field.default) ? '20px' : '2px', top: '2px',
                                                        width: '18px', height: '18px', background: '#fff',
                                                        borderRadius: '50%', transition: 'left 0.2s',
                                                    }} />
                                                </span>
                                            </label>
                                        ) : field.type === 'llm_model_picker' ? (
                                            <LlmModelSelect
                                                value={editingConfig[field.key] ?? field.default ?? ''}
                                                onChange={value => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: value }))}
                                                tenantId={selectedTenantId}
                                                supportsVision={field.filter?.supports_vision === true}
                                                required={field.required}
                                            />
                                        ) : field.type === 'select' ? (
                                            <select className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))}>
                                                {(field.options || []).map((opt: any) => (
                                                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                                                ))}
                                            </select>
                                        ) : field.type === 'number' ? (
                                            <input type="number" className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} min={field.min} max={field.max}
                                                onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: Number(e.target.value) }))} />
                                        ) : field.type === 'textarea' ? (
                                            <textarea
                                                className="form-input"
                                                value={editingConfig[field.key] ?? field.default ?? ''}
                                                placeholder={field.placeholder || ''}
                                                rows={Math.max(3, Math.min(10, String(editingConfig[field.key] ?? field.default ?? field.placeholder ?? '').split('\n').length))}
                                                style={{ minHeight: '88px', fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)', resize: 'vertical' }}
                                                onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))}
                                            />
                                        ) : field.type === 'password' ? (
                                            <input type="password" autoComplete="new-password" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''}
                                                onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))} />
                                        ) : (
                                            <input type="text" className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} placeholder={field.placeholder || ''}
                                                onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))} />
                                        )}
                                    </div>
                                );
                                return (
                                    <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                                        onClick={() => setEditingToolId(null)}>
                                        <div onClick={e => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}>
                                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                                                <div>
                                                    <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}><IconSettings size={20} stroke={1.8} /> {presentation.name}</h3>
                                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{t('enterprise.tools.globalConfigDescription')}</div>
                                                </div>
                                                <button onClick={() => setEditingToolId(null)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}>✕</button>
                                            </div>
                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                                                {primaryFields.map(renderField)}
                                                {advancedFields.length > 0 && (
                                                    <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '10px', marginTop: '2px' }}>
                                                        <button
                                                            type="button"
                                                            className="btn btn-ghost"
                                                            onClick={() => setShowAdvancedToolConfig((v: boolean) => !v)}
                                                            style={{ padding: 0, minWidth: 'auto', fontSize: '12px', color: 'var(--text-secondary)' }}
                                                        >
                                                            {showAdvancedToolConfig ? t('enterprise.tools.hideAdvancedSettings') : t('enterprise.tools.advancedSettings')}
                                                        </button>
                                                        {showAdvancedToolConfig && (
                                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '12px' }}>
                                                                {advancedFields.map(renderField)}
                                                            </div>
                                                        )}
                                                    </div>
                                                )}
                                                <div style={{ display: 'flex', gap: '8px', marginTop: '12px', justifyContent: 'flex-end', borderTop: '1px solid var(--border-subtle)', paddingTop: '16px' }}>
                                                    <button className="btn btn-secondary" onClick={() => setEditingToolId(null)}>{t('common.cancel')}</button>
                                                    <button className="btn btn-primary" disabled={!configDirty || missingRequiredModel} onClick={async () => {
                                                        if (tool.name === 'jina_search' || tool.name === 'jina_read') {
                                                            if (editingConfig.api_key) {
                                                                const token = localStorage.getItem('token');
                                                                await fetch('/api/enterprise/system-settings/jina_api_key', {
                                                                    method: 'PUT',
                                                                    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
                                                                    body: JSON.stringify({ value: { api_key: editingConfig.api_key } }),
                                                                });
                                                            }
                                                        } else {
                                                            await fetchJson(`/tools/${tool.id}`, { method: 'PUT', body: JSON.stringify({ config: editingConfig, tenant_id: selectedTenantId || undefined }) });
                                                        }
                                                        setEditingToolId(null);
                                                        loadAllTools();
                                                    }}>{t('enterprise.tools.saveConfig')}</button>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })()}

                            {/* Category-level config modal */}
                            {configCategory && GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory] && (
                                <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                                    onClick={() => setConfigCategory(null)}>
                                    <div onClick={e => e.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                                            <div>
                                                <h3 style={{ margin: 0 }}>{GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory].title}</h3>
                                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{t('enterprise.tools.globalCategoryConfigDescription')}</div>
                                            </div>
                                            <button onClick={() => setConfigCategory(null)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}>x</button>
                                        </div>
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                                            {GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory].fields.map((field: any) => (
                                                <div key={field.key}>
                                                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>{field.label}</label>
                                                    {field.type === 'password' ? (
                                                        <input type="password" autoComplete="new-password" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''}
                                                            onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))} />
                                                    ) : field.type === 'select' ? (
                                                        <select className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))}>
                                                            {(field.options || []).map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                                        </select>
                                                    ) : (
                                                        <input type="text" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''}
                                                            onChange={e => setEditingConfig((p: Record<string, any>) => ({ ...p, [field.key]: e.target.value }))} />
                                                    )}
                                                </div>
                                            ))}
                                            <div style={{ display: 'flex', gap: '8px', marginTop: '8px', justifyContent: 'flex-end' }}>
                                                <button className="btn btn-secondary" onClick={() => setConfigCategory(null)}>{t('common.cancel')}</button>
                                                <button className="btn btn-primary" disabled={JSON.stringify(editingConfig) === editingConfigInitial} onClick={async () => {
                                                    // Save config to the category's runtime representative tool.
                                                    const catTools = allTools.filter((tl: any) => (tl.category || 'general') === configCategory);
                                                    const primaryToolName = GLOBAL_CATEGORY_CONFIG_PRIMARY_TOOL[configCategory];
                                                    const representativeTool = catTools.find((tl: any) => tl.name === primaryToolName) || catTools[0];
                                                    if (representativeTool) {
                                                        await fetchJson(`/tools/${representativeTool.id}`, { method: 'PUT', body: JSON.stringify({ config: editingConfig, tenant_id: selectedTenantId || undefined }) });
                                                    }
                                                    setConfigCategory(null);
                                                    loadAllTools();
                                                }}>{t('common.save', 'Save')}</button>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            )}
                        </>}
                    </div>
    );
}
