import React from 'react';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorRole } from './types';

interface Props { server: MCPServer; agentId: string; role: EditorRole; onSaved: () => void }
export default function OverrideTab(_p: Props) { return <div style={{ color: 'var(--text-secondary)' }}>Override — wired in Task 7</div>; }
