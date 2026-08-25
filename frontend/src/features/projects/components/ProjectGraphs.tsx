import { useEffect, useMemo, useRef } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  IconBolt,
  IconBox,
  IconBrandGit,
  IconBroadcast,
  IconGitCommit,
  IconRobot,
  IconTargetArrow,
} from "@tabler/icons-react";

import "@xyflow/react/dist/style.css";
import "./ProjectGraphs.css";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import { resolveProjectSessionRoute } from "../projectSessionRouting";

export type ProjectGraphRecord = Record<string, unknown>;

type GraphTone = "neutral" | "info" | "success" | "warning" | "danger";
type GraphDirection = "horizontal" | "vertical";
type GraphNodeData = {
  label: string;
  caption: string;
  meta?: string;
  badge?: string;
  tone: GraphTone;
  kind: "agent" | "mesh" | "source" | "snapshot" | "run" | "work" | "commit";
  interactive?: boolean;
  direction?: GraphDirection;
};
type ProjectGraphNode = Node<GraphNodeData, "projectGraph">;
type Selection = {
  kind: GraphNodeData["kind"];
  id: string;
  record: ProjectGraphRecord;
};

const graphNodeTypes = { projectGraph: ProjectGraphNodeView };

const asRecord = (value: unknown): ProjectGraphRecord =>
  value && typeof value === "object" ? (value as ProjectGraphRecord) : {};

const valueText = (source: ProjectGraphRecord, ...keys: string[]): string => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" || typeof value === "number")
      return String(value);
  }
  return "";
};

const nestedText = (source: ProjectGraphRecord, keys: string[]): string => {
  const direct = valueText(source, ...keys);
  if (direct) return direct;
  for (const container of [
    "event_metadata",
    "metadata",
    "payload",
    "input",
    "output",
  ]) {
    const found = valueText(asRecord(source[container]), ...keys);
    if (found) return found;
  }
  return "";
};

const valueList = (source: ProjectGraphRecord, ...keys: string[]): string[] => {
  for (const key of keys) {
    const raw = source[key];
    if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
    if (typeof raw === "string" && raw.trim()) {
      try {
        const parsed = JSON.parse(raw) as unknown;
        if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
      } catch {
        return raw
          .split(",")
          .map((entry) => entry.trim())
          .filter(Boolean);
      }
    }
  }
  return [];
};

const compactId = (value: string): string =>
  value.length > 12 ? value.slice(0, 8) : value;

