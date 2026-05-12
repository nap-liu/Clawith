import React from 'react';
import type { MCPServer } from '../../types/mcpServer';

interface Props { server: MCPServer; agentId?: string }
export default function TestTab(_p: Props) { return <div style={{ color: 'var(--text-secondary)' }}>Test — wired in Task 8</div>; }
