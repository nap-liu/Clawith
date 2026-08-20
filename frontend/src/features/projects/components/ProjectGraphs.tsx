import { useEffect, useMemo, useRef } from 'react';
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
} from '@xyflow/react';
import {
    IconBolt,
    IconBox,
    IconBrandGit,
    IconBroadcast,
    IconGitCommit,
    IconRobot,
    IconTargetArrow,
} from '@tabler/icons-react';

import '@xyflow/react/dist/style.css';
import './ProjectGraphs.css';
import { resolveProjectSessionRoute } from '../projectSessionRouting';

export type ProjectGraphRecord = Record<string, unknown>;

type GraphTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';
type GraphDirection = 'horizontal' | 'vertical';
type GraphNodeData = {
    label: string;
    caption: string;
    meta?: string;
    badge?: string;
    tone: GraphTone;
    kind: 'agent' | 'mesh' | 'source' | 'snapshot' | 'run' | 'work' | 'commit';
    interactive?: boolean;
};
type ProjectGraphNode = Node<GraphNodeData, 'projectGraph'>;
type Selection = { kind: GraphNodeData['kind']; id: string; record: ProjectGraphRecord };

const graphNodeTypes = { projectGraph: ProjectGraphNodeView };

const asRecord = (value: unknown): ProjectGraphRecord => value && typeof value === 'object'
    ? value as ProjectGraphRecord
    : {};

const valueText = (source: ProjectGraphRecord, ...keys: string[]): string => {
    for (const key of keys) {
        const value = source[key];
        if (typeof value === 'string' || typeof value === 'number') return String(value);
    }
    return '';
};

const nestedText = (source: ProjectGraphRecord, keys: string[]): string => {
    const direct = valueText(source, ...keys);
    if (direct) return direct;
    for (const container of ['event_metadata', 'metadata', 'payload', 'input', 'output']) {
        const found = valueText(asRecord(source[container]), ...keys);
        if (found) return found;
    }
    return '';
};

const valueList = (source: ProjectGraphRecord, ...keys: string[]): string[] => {
    for (const key of keys) {
        const raw = source[key];
        if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
        if (typeof raw === 'string' && raw.trim()) {
            try {
                const parsed = JSON.parse(raw) as unknown;
                if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
            } catch {
                return raw.split(',').map((entry) => entry.trim()).filter(Boolean);
            }
        }
    }
    return [];
};

const compactId = (value: string): string => value.length > 12 ? value.slice(0, 8) : value;

const dateLabel = (raw: unknown): string => {
    if (!raw) return '时间未记录';
    const parsed = new Date(String(raw));
    return Number.isNaN(parsed.getTime())
        ? String(raw)
        : new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(parsed);
};

const statusTone = (status: string): GraphTone => {
    if (['done', 'completed', 'success', 'succeeded'].includes(status)) return 'success';
    if (['blocked', 'failed', 'error'].includes(status)) return 'danger';
    if (['waiting', 'review', 'paused'].includes(status)) return 'warning';
    if (['running', 'doing', 'in_progress', 'queued'].includes(status)) return 'info';
    return 'neutral';
};

const statusLabel = (status: string): string => ({
    backlog: '待规划', todo: '待处理', in_progress: '进行中', doing: '进行中', running: '运行中',
    queued: '排队中', review: '待评审', blocked: '阻塞', waiting: '等待中', paused: '已暂停',
    done: '已完成', completed: '已完成', success: '已完成', succeeded: '已完成', failed: '失败',
}[status] || status || '未设置状态');

function GraphIcon({ kind }: { kind: GraphNodeData['kind'] }) {
    if (kind === 'agent') return <IconRobot size={17} stroke={1.8} />;
    if (kind === 'mesh' || kind === 'source') return <IconBroadcast size={17} stroke={1.8} />;
    if (kind === 'snapshot') return <IconBox size={17} stroke={1.8} />;
    if (kind === 'run') return <IconBolt size={17} stroke={1.8} />;
    if (kind === 'work') return <IconTargetArrow size={17} stroke={1.8} />;
    return <IconGitCommit size={17} stroke={1.8} />;
}