const dateLabel = (raw: unknown, t: TFunction, language: string): string => {
  if (!raw) return t("projectGraphs.timeNotRecorded");
  const parsed = new Date(String(raw));
  return Number.isNaN(parsed.getTime())
    ? String(raw)
    : new Intl.DateTimeFormat(language.startsWith("zh") ? "zh-CN" : "en", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
};

const statusTone = (status: string): GraphTone => {
  if (["done", "completed", "success", "succeeded"].includes(status))
    return "success";
  if (["blocked", "failed", "error"].includes(status)) return "danger";
  if (["waiting", "review", "paused"].includes(status)) return "warning";
  if (["running", "doing", "in_progress", "queued"].includes(status))
    return "info";
  return "neutral";
};

const statusLabel = (status: string, t: TFunction): string =>
  status
    ? t(`projectGraphs.status.${status}`, { defaultValue: status })
    : t("projectGraphs.status.unset");

function GraphIcon({ kind }: { kind: GraphNodeData["kind"] }) {
  if (kind === "agent") return <IconRobot size={17} stroke={1.8} />;
  if (kind === "mesh" || kind === "source")
    return <IconBroadcast size={17} stroke={1.8} />;
  if (kind === "snapshot") return <IconBox size={17} stroke={1.8} />;
  if (kind === "run") return <IconBolt size={17} stroke={1.8} />;
  if (kind === "work") return <IconTargetArrow size={17} stroke={1.8} />;
  return <IconGitCommit size={17} stroke={1.8} />;
}

function ProjectGraphNodeView({ data, selected }: NodeProps<ProjectGraphNode>) {
  const vertical = data.direction === "vertical" || data.kind === "commit";
  return (
    <div
      className={`project-graph__node is-${data.tone}${selected ? " is-selected" : ""}${data.interactive === false ? " is-static" : ""}`}
    >
      <Handle
        type="target"
        position={vertical ? Position.Top : Position.Left}
        className="project-graph__handle project-graph__handle--target"
      />
      <span className="project-graph__node-icon">
        <GraphIcon kind={data.kind} />
      </span>
      <span className="project-graph__node-copy">
        <strong>{data.label}</strong>
        <small>{data.caption}</small>
        {data.meta && <code>{data.meta}</code>}
      </span>
      {data.badge && <em>{data.badge}</em>}
      <Handle
        type="source"
        position={vertical ? Position.Bottom : Position.Right}
        className="project-graph__handle project-graph__handle--source"
      />
    </div>
  );
}

const edgeStyle = { stroke: "var(--text-tertiary)", strokeWidth: 1.4 };
const edgeLabelStyle = {
  fill: "var(--text-secondary)",
  fontSize: 9,
  fontWeight: 600,
};
const edgeLabelBgStyle = { fill: "var(--bg-elevated)", fillOpacity: 1 };

function makeEdge(
  id: string,
  source: string,
  target: string,
  options: Partial<Edge> = {},
): Edge {
  return {
    id,
    source,
    target,
    type: "bezier",
    markerEnd: {
      type: MarkerType.ArrowClosed,
      color: "var(--text-tertiary)",
      width: 15,
      height: 15,
    },
    style: edgeStyle,
    labelStyle: edgeLabelStyle,
    labelBgStyle: edgeLabelBgStyle,
    labelBgPadding: [5, 3],
    labelBgBorderRadius: 4,
    ...options,
  };
}

function ProjectGraphCanvas({
  ariaLabel,
  nodes: sourceNodes,
  edges: sourceEdges,
  records,
  direction = "horizontal",
  miniMap = false,
  focusNodeIds,
  fitViewMinZoom,
  fitViewMaxZoom,
  fitViewPadding,
  panOnScroll = false,
  variant,
  summary,
  onSelect,
}: {
  ariaLabel: string;
  nodes: ProjectGraphNode[];
  edges: Edge[];
  records: Map<string, ProjectGraphRecord>;
  direction?: GraphDirection;
  miniMap?: boolean;
  focusNodeIds?: string[];
  fitViewMinZoom?: number;
  fitViewMaxZoom?: number;
  fitViewPadding?: number;
  panOnScroll?: boolean;
  variant?: "mesh" | "lineage";
  summary?: string;
  onSelect?: (selection: Selection) => void;
}) {
  return (
    <ReactFlowProvider>
      <ProjectGraphViewport
        ariaLabel={ariaLabel}
        sourceNodes={sourceNodes}
        sourceEdges={sourceEdges}
        records={records}
        direction={direction}
        miniMap={miniMap}
        focusNodeIds={focusNodeIds}
        fitViewMinZoom={fitViewMinZoom}
        fitViewMaxZoom={fitViewMaxZoom}
        fitViewPadding={fitViewPadding}
        panOnScroll={panOnScroll}
        variant={variant}
        summary={summary}
        onSelect={onSelect}
      />
    </ReactFlowProvider>
  );
}

function ProjectGraphViewport({
  ariaLabel,
  sourceNodes,
  sourceEdges,
  records,
  direction,
  miniMap,
  focusNodeIds,
  fitViewMinZoom,
  fitViewMaxZoom,
  fitViewPadding,
  panOnScroll,
  variant,
  summary,
  onSelect,
}: {
  ariaLabel: string;
  sourceNodes: ProjectGraphNode[];
  sourceEdges: Edge[];
  records: Map<string, ProjectGraphRecord>;
  direction: GraphDirection;
  miniMap: boolean;
  focusNodeIds?: string[];
  fitViewMinZoom?: number;
  fitViewMaxZoom?: number;
  fitViewPadding?: number;
  panOnScroll: boolean;
  variant?: "mesh" | "lineage";
  summary?: string;
  onSelect?: (selection: Selection) => void;
}) {
  const { t } = useTranslation();
  const [nodes, setNodes, onNodesChange] =
    useNodesState<ProjectGraphNode>(sourceNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(sourceEdges);
  const { fitView } = useReactFlow<ProjectGraphNode, Edge>();
  const nodesInitialized = useNodesInitialized();
  const viewportRef = useRef<HTMLDivElement>(null);
  const signature = useMemo(
    () =>
      `${sourceNodes.map((node) => node.id).join("|")}::${sourceEdges.map((edge) => edge.id).join("|")}::${focusNodeIds?.join("|") || ""}`,
    [focusNodeIds, sourceEdges, sourceNodes],
  );
  const focusNodes = useMemo(() => {
    if (!focusNodeIds?.length) return undefined;
    const ids = new Set(focusNodeIds);
    return sourceNodes.filter((node) => ids.has(node.id));
  }, [focusNodeIds, sourceNodes]);

  useEffect(() => {
    setNodes(sourceNodes);
    setEdges(sourceEdges);
  }, [setEdges, setNodes, sourceEdges, sourceNodes]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !nodesInitialized) return;
    let frame = 0;
    let previousWidth = -1;
    let previousHeight = -1;
    const refit = (width: number, height: number, duration = 0) => {
      if (
        width <= 0 ||
        height <= 0 ||
        (width === previousWidth && height === previousHeight)
      )
        return;
      previousWidth = width;
      previousHeight = height;
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(
        () =>
          void fitView({
            padding: fitViewPadding ?? 0.18,
            duration,
            ...(focusNodes?.length ? { nodes: focusNodes } : {}),
            ...(fitViewMinZoom === undefined
              ? {}
              : { minZoom: fitViewMinZoom }),
            ...(fitViewMaxZoom === undefined
              ? {}
              : { maxZoom: fitViewMaxZoom }),
          }),
      );
    };
    const initialRect = viewport.getBoundingClientRect();
    refit(initialRect.width, initialRect.height, 260);
    const resizeObserver = new ResizeObserver(([entry]) => {
      if (entry) refit(entry.contentRect.width, entry.contentRect.height);
    });
    resizeObserver.observe(viewport);
    return () => {
      resizeObserver.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, [
    fitView,
    fitViewMaxZoom,
    fitViewMinZoom,
    fitViewPadding,
    focusNodes,
    nodesInitialized,
    signature,
  ]);

  return (
    <div
      ref={viewportRef}
      className={`project-graph project-graph--${direction}${variant ? ` project-graph--${variant}` : ""}`}
      role="img"
      aria-label={ariaLabel}
      onKeyDown={(event) => {
        if (!["Enter", " "].includes(event.key)) return;
        const target = event.target as HTMLElement;
        const nodeElement = target.closest<HTMLElement>(
          ".react-flow__node[data-id]",
        );
        const node = nodes.find(
          (candidate) => candidate.id === nodeElement?.dataset.id,
        );
        if (!node || node.data.interactive === false) return;
        event.preventDefault();
        onSelect?.({
          kind: node.data.kind,
          id: node.id,
          record: records.get(node.id) || {},
        });
      }}
    >
      <ReactFlow<ProjectGraphNode, Edge>
        nodes={nodes}
        edges={edges}
        nodeTypes={graphNodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => {
          if (node.data.interactive === false) return;
          onSelect?.({
            kind: node.data.kind,
            id: node.id,
            record: records.get(node.id) || {},
          });
        }}
        minZoom={0.35}
        maxZoom={1.8}
        panOnScroll={panOnScroll}
        zoomOnScroll={!panOnScroll}
        nodesConnectable={false}
        elementsSelectable
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={22}
          size={1}
          color="var(--border-default)"
        />
        <Controls
          showInteractive={false}
          position="bottom-left"
          aria-label={t("projectGraphs.controlsAria")}
        />
        {miniMap && (
          <MiniMap
            position="bottom-right"
            pannable
            zoomable
            nodeColor="var(--bg-active)"
            nodeStrokeColor="var(--border-strong)"
            maskColor="var(--bg-primary)"
            ariaLabel={t("projectGraphs.minimapAria")}
          />
        )}
      </ReactFlow>
      {summary && (
        <div className="project-graph__summary" role="status">
          {summary}
        </div>
      )}
    </div>
  );
}

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
          ? valueText(event, "event_type", "type") || "A2A"
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
    grouped.forEach((entries, rank) => {
      const sortedEntries = [...entries].sort((left, right) => {
        const degree = (entry: ProjectGraphRecord) => {
          const id = valueText(entry, "agent_id", "id", "member_id");
          return relationshipCounts.filter(
            ({ source, target }) => source === id || target === id,
          ).length;
        };
        return (
          degree(right) - degree(left) ||
          valueText(left, "name_snapshot", "agent_name", "name").localeCompare(
            valueText(right, "name_snapshot", "agent_name", "name"),
            "zh-CN",
          )
        );
      });
      const firstX = -((sortedEntries.length - 1) * 286) / 2;
      sortedEntries.forEach((member, index) =>
        positions.set(valueText(member, "agent_id", "id", "member_id"), {
          x: firstX + index * 286,
          y: rank * 170,
        }),
      );
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
      fitViewMinZoom={0.72}
      fitViewMaxZoom={1.12}
      miniMap={ordered.length > 7}
      onSelect={({ id, record }) => onAgentSelect?.(id, record)}
    />
  );
}

export interface SnapshotLineageGraphProps {
  member: ProjectGraphRecord | null;
  runs?: ProjectGraphRecord[];
  runSnapshots?: ProjectGraphRecord[];
  onNodeSelect?: (selection: Selection) => void;
}

export function SnapshotLineageGraph({
  member,
  runs = [],
  runSnapshots = [],
  onNodeSelect,
}: SnapshotLineageGraphProps) {
  const { t, i18n } = useTranslation();
  const graph = useMemo(() => {
    if (!member)
      return {
        nodes: [] as ProjectGraphNode[],
        edges: [] as Edge[],
        records: new Map<string, ProjectGraphRecord>(),
        visibleRunCount: 0,
        totalRunCount: 0,
      };
    const memberId = valueText(member, "id", "member_id");
    const agentId = valueText(member, "agent_id");
    const sourceId = `source:${agentId || memberId}`;
    const projectId = `project:${memberId || agentId}`;
    const matchingSnapshots = runSnapshots.filter((snapshot) => {
      const snapshotMemberId = valueText(
        snapshot,
        "project_member_id",
        "member_id",
      );
      const snapshotAgentId = valueText(snapshot, "agent_id");
      return (
        (!snapshotMemberId && !snapshotAgentId) ||
        snapshotMemberId === memberId ||
        snapshotAgentId === agentId
      );
    });
    const matchingRuns = matchingSnapshots.length
      ? matchingSnapshots
      : runs.filter(
          (run) =>
            !valueText(run, "agent_id") ||
            valueText(run, "agent_id") === agentId,
        );
    const traceableRuns = matchingRuns
      .map((entry) => {
        const runRecord = matchingSnapshots.length
          ? runs.find(
              (run) =>
                valueText(run, "id", "run_id") === valueText(entry, "run_id"),
            ) || entry
          : entry;
        return { entry, runRecord };
      })
      .filter(({ runRecord }) =>
        Boolean(resolveProjectSessionRoute(runRecord, "run")),
      )
      .sort((left, right) => {
        const timestamp = (record: ProjectGraphRecord) => {
          const raw = valueText(
            record,
            "finished_at",
            "started_at",
            "updated_at",
            "created_at",
          );
          const parsed = raw ? new Date(raw).getTime() : 0;
          return Number.isFinite(parsed) ? parsed : 0;
        };
        return (
          timestamp(right.runRecord) - timestamp(left.runRecord) ||
          valueText(right.runRecord, "id", "run_id").localeCompare(
            valueText(left.runRecord, "id", "run_id"),
          )
        );
      });
    const visibleRuns = traceableRuns.slice(0, 8);
    const runsPerColumn = Math.min(4, Math.max(1, visibleRuns.length));
    const runGap = 112;
    const firstRunY = -((runsPerColumn - 1) * runGap) / 2;
    const nodes: ProjectGraphNode[] = [
      {
        id: sourceId,
        type: "projectGraph",
        position: { x: 0, y: 0 },
        data: {
          kind: "source",
          label:
            valueText(member, "name_snapshot", "agent_name", "name") ||
            t("projectGraphs.sourceAgent"),
          caption: t("projectGraphs.sourceCaption"),
          meta: compactId(agentId),
          badge: t("projectGraphs.readOnlySource"),
          tone: "neutral",
          interactive: false,
        },
      },
      {
        id: projectId,
        type: "projectGraph",
        position: { x: 340, y: 0 },
        data: {
          kind: "snapshot",
          label: t("projectGraphs.isolatedSnapshot"),
          caption: projectUserFacingCopy(
            valueText(member, "role_snapshot", "role") ||
              t("projectGraphs.memberConfig"),
            t,
          ),
          meta: compactId(memberId),
          badge:
            member.is_leader === true
              ? t("projectTerminology.owner")
              : member.is_enabled === false
                ? t("projectGraphs.disabled")
                : t("projectGraphs.active"),
          tone: member.is_enabled === false ? "neutral" : "info",
          interactive: false,
        },
      },
    ];
    const records = new Map<string, ProjectGraphRecord>([
      [sourceId, member],
      [projectId, member],
    ]);
    const edges: Edge[] = [
      makeEdge("source-to-project", sourceId, projectId, {
        label: t("projectGraphs.snapshotCreated"),
      }),
    ];
    visibleRuns.forEach(({ entry, runRecord }, index) => {
      const runId = valueText(entry, "run_id", "id") || `${index}`;
      const nodeId = `run:${runId}`;
      const status = valueText(runRecord, "status") || "frozen";
      const column = Math.floor(index / runsPerColumn);
      const row = index % runsPerColumn;
      nodes.push({
        id: nodeId,
        type: "projectGraph",
        position: {
          x: 680 + column * 300,
          y: firstRunY + row * runGap,
        },
        data: {
          kind: "run",
          label:
            valueText(runRecord, "name", "title") || `Run ${compactId(runId)}`,
          caption: `${
            status === "frozen"
              ? t("projectGraphs.frozen")
              : statusLabel(status, t)
          } · ${dateLabel(runRecord.created_at, t, i18n.resolvedLanguage || i18n.language)}`,
          meta: compactId(runId),
          badge: t("projectGraphs.frozen"),
          tone: statusTone(status),
          interactive: true,
        },
      });
      records.set(nodeId, runRecord);
      edges.push(
        makeEdge(`project-to-${nodeId}`, projectId, nodeId, {
          label: t("projectGraphs.runtimeFrozen"),
        }),
      );
    });
    return {
      nodes,
      edges,
      records,
      visibleRunCount: visibleRuns.length,
      totalRunCount: matchingRuns.length,
    };
  }, [i18n.language, i18n.resolvedLanguage, member, runSnapshots, runs, t]);

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.snapshotAria", {
        name:
          valueText(member || {}, "name_snapshot", "agent_name", "name") ||
          t("projectGraphs.unnamedAgent"),
        runs: graph.totalRunCount,
      })}
      nodes={graph.nodes}
      edges={graph.edges}
      records={graph.records}
      variant="lineage"
      fitViewMinZoom={0.35}
      fitViewMaxZoom={0.95}
      fitViewPadding={0.2}
      panOnScroll
      summary={t("projectGraphs.snapshotSummary", {
        shown: graph.visibleRunCount,
        total: graph.totalRunCount,
      })}
      onSelect={onNodeSelect}
    />
  );
}

