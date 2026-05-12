import React from 'react';
import { OverrideMatrix } from '../mcp-servers/OverrideMatrix';
import { DryRunPanel } from '../mcp-servers/DryRunPanel';
import type { MCPServer } from '../../types/mcpServer';

interface Props {
  server: MCPServer;
}

export default function TenantOverridesTab({ server }: Props) {
  return (
    <div>
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 10 }}>
          租户级 Overrides
        </div>
        <OverrideMatrix
          serverId={server.id}
          lockedScope={{ scope_type: 'tenant', tenant_only: true }}
        />
      </div>
      <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: 14 }}>
        <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 10 }}>
          Dry-Run 预览
        </div>
        <DryRunPanel serverId={server.id} />
      </div>
    </div>
  );
}
