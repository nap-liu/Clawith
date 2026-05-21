import React, { useEffect, useMemo, useState, useCallback } from 'react';
import { IconX } from '@tabler/icons-react';
import { mcpServersApi } from '../../services/mcpServers';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorTab, MCPServerEditorProps } from './types';
import { visibleTabs } from './types';
import BasicTab from './BasicTab';
import AdvancedTab from './AdvancedTab';
import OverrideTab from './OverrideTab';
import TestTab from './TestTab';

export default function MCPServerEditor(props: MCPServerEditorProps) {
  const { serverId, mode, defaultTab, agentId, role, titleSuffix, onClose, onSaved } = props;
  const tabs = useMemo(() => visibleTabs(role, mode), [role, mode]);
  const initialTab: EditorTab = useMemo(() => {
    if (defaultTab && tabs.includes(defaultTab)) return defaultTab;
    return tabs[0] ?? 'basic';
  }, [defaultTab, tabs]);
  const [activeTab, setActiveTab] = useState<EditorTab>(initialTab);
  const [server, setServer] = useState<MCPServer | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    try {
      const s = await mcpServersApi.get(serverId);
      setServer(s);
    } catch (e: any) {
      setError(e?.message ?? String(e));
    }
  }, [serverId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const s = await mcpServersApi.get(serverId);
        if (!cancelled) setServer(s);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [serverId]);

  const labelFor = (t: EditorTab) =>
    t === 'basic' ? '基础'
    : t === 'advanced' ? '高级'
    : t === 'override' ? 'Override'
    : '测试';

  const handleSaved = useCallback(() => {
    onSaved?.();
    refetch();
  }, [onSaved, refetch]);

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 2100,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-primary)', borderRadius: 12,
          width: 'min(720px, 92vw)', maxHeight: '90vh',
          display: 'flex', flexDirection: 'column',
          border: '1px solid var(--border-subtle)',
        }}
      >
        <div style={{
          padding: '14px 18px', display: 'flex', alignItems: 'center', gap: 12,
          borderBottom: '1px solid var(--border-subtle)',
        }}>
          <h3 style={{ flex: 1, margin: 0, fontSize: 15, color: 'var(--text-primary)' }}>
            MCP Server: {server?.display_name ?? serverId.slice(0, 8)}
            {titleSuffix && <span style={{ color: 'var(--text-secondary)', fontWeight: 400 }}> · {titleSuffix}</span>}
          </h3>
          <button onClick={onClose} aria-label="close" style={{ background: 'none', border: 'none', cursor: 'pointer' }}>
            <IconX size={18} stroke={1.8} />
          </button>
        </div>

        <div role="tablist" style={{
          display: 'flex', gap: 4, padding: '8px 12px',
          borderBottom: '1px solid var(--border-subtle)',
        }}>
          {tabs.map(t => (
            <button
              key={t}
              role="tab"
              aria-selected={activeTab === t}
              onClick={() => setActiveTab(t)}
              style={{
                padding: '6px 12px', fontSize: 12, borderRadius: 6,
                border: '1px solid var(--border-subtle)',
                background: activeTab === t ? 'var(--bg-secondary)' : 'transparent',
                color: activeTab === t ? 'var(--text-primary)' : 'var(--text-secondary)',
                cursor: 'pointer',
              }}
            >
              {labelFor(t)}
            </button>
          ))}
        </div>

        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 18 }}>
          {loading && <div style={{ color: 'var(--text-secondary)' }}>加载中…</div>}
          {!loading && error && <div style={{ color: '#ef4444' }}>加载失败：{error}</div>}
          {!loading && !error && server && (
            <>
              {activeTab === 'basic' && <BasicTab server={server} role={role} agentId={agentId} onSaved={handleSaved} />}
              {activeTab === 'advanced' && <AdvancedTab server={server} role={role} agentId={agentId} onSaved={handleSaved} />}
              {activeTab === 'override' && agentId && <OverrideTab server={server} agentId={agentId} role={role} onSaved={handleSaved} />}
              {activeTab === 'test' && <TestTab server={server} agentId={agentId} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
