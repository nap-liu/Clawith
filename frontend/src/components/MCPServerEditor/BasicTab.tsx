import React from 'react';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorRole } from './types';

interface Props { server: MCPServer; role: EditorRole; agentId?: string; onSaved: () => void }
export default function BasicTab(_p: Props) { return <div style={{ color: 'var(--text-secondary)' }}>Basic — wired in Task 5</div>; }
