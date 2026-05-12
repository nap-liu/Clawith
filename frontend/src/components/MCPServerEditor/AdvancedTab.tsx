import React from 'react';
import type { MCPServer } from '../../types/mcpServer';
import type { EditorRole } from './types';

interface Props { server: MCPServer; role: EditorRole; agentId?: string; onSaved: () => void }
export default function AdvancedTab(_p: Props) { return <div style={{ color: 'var(--text-secondary)' }}>Advanced — wired in Task 6</div>; }
