import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { mcpOverridesApi } from '../../services/mcpServers';
import type { MCPServerOverride, MCPServerOverridePutPayload } from '../../types/mcpServer';

interface Props {
  serverId: string;
  /** When set, locks the matrix to a single agent scope (used from AgentDetail). */
  lockedScope?: { scope_type: 'agent'; scope_id: string };
}

export function OverrideMatrix({ serverId, lockedScope }: Props) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<{
    scope_type: 'tenant' | 'agent';
    scope_id: string;
    system_prompt_block: string;
  } | null>(null);

  const { data: overrides, isLoading } = useQuery({
    queryKey: ['mcp-overrides', serverId, lockedScope?.scope_id],
    queryFn: () => mcpOverridesApi.list(serverId, lockedScope?.scope_id),
  });

  const upsertMut = useMutation({
    mutationFn: (input: { scope_type: 'tenant' | 'agent'; scope_id: string; payload: MCPServerOverridePutPayload }) =>
      input.scope_type === 'tenant'
        ? mcpOverridesApi.putTenant(serverId, input.scope_id, input.payload)
        : mcpOverridesApi.putAgent(serverId, input.scope_id, input.payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-overrides', serverId] });
      setEditing(null);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (input: { scope_type: 'tenant' | 'agent'; scope_id: string }) =>
      input.scope_type === 'tenant'
        ? mcpOverridesApi.deleteTenant(serverId, input.scope_id)
        : mcpOverridesApi.deleteAgent(serverId, input.scope_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-overrides', serverId] });
    },
  });

  if (isLoading) return <div className="p-4 text-gray-500">加载中…</div>;

  const visibleTenant = lockedScope ? [] : overrides?.tenant || [];
  const visibleAgent = lockedScope
    ? (overrides?.agent || []).filter(o => o.scope_id === lockedScope.scope_id)
    : overrides?.agent || [];

  return (
    <div className="p-4 space-y-6">
      {!lockedScope && (
        <Section title="租户级 Override" rows={visibleTenant} scope_type="tenant"
          onEdit={(o) => setEditing({ scope_type: 'tenant', scope_id: o.scope_id,
                                      system_prompt_block: o.system_prompt_block || '' })}
          onDelete={(o) => deleteMut.mutate({ scope_type: 'tenant', scope_id: o.scope_id })}
        />
      )}
      <Section title={lockedScope ? '当前 Agent 的 Override' : 'Agent 级 Override'}
        rows={visibleAgent} scope_type="agent"
        onEdit={(o) => setEditing({ scope_type: 'agent', scope_id: o.scope_id,
                                    system_prompt_block: o.system_prompt_block || '' })}
        onDelete={(o) => deleteMut.mutate({ scope_type: 'agent', scope_id: o.scope_id })}
        addNew={lockedScope ? (() => setEditing({
          scope_type: 'agent', scope_id: lockedScope.scope_id, system_prompt_block: '',
        })) : undefined}
        emptyHint={lockedScope ? '该 agent 暂无 override，下方点击添加。' : '暂无 override'}
      />

      {editing && (
        <EditOverrideDialog
          scope_type={editing.scope_type}
          scope_id={editing.scope_id}
          initial={editing.system_prompt_block}
          onCancel={() => setEditing(null)}
          onSubmit={(v) => upsertMut.mutate({
            scope_type: editing.scope_type, scope_id: editing.scope_id,
            payload: { system_prompt_block: v },
          })}
          saving={upsertMut.isPending}
        />
      )}
    </div>
  );
}

function Section(props: {
  title: string;
  rows: MCPServerOverride[];
  scope_type: 'tenant' | 'agent';
  onEdit: (o: MCPServerOverride) => void;
  onDelete: (o: MCPServerOverride) => void;
  addNew?: () => void;
  emptyHint?: string;
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h4 className="font-medium">{props.title}</h4>
        {props.addNew && (
          <button onClick={props.addNew} className="text-sm text-blue-600 hover:underline">
            + 添加
          </button>
        )}
      </div>
      {props.rows.length === 0 ? (
        <div className="text-sm text-gray-500 italic">{props.emptyHint || '暂无'}</div>
      ) : (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b text-gray-600">
              <th className="text-left p-1 font-mono text-xs">scope_id</th>
              <th className="text-left p-1">Prompt 片段</th>
              <th className="p-1 w-20"></th>
            </tr>
          </thead>
          <tbody>
            {props.rows.map((o) => (
              <tr key={o.id} className="border-b hover:bg-gray-50">
                <td className="p-1 font-mono text-xs">{o.scope_id.slice(0, 8)}…</td>
                <td className="p-1 text-xs text-gray-700 truncate max-w-md">
                  {(o.system_prompt_block || '').slice(0, 80) || <span className="italic text-gray-400">(空)</span>}
                </td>
                <td className="p-1 text-right">
                  <button onClick={() => props.onEdit(o)} className="text-blue-600 mr-2">编辑</button>
                  <button onClick={() => props.onDelete(o)} className="text-red-600">删</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function EditOverrideDialog(props: {
  scope_type: 'tenant' | 'agent';
  scope_id: string;
  initial: string;
  onCancel: () => void;
  onSubmit: (v: string) => void;
  saving: boolean;
}) {
  const [value, setValue] = useState(props.initial);
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg p-6 w-[560px] max-w-full">
        <h3 className="text-lg font-semibold mb-2">
          编辑 {props.scope_type} Override
        </h3>
        <div className="text-xs text-gray-500 mb-2 font-mono">{props.scope_id}</div>
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={10}
          className="w-full border rounded px-2 py-1 font-mono text-xs"
          placeholder="该 scope 追加的 prompt 片段（可留空 = 不追加 prompt）"
        />
        <div className="text-xs text-gray-500 mt-1">
          Prompt 文本不允许 <code>${'{user.*}'}</code> 占位符（会被引擎拒绝）。
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={props.onCancel} className="px-3 py-1.5 border rounded">取消</button>
          <button
            onClick={() => props.onSubmit(value)}
            disabled={props.saving}
            className="px-3 py-1.5 bg-blue-600 text-white rounded disabled:opacity-50"
          >
            {props.saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}
