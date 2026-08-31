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
  IconBroadcast,
  IconGitCommit,
  IconRobot,
  IconTargetArrow,
} from "@tabler/icons-react";

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
export type ProjectGraphNode = Node<GraphNodeData, "projectGraph">;
export type Selection = {
  kind: GraphNodeData["kind"];
  id: string;
  record: ProjectGraphRecord;
};

const graphNodeTypes = { projectGraph: ProjectGraphNodeView };

const asRecord = (value: unknown): ProjectGraphRecord =>
  value && typeof value === "object" ? (value as ProjectGraphRecord) : {};

export const valueText = (
  source: ProjectGraphRecord,
  ...keys: string[]
): string => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" || typeof value === "number")
      return String(value);
  }
  return "";
};

export const nestedText = (
  source: ProjectGraphRecord,
  keys: string[],
): string => {
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

export const valueList = (
  source: ProjectGraphRecord,
  ...keys: string[]
): string[] => {
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

export const compactId = (value: string): string =>
  value.length > 12 ? value.slice(0, 8) : value;

export const dateLabel = (
  raw: unknown,
  t: TFunction,
  language: string,
): string => {
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

export const statusTone = (status: string): GraphTone => {
  if (["done", "completed", "success", "succeeded"].includes(status))
    return "success";
  if (["blocked", "failed", "error"].includes(status)) return "danger";
  if (["waiting", "review", "paused"].includes(status)) return "warning";
  if (["running", "doing", "in_progress", "queued"].includes(status))
    return "info";
  return "neutral";
};

export const statusLabel = (status: string, t: TFunction): string =>
  status
    ? t(`projectGraphs.status.${status}`, {
        defaultValue: t("projectGraphs.status.unknown"),
      })
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

export const edgeStyle = {
  stroke: "var(--text-tertiary)",
  strokeWidth: 1.4,
};
const edgeLabelStyle = {
  fill: "var(--text-secondary)",
  fontSize: 9,
  fontWeight: 600,
};
const edgeLabelBgStyle = { fill: "var(--bg-elevated)", fillOpacity: 1 };

export function makeEdge(
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

export function ProjectGraphCanvas({
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