export interface WorkDependencyGraphProps {
  items: ProjectGraphRecord[];
  selectedWorkItemId?: string;
  onWorkItemSelect?: (workItemId: string, item: ProjectGraphRecord) => void;
}

function dependencyRanks(items: ProjectGraphRecord[]): Map<string, number> {
  const ids = new Set(
    items.map((item) => valueText(item, "id", "work_item_id")).filter(Boolean),
  );
  const dependencies = new Map(
    items.map((item) => {
      const id = valueText(item, "id", "work_item_id");
      const refs = [...new Set(valueList(item, "dependency_ids"))].filter(
        (ref) => ref && ref !== id && ids.has(ref),
      );
      return [id, refs] as const;
    }),
  );
  const ranks = new Map<string, number>();
  const unresolved = new Set(ids);
  let changed = true;
  while (unresolved.size && changed) {
    changed = false;
    for (const id of [...unresolved]) {
      const refs = dependencies.get(id) || [];
      if (refs.some((dependency) => !ranks.has(dependency))) continue;
      ranks.set(
        id,
        refs.length
          ? Math.max(...refs.map((dependency) => ranks.get(dependency) || 0)) +
              1
          : 0,
      );
      unresolved.delete(id);
      changed = true;
    }
  }
  // Cycles are invalid project data, but the graph must remain inspectable.
  // Keep every unresolved node together after the valid DAG instead of recursing forever.
  const cycleRank = Math.max(-1, ...ranks.values()) + 1;
  [...unresolved].sort().forEach((id) => ranks.set(id, cycleRank));
  return ranks;
}

