import { IconChevronDown } from '@tabler/icons-react';
import { getLocalizedToolPresentation } from '../../../utils/toolPresentation';
import fetchJson from '../api';

export default function AgentInstalledToolsPanel({ model }: { model: any }) {
    const {
        agentInstalledTools,
        dialog,
        expandedAgentInstalledGroups,
        getToolGroupMeta,
        loadAgentInstalledTools,
        renderCategoryIcon,
        setExpandedAgentInstalledGroups,
        t,
    } = model;

    return (
                            <div>
                                <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>{t('enterprise.tools.agentInstalledHint')}</p>
                                {agentInstalledTools.length === 0 ? (
                                    <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('enterprise.tools.noAgentInstalledTools')}</div>
                                ) : (
                                    (() => {
                                        const grouped = agentInstalledTools.reduce((acc: Record<string, any[]>, row: any) => {
                                            const groupKey = getLocalizedToolPresentation(t, row).groupKey;
                                            (acc[groupKey] = acc[groupKey] || []).push(row);
                                            return acc;
                                        }, {});
                                        const toggleAgentInstalledGroup = (groupKey: string) => {
                                            setExpandedAgentInstalledGroups((prev: Set<string>) => {
                                                const next = new Set(prev);
                                                if (next.has(groupKey)) next.delete(groupKey);
                                                else next.add(groupKey);
                                                return next;
                                            });
                                        };
                                        return (
                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                                {Object.entries(grouped)
                                                    .sort(([a, aRows], [b, bRows]) => {
                                                        const aMeta = getToolGroupMeta(a, aRows as any[]);
                                                        const bMeta = getToolGroupMeta(b, bRows as any[]);
                                                        return aMeta.label.localeCompare(bMeta.label);
                                                    })
                                                    .map(([groupKey, rows]) => {
                                                        const groupRows = rows as any[];
                                                        const meta = getToolGroupMeta(groupKey, groupRows);
                                                        const expanded = expandedAgentInstalledGroups.has(groupKey);
                                                        return (
                                                            <div key={groupKey} style={{ border: '1px solid var(--border-subtle)', borderRadius: '8px', overflow: 'hidden', background: 'var(--bg-primary)' }}>
                                                                <div
                                                                    role="button"
                                                                    tabIndex={0}
                                                                    onClick={() => toggleAgentInstalledGroup(groupKey)}
                                                                    onKeyDown={(e) => {
                                                                        if (e.key === 'Enter' || e.key === ' ') {
                                                                            e.preventDefault();
                                                                            toggleAgentInstalledGroup(groupKey);
                                                                        }
                                                                    }}
                                                                    style={{ background: 'var(--bg-secondary)', padding: '12px 14px', display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer', userSelect: 'none' }}
                                                                >
                                                                    <IconChevronDown size={14} stroke={1.8} style={{ color: 'var(--text-tertiary)', transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)', transition: 'transform 0.15s ease', flexShrink: 0 }} />
                                                                    <span style={{ width: '26px', height: '26px', borderRadius: '7px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{renderCategoryIcon(meta.iconCategory, 15)}</span>
                                                                    <div style={{ minWidth: 0 }}>
                                                                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                                                                            <span style={{ fontSize: '13px', fontWeight: 650, color: 'var(--text-primary)' }}>{meta.label}</span>
                                                                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                                                                {t('agent.tools.toolCount', { count: groupRows.length })}
                                                                            </span>
                                                                        </div>
                                                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{meta.description}</div>
                                                                    </div>
                                                                </div>
                                                                {expanded && groupRows.map((row: any, idx: number) => {
                                                                    const presentation = getLocalizedToolPresentation(t, row);
                                                                    return (
                                                                    <div key={row.agent_tool_id} style={{
                                                                        display: 'grid',
                                                                        gridTemplateColumns: 'minmax(0, 1fr) auto',
                                                                        gap: '12px',
                                                                        alignItems: 'center',
                                                                        padding: '10px 14px',
                                                                        borderTop: idx === 0 ? '1px solid var(--border-subtle)' : 'none',
                                                                        borderBottom: idx < groupRows.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                                                                    }}>
                                                                        <div style={{ minWidth: 0 }}>
                                                                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0, flexWrap: 'wrap' }}>
                                                                                <span style={{ fontWeight: 500, fontSize: '13px', color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{presentation.name}</span>
                                                                                {row.type === 'mcp' && <span style={{ fontSize: '10px', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', borderRadius: '4px', padding: '1px 5px' }}>MCP</span>}
                                                                                {row.configured && <span style={{ fontSize: '10px', background: 'rgba(99,102,241,0.15)', color: 'var(--accent-color)', borderRadius: '4px', padding: '1px 5px' }}>{t('enterprise.tools.configured', 'Configured')}</span>}
                                                                            </div>
                                                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                                                {row.installed_by_agent_name || t('enterprise.tools.unknownDigitalEmployee')}
                                                                                {row.installed_at && <span> · {new Date(row.installed_at).toLocaleString()}</span>}
                                                                            </div>
                                                                        </div>
                                                                        <button className="btn btn-ghost" style={{ color: 'var(--error)', fontSize: '12px' }} onClick={async () => {
                                                                            const ok = await dialog.confirm(t('enterprise.tools.removeFromAgent', { name: presentation.name }), { title: t('agent.tools.removeTool'), danger: true, confirmLabel: t('common.confirmActions.removeLabel') });
                                                                            if (!ok) return;
                                                                            try {
                                                                                await fetchJson(`/tools/agent-tool/${row.agent_tool_id}`, { method: 'DELETE' });
                                                                            } catch {
                                                                                // Already deleted (e.g. removed via Global Tools) — just refresh
                                                                            }
                                                                            loadAgentInstalledTools();
                                                                        }}>{t('enterprise.tools.delete')}</button>
                                                                    </div>
                                                                    );
                                                                })}
                                                            </div>
                                                        );
                                                    })}
                                            </div>
                                        );
                                    })()
                                )}
                            </div>
    );
}
