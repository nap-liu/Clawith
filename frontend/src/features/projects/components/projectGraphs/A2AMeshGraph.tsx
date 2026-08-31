import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { MarkerType, type Edge } from "@xyflow/react";

import { projectUserFacingCopy } from "../../projectUserFacingCopy";
import {
  edgeStyle,
  makeEdge,
  nestedText,
  ProjectGraphCanvas,
  type ProjectGraphNode,
  type ProjectGraphRecord,
  valueText,
} from "./shared";

export interface A2AMeshGraphProps {
  members: ProjectGraphRecord[];
  events?: ProjectGraphRecord[];
  selectedAgentId?: string;
  onAgentSelect?: (agentId: string, member: ProjectGraphRecord) => void;
}

export function A2AMeshGraph({
  members,
  events = [],
  selectedAgentId,
  onAgentSelect,
}: A2AMeshGraphProps) {
  const { t } = useTranslation();
  const ordered = useMemo(
    () =>
      [...members].sort((left, right) => {
        const leaderDelta =
          Number(right.is_leader === true) - Number(left.is_leader === true);
        if (leaderDelta) return leaderDelta;
        return valueText(
          left,
          "name_snapshot",
          "agent_name",
          "name",
        ).localeCompare(
          valueText(right, "name_snapshot", "agent_name", "name"),
          "zh-CN",
        );
      }),
    [members],
  );
  const memberIds = useMemo(
    () =>
      new Set(
        ordered
          .map((member) => valueText(member, "agent_id", "id", "member_id"))
          .filter(Boolean),
      ),
    [ordered],
  );
  const relationshipCounts = useMemo(() => {
    // The event timeline retains every direction change. The topology is a
    // relationship overview, so reciprocal traffic is collapsed into one
    // pair and rendered in stable owner-first layout order.
    const counts = new Map<
      string,
      {
        source: string;
        target: string;
        count: number;
        latestType: string;
        latestAt: number;
      }
    >();
    events.forEach((event, index) => {
      const source = nestedText(event, ["from_agent_id", "source_agent_id"]);
      const target = nestedText(event, ["to_agent_id", "target_agent_id"]);
      if (
        !source ||
        !target ||
        source === target ||
        !memberIds.has(source) ||
        !memberIds.has(target)
      )
        return;
      const key = [source, target].sort().join("<->");
      const current = counts.get(key);
      const rawTimestamp = nestedText(event, [
        "created_at",
        "updated_at",
        "occurred_at",
      ]);
      const parsedTimestamp = rawTimestamp
        ? new Date(rawTimestamp).getTime()
        : Number.NaN;
      const latestAt = Number.isFinite(parsedTimestamp)
        ? parsedTimestamp
        : index;
      const isLatest = !current || latestAt >= current.latestAt;
      counts.set(key, {
        source: isLatest ? source : current.source,
        target: isLatest ? target : current.target,
        count: (current?.count || 0) + 1,
        latestType: isLatest
          ? valueText(event, "event_type", "type") ||
            t("projectAudit.eventFallback")
          : current.latestType,
        latestAt: Math.max(
          current?.latestAt ?? Number.NEGATIVE_INFINITY,
          latestAt,
        ),
      });
    });
    return [...counts.values()].sort((left, right) =>
      `${left.source}${left.target}`.localeCompare(
        `${right.source}${right.target}`,
      ),
    );
  }, [events, memberIds]);
  const leaderId = valueText(
    ordered.find((member) => member.is_leader === true) || ordered[0] || {},
    "agent_id",
    "id",
    "member_id",
  );
  const ranks = useMemo(() => {
    const adjacency = new Map<string, Set<string>>();
    ordered.forEach((member) =>
      adjacency.set(
        valueText(member, "agent_id", "id", "member_id"),
        new Set(),
      ),
    );
    relationshipCounts.forEach(({ source, target }) => {
      adjacency.get(source)?.add(target);
      adjacency.get(target)?.add(source);
    });
    const distances = new Map<string, number>();
    if (leaderId) distances.set(leaderId, 0);
    const walk = (seed: string, seedRank: number) => {
      const queue = [seed];
      if (!distances.has(seed)) distances.set(seed, seedRank);
      while (queue.length) {
        const current = queue.shift() as string;
        const nextRank = (distances.get(current) || 0) + 1;
        [...(adjacency.get(current) || [])].sort().forEach((neighbor) => {
          if (distances.has(neighbor)) return;
          distances.set(neighbor, nextRank);
          queue.push(neighbor);
        });
      }
    };
    if (leaderId) walk(leaderId, 0);
    // Disconnected collaboration components still begin one column after
    // the project owner, then retain their own shortest-path structure.
    ordered.forEach((member) => {
      const id = valueText(member, "agent_id", "id", "member_id");
      if (id && !distances.has(id)) walk(id, 1);
    });
    return distances;
  }, [leaderId, ordered, relationshipCounts]);
  const nodes = useMemo<ProjectGraphNode[]>(() => {
    const grouped = new Map<number, ProjectGraphRecord[]>();
    ordered.forEach((member) => {
      const id = valueText(member, "agent_id", "id", "member_id");
      const rank = ranks.get(id) ?? (id === leaderId ? 0 : 1);
      grouped.set(rank, [...(grouped.get(rank) || []), member]);
    });
    const positions = new Map<string, { x: number; y: number }>();
    let nextRowY = 0;
    [...grouped.entries()]
      .sort(([leftRank], [rightRank]) => leftRank - rightRank)
      .forEach(([, entries]) => {
        const sortedEntries = [...entries].sort((left, right) => {
          const degree = (entry: ProjectGraphRecord) => {
            const id = valueText(entry, "agent_id", "id", "member_id");
            return relationshipCounts.filter(
              ({ source, target }) => source === id || target === id,
            ).length;
          };
          return (
            degree(right) - degree(left) ||
            valueText(
              left,
              "name_snapshot",
              "agent_name",
              "name",
            ).localeCompare(
              valueText(right, "name_snapshot", "agent_name", "name"),
              "zh-CN",
            )
          );
        });
        const maxColumns = 4;
        const rowCount = Math.ceil(sortedEntries.length / maxColumns);
        sortedEntries.forEach((member, index) => {
          const row = Math.floor(index / maxColumns);
          const column = index % maxColumns;
          const rowSize = Math.min(
            maxColumns,
            sortedEntries.length - row * maxColumns,
          );
          const firstX = -((rowSize - 1) * 286) / 2;
          positions.set(valueText(member, "agent_id", "id", "member_id"), {
            x: firstX + column * 286,
            y: nextRowY + row * 170,
          });
        });
        nextRowY += Math.max(1, rowCount) * 170;
      });
    return ordered.map<ProjectGraphNode>((member) => {
      const id = valueText(member, "agent_id", "id", "member_id");
      const isLeader = member.is_leader === true || id === leaderId;
      const enabled = member.is_enabled !== false;
      return {
        id,
        type: "projectGraph",
        position: positions.get(id) || { x: 0, y: 0 },
        data: {
          kind: "agent",
          direction: "vertical",
          label:
            valueText(member, "name_snapshot", "agent_name", "name") ||
            t("projectGraphs.unnamedAgent"),
          caption: isLeader
            ? t("projectTerminology.projectOwner")
            : projectUserFacingCopy(
                valueText(member, "role_snapshot", "role") ||
                  t("projectGraphs.projectMember"),
                t,
              ),
          badge: !enabled
            ? t("projectGraphs.disabled")
            : selectedAgentId === id
              ? t("projectGraphs.selected")
              : undefined,
          tone: !enabled ? "neutral" : isLeader ? "info" : "success",
        },
      };
    });
  }, [leaderId, ordered, ranks, relationshipCounts, selectedAgentId, t]);
  const edges = useMemo<Edge[]>(
    () => [
      ...ordered
        .filter((member) => {
          const agentId = valueText(member, "agent_id", "id", "member_id");
          return (
            member.is_enabled !== false &&
            agentId !== leaderId &&
            ranks.get(agentId) === 1 &&
            !relationshipCounts.some(
              ({ source, target }) =>
                [source, target].includes(leaderId) &&
                [source, target].includes(agentId),
            )
          );
        })
        .map((member) => {
          const agentId = valueText(member, "agent_id", "id", "member_id");
          return makeEdge(`leader-entry-${agentId}`, leaderId, agentId, {
            label: t("projectGraphs.collaborationEntry"),
            markerEnd: {
              type: MarkerType.ArrowClosed,
              color: "var(--text-tertiary)",
              width: 15,
              height: 15,
            },
            style: { ...edgeStyle, strokeDasharray: "4 5" },
          });
        }),
      ...relationshipCounts.map((relation) => {
        const sourceRank = ranks.get(relation.source) ?? 0;
        const targetRank = ranks.get(relation.target) ?? 0;
        const [visualSource, visualTarget] =
          sourceRank < targetRank ||
          (sourceRank === targetRank &&
            relation.source.localeCompare(relation.target) <= 0)
            ? [relation.source, relation.target]
            : [relation.target, relation.source];
        return makeEdge(
          `a2a-${relation.source}-${relation.target}`,
          visualSource,
          visualTarget,
          {
            animated:
              relation.latestType.includes("queued") ||
              relation.latestType.includes("wake"),
            label:
              relation.count > 1
                ? t("projectGraphs.times", { count: relation.count })
                : relation.latestType.replace("a2a.", ""),
            style: { stroke: "var(--info)", strokeWidth: 2 },
            markerEnd: {
              type: MarkerType.ArrowClosed,
              color: "var(--info)",
              width: 15,
              height: 15,
            },
          },
        );
      }),
    ],
    [leaderId, ordered, ranks, relationshipCounts, t],
  );
  const records = useMemo(
    () =>
      new Map<string, ProjectGraphRecord>(
        ordered.map(
          (member) =>
            [valueText(member, "agent_id", "id", "member_id"), member] as [
              string,
              ProjectGraphRecord,
            ],
        ),
      ),
    [ordered],
  );

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.meshAria", {
        members: ordered.length,
        edges: relationshipCounts.length,
      })}
      nodes={nodes}
      edges={edges}
      records={records}
      variant="mesh"
      direction="vertical"
      fitViewPadding={0.12}
      fitViewMinZoom={0.35}
      fitViewMaxZoom={1.12}
      miniMap={ordered.length > 7}
      onSelect={({ id, record }) => onAgentSelect?.(id, record)}
    />
  );
}