function ProjectGraphNodeView({ data, selected }: NodeProps<ProjectGraphNode>) {
    const vertical = data.kind === 'commit';
    return (
        <div className={`project-graph__node is-${data.tone}${selected ? ' is-selected' : ''}${data.interactive === false ? ' is-static' : ''}`}>
            <Handle type="target" position={vertical ? Position.Bottom : Position.Left} className="project-graph__handle project-graph__handle--target" />
            <span className="project-graph__node-icon"><GraphIcon kind={data.kind} /></span>
            <span className="project-graph__node-copy">
                <strong>{data.label}</strong>
                <small>{data.caption}</small>
                {data.meta && <code>{data.meta}</code>}
            </span>
            {data.badge && <em>{data.badge}</em>}
            <Handle type="source" position={vertical ? Position.Top : Position.Right} className="project-graph__handle project-graph__handle--source" />
        </div>
    );
}

const edgeStyle = { stroke: 'var(--text-tertiary)', strokeWidth: 1.4 };
const edgeLabelStyle = { fill: 'var(--text-secondary)', fontSize: 9, fontWeight: 600 };
const edgeLabelBgStyle = { fill: 'var(--bg-elevated)', fillOpacity: 1 };

function makeEdge(id: string, source: string, target: string, options: Partial<Edge> = {}): Edge {
    return {
        id,
        source,
        target,
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--text-tertiary)', width: 15, height: 15 },
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
    direction = 'horizontal',
    miniMap = false,
    focusNodeIds,
    fitViewMinZoom,
    fitViewMaxZoom,
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
    summary?: string;
    onSelect?: (selection: Selection) => void;
}) {
    const [nodes, setNodes, onNodesChange] = useNodesState<ProjectGraphNode>(sourceNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(sourceEdges);
    const { fitView } = useReactFlow<ProjectGraphNode, Edge>();
    const nodesInitialized = useNodesInitialized();
    const viewportRef = useRef<HTMLDivElement>(null);
    const signature = useMemo(
        () => `${sourceNodes.map((node) => node.id).join('|')}::${sourceEdges.map((edge) => edge.id).join('|')}::${focusNodeIds?.join('|') || ''}`,
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
            if (width <= 0 || height <= 0 || (width === previousWidth && height === previousHeight)) return;
            previousWidth = width;
            previousHeight = height;
            window.cancelAnimationFrame(frame);
            frame = window.requestAnimationFrame(() => void fitView({
                padding: 0.18,
                duration,
                ...(focusNodes?.length ? { nodes: focusNodes } : {}),
                ...(fitViewMinZoom === undefined ? {} : { minZoom: fitViewMinZoom }),
                ...(fitViewMaxZoom === undefined ? {} : { maxZoom: fitViewMaxZoom }),
            }));
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
    }, [fitView, fitViewMaxZoom, fitViewMinZoom, focusNodes, nodesInitialized, signature]);

    return (
        <div ref={viewportRef} className={`project-graph project-graph--${direction}`} role="img" aria-label={ariaLabel}>
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
                nodesConnectable={false}
                elementsSelectable
                proOptions={{ hideAttribution: true }}
            >
                <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border-default)" />
                <Controls showInteractive={false} position="bottom-left" aria-label="图形缩放与适配控制" />
                {miniMap && (
                    <MiniMap
                        position="bottom-right"
                        pannable
                        zoomable
                        nodeColor="var(--bg-active)"
                        nodeStrokeColor="var(--border-strong)"
                        maskColor="var(--bg-primary)"
                        ariaLabel="图形缩略导航"
                    />
                )}
            </ReactFlow>
            {summary && <div className="project-graph__summary" role="status">{summary}</div>}
        </div>
    );
}

export interface A2AMeshGraphProps {
    members: ProjectGraphRecord[];
    events?: ProjectGraphRecord[];
    selectedAgentId?: string;
    onAgentSelect?: (agentId: string, member: ProjectGraphRecord) => void;
}

