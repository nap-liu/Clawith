import { useEffect, useRef, useState } from 'react';
import { mcpOverridesApi } from '../../services/mcpServers';
import type { DraftOverrides, DryRunResponse } from '../../types/mcpServer';

interface Input {
  serverId: string;
  agentId?: string;
  draftOverrides?: DraftOverrides | null;
  identity?: 'current_user' | 'synthetic';
}

export function useResolvedPreview({
  serverId,
  agentId,
  draftOverrides,
  identity = 'current_user',
}: Input) {
  const [data, setData] = useState<DryRunResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      setPending(true);
      setError(null);
      try {
        const resp = await mcpOverridesApi.dryRun(serverId, {
          identity,
          scope: agentId ? 'agent' : 'platform',
          agent_id: agentId ?? null,
          draft_overrides: draftOverrides ?? null,
        });
        setData(resp);
      } catch (e: any) {
        setError(e?.message ?? String(e));
      } finally {
        setPending(false);
      }
    }, 300);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [serverId, agentId, identity, JSON.stringify(draftOverrides ?? {})]);

  return { data, error, pending };
}