export function WorkDependencyGraph({
  items,
  selectedWorkItemId,
  onWorkItemSelect,
}: WorkDependencyGraphProps) {
  const { t, i18n } = useTranslation();
  const graph = useMemo(() => {
    const ranks = dependencyRanks(items);
    const grouped = new Map<number, ProjectGraphRecord[]>();
    for (const item of items) {
      const rank = ranks.get(valueText(item, "id", "work_item_id")) || 0;
      grouped.set(rank, [...(grouped.get(rank) || []), item]);
    }
    for (const [rank, entries] of grouped) {
      grouped.set(
        rank,
        entries.sort((left, right) =>
          valueText(left, "title", "name").localeCompare(
            valueText(right, "title", "name"),
            i18n.resolvedLanguage || i18n.language,
          ),
        ),
      );
    }
    const nodes: ProjectGraphNode[] = [];
    const records = new Map<string, ProjectGraphRecord>();
    for (const [rank, entries] of [...grouped.entries()].sort(
      ([left], [right]) => left - right,
    )) {
      const gap = 118;
      const firstY = -((entries.length - 1) * gap) / 2;
      entries.forEach((item, index) => {
        const id = valueText(item, "id", "work_item_id");
        const status = valueText(item, "status", "state");
        nodes.push({
          id,
          type: "projectGraph",
          position: { x: rank * 310, y: firstY + index * gap },
          data: {
            kind: "work",
            label:
              valueText(item, "title", "name") ||
              t("projectGraphs.unnamedWorkItem"),
            caption:
              valueText(
                item,
                "assignee_name",
                "agent_name",
                "assignee_agent_id",
              ) || t("projectGraphs.unassigned"),
            meta: compactId(id),
            badge:
              selectedWorkItemId === id
                ? t("projectGraphs.selected")
                : statusLabel(status, t),
            tone: statusTone(status),
          },
        });
        records.set(id, item);
      });
    }
    const edges: Edge[] = [];
    const knownIds = new Set(nodes.map((node) => node.id));
    for (const item of items) {
      const target = valueText(item, "id", "work_item_id");
      const dependencyIds = [...new Set(valueList(item, "dependency_ids"))];
      for (const dependency of dependencyIds) {
        if (dependency !== target && knownIds.has(dependency)) {
          edges.push(
            makeEdge(`dependency-${dependency}-${target}`, dependency, target, {
              label: t("projectGraphs.dependency"),
            }),
          );
        }
      }
    }
    const activeStatuses = new Set([
      "running",
      "doing",
      "in_progress",
      "review",
      "blocked",
      "waiting",
      "paused",
    ]);
    let focusIndex = nodes.findIndex((node) =>
      activeStatuses.has(
        valueText(records.get(node.id) || {}, "status", "state"),
      ),
    );
    if (focusIndex < 0)
      focusIndex = nodes.findIndex(
        (node) =>
          !["done", "completed", "success", "succeeded"].includes(
            valueText(records.get(node.id) || {}, "status", "state"),
          ),
      );
    if (focusIndex < 0) focusIndex = Math.max(0, nodes.length - 1);
    const focusStart = Math.min(
      Math.max(0, focusIndex - 1),
      Math.max(0, nodes.length - 3),
    );
    const focusNodeIds = nodes
      .slice(focusStart, focusStart + 3)
      .map((node) => node.id);
    return { nodes, edges, records, focusNodeIds };
  }, [i18n.language, i18n.resolvedLanguage, items, selectedWorkItemId, t]);

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.workAria", {
        nodes: graph.nodes.length,
        edges: graph.edges.length,
      })}
      nodes={graph.nodes}
      edges={graph.edges}
      records={graph.records}
      miniMap={graph.nodes.length > 4}
      fitViewMinZoom={0.35}
      fitViewMaxZoom={1}
      fitViewPadding={0.16}
      onSelect={({ id, record }) => onWorkItemSelect?.(id, record)}
    />
  );
}