export function A2AMeshGraph({ members, events = [], selectedAgentId, onAgentSelect }: A2AMeshGraphProps) {
    const ordered = useMemo(() => [...members].sort((left, right) => {
        const leaderDelta = Number(right.is_leader === true) - Number(left.is_leader === true);
        if (leaderDelta) return leaderDelta;
        return valueText(left, 'name_snapshot', 'agent_name', 'name').localeCompare(valueText(right, 'name_snapshot', 'agent_name', 'name'), 'zh-CN');
    }), [members]);
    const memberIds = useMemo(() => new Set(ordered.map((member) => valueText(member, 'agent_id', 'id', 'member_id')).filter(Boolean)), [ordered]);
    const relationshipCounts = useMemo(() => {
        const counts = new Map<string, { source: string; target: string; count: number; latestType: string }>();
        for (const event of events) {
            const source = nestedText(event, ['from_agent_id', 'source_agent_id']);
            const target = nestedText(event, ['to_agent_id', 'target_agent_id']);
            if (!source || !target || source === target || !memberIds.has(source) || !memberIds.has(target)) continue;
            const key = `${source}->${target}`;
            const current = counts.get(key);
            counts.set(key, {
                source,
                target,
                count: (current?.count || 0) + 1,
                latestType: valueText(event, 'event_type', 'type') || current?.latestType || 'A2A',
            });
        }
        return [...counts.values()].sort((left, right) => `${left.source}${left.target}`.localeCompare(`${right.source}${right.target}`));
    }, [events, memberIds]);
    const leaderId = valueText(ordered.find((member) => member.is_leader === true) || ordered[0] || {}, 'agent_id', 'id', 'member_id');
    const participants = ordered.filter((member) => valueText(member, 'agent_id', 'id', 'member_id') !== leaderId);
    const nodes = useMemo<ProjectGraphNode[]>(() => ordered.map<ProjectGraphNode>((member) => {
        const id = valueText(member, 'agent_id', 'id', 'member_id');
        const isLeader = member.is_leader === true;
        const enabled = member.is_enabled !== false;
        const participantIndex = participants.findIndex((entry) => valueText(entry, 'agent_id', 'id', 'member_id') === id);
        const columns = Math.min(4, Math.max(1, participants.length));
        const row = Math.floor(Math.max(0, participantIndex) / columns);
        const column = Math.max(0, participantIndex) % columns;
        const rowCount = Math.min(columns, Math.max(1, participants.length - (row * columns)));
        return {
            id,
            type: 'projectGraph',
            position: isLeader || id === leaderId
                ? { x: 0, y: 0 }
                : { x: (column - ((rowCount - 1) / 2)) * 330, y: 230 + (row * 150) },
            data: {
                kind: 'agent',
                label: valueText(member, 'name_snapshot', 'agent_name', 'name') || '未命名 Agent',
                caption: isLeader ? '项目 Leader' : valueText(member, 'role_snapshot', 'role') || '项目成员',
                meta: compactId(id),
                badge: !enabled ? '已停用' : selectedAgentId === id ? '已选择' : undefined,
                tone: !enabled ? 'neutral' : isLeader ? 'info' : 'success',
            },
        };
    }), [leaderId, ordered, participants, selectedAgentId]);
    const edges = useMemo<Edge[]>(() => [
        ...ordered.filter((member) => member.is_enabled !== false && valueText(member, 'agent_id', 'id', 'member_id') !== leaderId).map((member) => {
            const agentId = valueText(member, 'agent_id', 'id', 'member_id');
            return makeEdge(`leader-entry-${agentId}`, leaderId, agentId, {
                type: 'bezier',
                label: '协作入口',
                markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--text-tertiary)', width: 15, height: 15 },
                style: { ...edgeStyle, strokeDasharray: '4 5' },
            });
        }),
        ...relationshipCounts.map((relation) => makeEdge(
            `a2a-${relation.source}-${relation.target}`,
            relation.source,
            relation.target,
            {
                type: 'bezier',
                animated: relation.latestType.includes('queued') || relation.latestType.includes('wake'),
                label: relation.count > 1 ? `${relation.count} 次` : relation.latestType.replace('a2a.', ''),
                style: { stroke: 'var(--info)', strokeWidth: 2 },
                markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--info)', width: 15, height: 15 },
            },
        )),
    ], [leaderId, ordered, relationshipCounts]);
    const records = useMemo(() => new Map<string, ProjectGraphRecord>(ordered.map((member) => [valueText(member, 'agent_id', 'id', 'member_id'), member] as [string, ProjectGraphRecord])), [ordered]);

    return (
        <ProjectGraphCanvas
            ariaLabel={`A2A 成员关系图，共 ${ordered.length} 个 Agent、${relationshipCounts.length} 条已发生协作关系`}
            nodes={nodes}
            edges={edges}
            records={records}
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

export function SnapshotLineageGraph({ member, runs = [], runSnapshots = [], onNodeSelect }: SnapshotLineageGraphProps) {
    const graph = useMemo(() => {
        if (!member) return { nodes: [] as ProjectGraphNode[], edges: [] as Edge[], records: new Map<string, ProjectGraphRecord>(), visibleRunCount: 0, totalRunCount: 0 };
        const memberId = valueText(member, 'id', 'member_id');
        const agentId = valueText(member, 'agent_id');
        const sourceId = `source:${agentId || memberId}`;
        const projectId = `project:${memberId || agentId}`;
        const matchingSnapshots = runSnapshots.filter((snapshot) => {
            const snapshotMemberId = valueText(snapshot, 'project_member_id', 'member_id');
            const snapshotAgentId = valueText(snapshot, 'agent_id');
            return (!snapshotMemberId && !snapshotAgentId) || snapshotMemberId === memberId || snapshotAgentId === agentId;
        });
        const matchingRuns = matchingSnapshots.length
            ? matchingSnapshots
            : runs.filter((run) => !valueText(run, 'agent_id') || valueText(run, 'agent_id') === agentId);
        const traceableRuns = matchingRuns.map((entry) => {
            const runRecord = matchingSnapshots.length
                ? runs.find((run) => valueText(run, 'id', 'run_id') === valueText(entry, 'run_id')) || entry
                : entry;
            return { entry, runRecord };
        }).filter(({ runRecord }) => Boolean(resolveProjectSessionRoute(runRecord, 'run'))).sort((left, right) => {
            const timestamp = (record: ProjectGraphRecord) => {
                const raw = valueText(record, 'finished_at', 'started_at', 'updated_at', 'created_at');
                const parsed = raw ? new Date(raw).getTime() : 0;
                return Number.isFinite(parsed) ? parsed : 0;
            };
            return timestamp(right.runRecord) - timestamp(left.runRecord)
                || valueText(right.runRecord, 'id', 'run_id').localeCompare(valueText(left.runRecord, 'id', 'run_id'));
        });
        const visibleRuns = traceableRuns.slice(0, 8);
        const runGap = 118;
        const runColumns = visibleRuns.length > 4 ? 2 : 1;
        const rowsPerColumn = Math.ceil(visibleRuns.length / runColumns);
        const nodes: ProjectGraphNode[] = [
            {
                id: sourceId,
                type: 'projectGraph',
                position: { x: 0, y: -190 },
                data: {
                    kind: 'source',
                    label: valueText(member, 'name_snapshot', 'agent_name', 'name') || '源 Agent',
                    caption: '全局身份与基础能力',
                    meta: compactId(agentId),
                    badge: '只读源',
                    tone: 'neutral',
                    interactive: false,
                },
            },
            {
                id: projectId,
                type: 'projectGraph',
                position: { x: 310, y: -190 },
                data: {
                    kind: 'snapshot',
                    label: '项目隔离快照',
                    caption: valueText(member, 'role_snapshot', 'role') || '项目成员配置',
                    meta: compactId(memberId),
                    badge: member.is_leader === true ? 'Leader' : member.is_enabled === false ? '已停用' : '生效中',
                    tone: member.is_enabled === false ? 'neutral' : 'info',
                    interactive: false,
                },
            },
        ];
        const records = new Map<string, ProjectGraphRecord>([[sourceId, member], [projectId, member]]);
        const edges: Edge[] = [makeEdge('source-to-project', sourceId, projectId, { label: '创建快照' })];
        visibleRuns.forEach(({ entry, runRecord }, index) => {
            const runId = valueText(entry, 'run_id', 'id') || `${index}`;
            const nodeId = `run:${runId}`;
            const status = valueText(runRecord, 'status') || 'frozen';
            const column = Math.floor(index / rowsPerColumn);
            const row = index % rowsPerColumn;
            nodes.push({
                id: nodeId,
                type: 'projectGraph',
                position: {
                    x: column * 310,
                    y: -30 + (row * runGap),
                },
                data: {
                    kind: 'run',
                    label: valueText(runRecord, 'name', 'title') || `Run ${compactId(runId)}`,
                    caption: `${status === 'frozen' ? '已冻结' : statusLabel(status)} · ${dateLabel(runRecord.created_at)}`,
                    meta: compactId(runId),
                    badge: '不可变',
                    tone: statusTone(status),
                    interactive: true,
                },
            });
            records.set(nodeId, runRecord);
            edges.push(makeEdge(`project-to-${nodeId}`, projectId, nodeId, { label: '运行时冻结' }));
        });
        return {
            nodes,
            edges,
            records,
            visibleRunCount: visibleRuns.length,
            totalRunCount: matchingRuns.length,
        };
    }, [member, runSnapshots, runs]);

    return (
        <ProjectGraphCanvas
            ariaLabel={`成员快照血缘图，最近 ${graph.visibleRunCount} 个可追溯 Run，共 ${graph.totalRunCount} 个 Run`}
            nodes={graph.nodes}
            edges={graph.edges}
            records={graph.records}
            fitViewMinZoom={0.82}
            fitViewMaxZoom={1}
            summary={`最近 ${graph.visibleRunCount} / 共 ${graph.totalRunCount} 次 Run`}
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
    const ids = new Set(items.map((item) => valueText(item, 'id', 'work_item_id')).filter(Boolean));
    const dependencies = new Map(items.map((item) => {
        const id = valueText(item, 'id', 'work_item_id');
        const refs = [...new Set(valueList(item, 'dependency_ids'))].filter((ref) => ref && ref !== id && ids.has(ref));
        return [id, refs] as const;
    }));
    const ranks = new Map<string, number>();
    const unresolved = new Set(ids);
    let changed = true;
    while (unresolved.size && changed) {
        changed = false;
        for (const id of [...unresolved]) {
            const refs = dependencies.get(id) || [];
            if (refs.some((dependency) => !ranks.has(dependency))) continue;
            ranks.set(id, refs.length ? Math.max(...refs.map((dependency) => ranks.get(dependency) || 0)) + 1 : 0);
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

export function WorkDependencyGraph({ items, selectedWorkItemId, onWorkItemSelect }: WorkDependencyGraphProps) {
    const graph = useMemo(() => {
        const ranks = dependencyRanks(items);
        const grouped = new Map<number, ProjectGraphRecord[]>();
        for (const item of items) {
            const rank = ranks.get(valueText(item, 'id', 'work_item_id')) || 0;
            grouped.set(rank, [...(grouped.get(rank) || []), item]);
        }
        for (const [rank, entries] of grouped) {
            grouped.set(rank, entries.sort((left, right) => valueText(left, 'title', 'name').localeCompare(valueText(right, 'title', 'name'), 'zh-CN')));
        }
        const nodes: ProjectGraphNode[] = [];
        const records = new Map<string, ProjectGraphRecord>();
        for (const [rank, entries] of [...grouped.entries()].sort(([left], [right]) => left - right)) {
            const gap = 118;
            const firstY = -((entries.length - 1) * gap) / 2;
            entries.forEach((item, index) => {
                const id = valueText(item, 'id', 'work_item_id');
                const status = valueText(item, 'status', 'state');
                nodes.push({
                    id,
                    type: 'projectGraph',
                    position: { x: rank * 310, y: firstY + (index * gap) },
                    data: {
                        kind: 'work',
                        label: valueText(item, 'title', 'name') || '未命名工作项',
                        caption: valueText(item, 'assignee_name', 'agent_name', 'assignee_agent_id') || '未指派',
                        meta: compactId(id),
                        badge: selectedWorkItemId === id ? '已选择' : statusLabel(status),
                        tone: statusTone(status),
                    },
                });
                records.set(id, item);
            });
        }
        const edges: Edge[] = [];
        const knownIds = new Set(nodes.map((node) => node.id));
        for (const item of items) {
            const target = valueText(item, 'id', 'work_item_id');
            const dependencyIds = [...new Set(valueList(item, 'dependency_ids'))];
            for (const dependency of dependencyIds) {
                if (dependency !== target && knownIds.has(dependency)) {
                    edges.push(makeEdge(`dependency-${dependency}-${target}`, dependency, target, { label: '依赖' }));
                }
            }
        }
        const activeStatuses = new Set(['running', 'doing', 'in_progress', 'review', 'blocked', 'waiting', 'paused']);
        let focusIndex = nodes.findIndex((node) => activeStatuses.has(valueText(records.get(node.id) || {}, 'status', 'state')));
        if (focusIndex < 0) focusIndex = nodes.findIndex((node) => !['done', 'completed', 'success', 'succeeded'].includes(valueText(records.get(node.id) || {}, 'status', 'state')));
        if (focusIndex < 0) focusIndex = Math.max(0, nodes.length - 1);
        const focusStart = Math.min(Math.max(0, focusIndex - 1), Math.max(0, nodes.length - 3));
        const focusNodeIds = nodes.slice(focusStart, focusStart + 3).map((node) => node.id);
        return { nodes, edges, records, focusNodeIds };
    }, [items, selectedWorkItemId]);

    return (
        <ProjectGraphCanvas
            ariaLabel={`工作依赖图，共 ${graph.nodes.length} 个工作项、${graph.edges.length} 条依赖`}
            nodes={graph.nodes}
            edges={graph.edges}
            records={graph.records}
            miniMap={graph.nodes.length > 4}
            focusNodeIds={graph.focusNodeIds}
            fitViewMinZoom={0.85}
            fitViewMaxZoom={1}
            onSelect={({ id, record }) => onWorkItemSelect?.(id, record)}
        />
    );
}

export interface GitHistoryGraphProps {
    commits: ProjectGraphRecord[];
    selectedCommitId?: string;
    onCommitSelect?: (commitId: string, commit: ProjectGraphRecord) => void;
}

export function GitHistoryGraph({ commits, selectedCommitId, onCommitSelect }: GitHistoryGraphProps) {
    const graph = useMemo(() => {
        const ids = commits.map((commit) => valueText(commit, 'commit', 'hash', 'commit_hash', 'id')).filter(Boolean);
        const idSet = new Set(ids);
        const branchLanes = new Map<string, number>();
        commits.forEach((commit) => {
            const branch = valueText(commit, 'branch', 'branch_name') || 'main';
            if (!branchLanes.has(branch)) branchLanes.set(branch, branchLanes.size);
        });
        const nodes: ProjectGraphNode[] = commits.map((commit, index) => {
            const id = ids[index];
            const branch = valueText(commit, 'branch', 'branch_name') || 'main';
            const isHead = index === 0 || commit.is_head === true || commit.current === true;
            return {
                id,
                type: 'projectGraph',
                position: { x: (branchLanes.get(branch) || 0) * 270, y: index * 112 },
                data: {
                    kind: 'commit',
                    label: valueText(commit, 'message', 'title') || '未填写提交说明',
                    caption: `${valueText(commit, 'author', 'author_name', 'agent_name') || '项目系统'} · ${dateLabel(commit.created_at)}`,
                    meta: valueText(commit, 'short_commit') || compactId(id),
                    badge: selectedCommitId === id ? '已选择' : isHead ? 'HEAD' : branch !== 'main' ? branch : undefined,
                    tone: isHead ? 'success' : 'neutral',
                },
            };
        });
        const edges: Edge[] = [];
        commits.forEach((commit, index) => {
            const child = ids[index];
            let parents = valueList(commit, 'parents', 'parent_hashes', 'parent_commits');
            const singleParent = valueText(commit, 'parent', 'parent_hash', 'parent_commit');
            if (singleParent) parents = [...parents, singleParent];
            if (!parents.length && ids[index + 1]) parents = [ids[index + 1]];
            for (const parent of [...new Set(parents)]) {
                if (idSet.has(parent)) {
                    edges.push(makeEdge(`git-${parent}-${child}`, parent, child, {
                        type: 'smoothstep',
                        label: parents.length > 1 ? 'merge' : undefined,
                    }));
                }
            }
        });
        return {
            nodes,
            edges,
            records: new Map(commits.map((commit, index) => [ids[index], commit])),
        };
    }, [commits, selectedCommitId]);

    return (
        <ProjectGraphCanvas
            ariaLabel={`Git 历史图，共 ${graph.nodes.length} 个提交`}
            nodes={graph.nodes}
            edges={graph.edges}
            records={graph.records}
            direction="vertical"
            miniMap={graph.nodes.length > 10}
            onSelect={({ id, record }) => onCommitSelect?.(id, record)}
        />
    );
}

export const ProjectGraphLegend = () => (
    <div className="project-graph__legend" aria-label="关系图图例">
        <span><i className="is-info" />当前或运行中</span>
        <span><i className="is-success" />正常或已完成</span>
        <span><i className="is-warning" />等待或评审</span>
        <span><i className="is-danger" />阻塞或失败</span>
    </div>
);