export interface GitHistoryGraphProps {
  commits: ProjectGraphRecord[];
  selectedCommitId?: string;
  onCommitSelect?: (commitId: string, commit: ProjectGraphRecord) => void;
}

export function GitHistoryGraph({
  commits,
  selectedCommitId,
  onCommitSelect,
}: GitHistoryGraphProps) {
  const { t, i18n } = useTranslation();
  const graph = useMemo(() => {
    const ids = commits
      .map((commit) => valueText(commit, "commit", "hash", "commit_hash", "id"))
      .filter(Boolean);
    const idSet = new Set(ids);
    const branchLanes = new Map<string, number>();
    commits.forEach((commit) => {
      const branch = valueText(commit, "branch", "branch_name") || "main";
      if (!branchLanes.has(branch)) branchLanes.set(branch, branchLanes.size);
    });
    const nodes: ProjectGraphNode[] = commits.map((commit, index) => {
      const id = ids[index];
      const branch = valueText(commit, "branch", "branch_name") || "main";
      const isHead =
        index === 0 || commit.is_head === true || commit.current === true;
      return {
        id,
        type: "projectGraph",
        position: { x: (branchLanes.get(branch) || 0) * 270, y: index * 112 },
        data: {
          kind: "commit",
          label:
            valueText(commit, "message", "title") ||
            t("projectGraphs.commitNoMessage"),
          caption: `${valueText(commit, "author", "author_name", "agent_name") || t("projectGraphs.projectMember")} · ${dateLabel(commit.created_at, t, i18n.resolvedLanguage || i18n.language)}`,
          meta: valueText(commit, "short_commit") || compactId(id),
          badge:
            selectedCommitId === id
              ? t("projectGraphs.selected")
              : isHead
                ? "HEAD"
                : branch !== "main"
                  ? branch
                  : undefined,
          tone: isHead ? "success" : "neutral",
        },
      };
    });
    const edges: Edge[] = [];
    commits.forEach((commit, index) => {
      const child = ids[index];
      let parents = valueList(
        commit,
        "parents",
        "parent_hashes",
        "parent_commits",
      );
      const singleParent = valueText(
        commit,
        "parent",
        "parent_hash",
        "parent_commit",
      );
      if (singleParent) parents = [...parents, singleParent];
      if (!parents.length && ids[index + 1]) parents = [ids[index + 1]];
      for (const parent of [...new Set(parents)]) {
        if (idSet.has(parent)) {
          edges.push(
            makeEdge(`git-${parent}-${child}`, parent, child, {
              label: parents.length > 1 ? "merge" : undefined,
            }),
          );
        }
      }
    });
    return {
      nodes,
      edges,
      records: new Map(commits.map((commit, index) => [ids[index], commit])),
    };
  }, [commits, i18n.language, i18n.resolvedLanguage, selectedCommitId, t]);

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.gitAria", { count: graph.nodes.length })}
      nodes={graph.nodes}
      edges={graph.edges}
      records={graph.records}
      direction="vertical"
      miniMap={graph.nodes.length > 10}
      onSelect={({ id, record }) => onCommitSelect?.(id, record)}
    />
  );
}

export const ProjectGraphLegend = () => {
  const { t } = useTranslation();
  return (
    <div
      className="project-graph__legend"
      aria-label={t("projectGraphs.legendAria")}
    >
      <span>
        <i className="is-info" />
        {t("projectGraphs.legendInfo")}
      </span>
      <span>
        <i className="is-success" />
        {t("projectGraphs.legendSuccess")}
      </span>
      <span>
        <i className="is-warning" />
        {t("projectGraphs.legendWarning")}
      </span>
      <span>
        <i className="is-danger" />
        {t("projectGraphs.legendDanger")}
      </span>
    </div>
  );
};
